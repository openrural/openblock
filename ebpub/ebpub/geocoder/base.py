#   Copyright 2007,2008,2009,2011 Everyblock LLC, OpenPlans, and contributors
#
#   This file is part of ebpub
#
#   ebpub is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   ebpub is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with ebpub.  If not, see <http://www.gnu.org/licenses/>.
#

"""
Flexible geocoding against our models, including db.Location,
streets.Place, streets.Block, streets.Intersection ...
"""

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from ebpub.geocoder.parser.parsing import normalize, parse, ParsingError
from ebpub.utils.text import address_to_block
import logging
import re

block_re = re.compile(r'^(\d+)[-\s]+(?:blk|block)\s+(?:of\s+)?(.*)$', re.IGNORECASE)
intersection_re = re.compile(r'(?<=.) (?:and|\&|at|near|@|around|towards?|off|/|(?:just )?(?:north|south|east|west) of|(?:just )?past) (?=.)', re.IGNORECASE)
# segment_re = re.compile(r'^.{1,40}?\b(?:between .{1,40}? and|from .{1,40}? to) .{1,40}?$', re.IGNORECASE) # TODO

logger = logging.getLogger('ebpub.geocoder.base')

class GeocodingException(Exception):
    pass

class AmbiguousResult(GeocodingException):
    def __init__(self, choices, message=None):
        self.choices = choices
        if message is None:
            message = "Address DB returned %s results" % len(choices)
        self.message = message

    def __str__(self):
        return self.message

class DoesNotExist(GeocodingException):
    pass

class UnparseableLocation(GeocodingException):
    pass

class InvalidBlockButValidStreet(GeocodingException):
    def __init__(self, block_number, street_name, block_list):
        self.block_number = block_number
        self.street_name = street_name
        self.block_list = block_list

class Address(dict):
    "A simple container class for representing a single street address."
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self._cache_hit = False

    @property
    def latitude(self):
        if self["point"]:
            return self["point"].y
    lat = latitude

    @property
    def longitude(self):
        if self["point"]:
            return self["point"].x
    lng = longitude

    @property
    def location(self):
        """Everything else full_geocode() can return has a .location,
        so this is here for consistency of API."""
        if self["point"]:
            return self["point"]

    def __unicode__(self):
        return u", ".join([self[k] for k in ["address", "city", "state", "zip"]])

    @classmethod
    def from_cache(cls, cached):
        """
        Builds an Address object from a GeocoderCache result object.
        """
        #  This should probably use the normal Django cache, see ticket #163
        fields = {
            'address': cached.address,
            'city': cached.city,
            'state': cached.state,
            'zip': cached.zip,
            'point': cached.location,
            'intersection_id': cached.intersection_id,
        }
        try:
            block_obj = cached.block
        except ObjectDoesNotExist:
            fields.update({'block': None})
        else:
            fields.update({'block': block_obj})
        try:
            intersection_obj = cached.intersection
        except ObjectDoesNotExist:
            fields.update({'intersection': None})
        else:
            fields.update({'intersection': intersection_obj})
        obj = cls(fields)
        obj._cache_hit = True
        return obj

class Geocoder(object):
    """
    Generic Geocoder class.

    Subclasses must override the following attribute:

        _do_geocode(self, location_string)
            Actually performs the geocoding. The base class implementation of
            geocode() calls this behind the scenes.
    """
    def __init__(self, use_cache=True):
        self.use_cache = use_cache

    def geocode(self, location):
        """
        Geocodes the given location, handling caching behind the scenes.
        """
        location = normalize(location)
        result, cache_hit = None, False

        # Get the result (an Address instance), either from the cache or by
        # calling _do_geocode().
        # TODO: Why does this not use the normal Django caching
        # framework?
        # Defer import to avoid cyclical imports.
        from ebpub.geocoder.models import GeocoderCache
        if self.use_cache:
            try:
                cached = GeocoderCache.objects.filter(normalized_location=location)[0]
            except IndexError:
                pass
            else:
                logger.debug('GeocoderCache HIT for %r' % location)
                result = Address.from_cache(cached)
                cache_hit = True

        if result is None:
            try:
                result = self._do_geocode(location)
            except AmbiguousResult, e:
                # If multiple results were found, check whether they have the
                # same point. If they all have the same point, don't raise the
                # AmbiguousResult exception -- just return the first one.
                #
                # An edge case is if result['point'] is None. This could happen
                # if the geocoder found locations, not points. In that case,
                # just raise the AmbiguousResult.
                result = e.choices[0]
                if result['point'] is None:
                    raise
                for i in e.choices[1:]:
                    if i['point'] != result['point']:
                        raise
                logger.debug('Got ambiguous results but all had same point, '
                             'returning the first')
        # Save the result to the cache if it wasn't in there already.
        if not cache_hit and self.use_cache:
            logger.debug('caching result for %r' % location)
            GeocoderCache.populate(location, result)

        logger.debug('geocoded: %r to %s' % (location, result))
        return result


class AddressGeocoder(Geocoder):
    """
    Treats the location_string as an address and looks for a matching Block.
    """
    def _do_geocode(self, location_string):
        # Parse the address.
        try:
            locations = parse(location_string)
        except ParsingError, e:
            raise e

        all_results = []

        # For capturing streets with matching name but no matching block.
        invalid_block_args = []

        for loc in locations:
            logger.debug('AddressGeocoder: Trying %r' % loc)
            loc_results = self._db_lookup(loc)
            # If none were found, maybe the street was
            # misspelled. Check that.
            # Defer import to avoid cyclical import.
            from ebpub.streets.models import StreetMisspelling
            if not loc_results and loc['street']:
                logger.debug('AddressGeocoder: checking for alternate spellings of %r'
                             % loc['street'])
                try:
                    misspelling = StreetMisspelling.objects.get(incorrect=loc['street'])
                    # TODO: stash away the original 'street' value for
                    # possible disambiguation later? ticket #295
                    loc['street'] = misspelling.correct
                    logger.debug(' ... corrected to %r' % loc['street'])
                except StreetMisspelling.DoesNotExist:
                    logger.debug(' ... no StreetMisspellings found.')
                    pass
                else:
                    loc_results = self._db_lookup(loc)
                # Next, try removing the street suffix, in case an incorrect
                # one was given.
                if not loc_results and loc['suffix']:
                    logger.debug('No results, will try removing suffix %r'
                                 % loc['suffix'])
                    loc_results = self._db_lookup(dict(loc, suffix=None))
                # Next, try looking for the street, in case the street
                # (without any suffix) exists but the address doesn't.
                if not loc_results and loc['number']:
                    kwargs = {'street': loc['street']}
                    sided_filters = []
                    if loc['city']:
                        city_filter = Q(left_city=loc['city']) | Q(right_city=loc['city'])
                        sided_filters.append(city_filter)
                    # Defer this to avoid import cycle.
                    from ebpub.streets.models import Block
                    b_list = Block.objects.filter(*sided_filters, **kwargs).order_by('predir', 'from_num', 'to_num')
                    if b_list:
                        # We got some blocks with the bare street name.
                        # Might be InvalidBlockButValidStreet, but we don't
                        # want to raise that till we've tried all locations,
                        # in case there's a better one coming up.
                        logger.debug("Street %r exists but block %r doesn't"
                                     % (b_list[0].street_pretty_name, loc['number']))
                        invalid_block_args = [loc['number'], b_list[0].street_pretty_name, b_list]

            if loc_results:
                logger.debug(u'Success. Adding to results: %s' % [unicode(r) for r in loc_results])
                all_results.extend(loc_results)
            else:
                logger.debug('... Got nothing.')

        if not all_results:
            if invalid_block_args:
                raise InvalidBlockButValidStreet(*invalid_block_args)
            else:
                raise DoesNotExist("Geocoder db couldn't find this location: %r" % location_string)
        elif len(all_results) == 1:
            return all_results[0]
        else:
            raise AmbiguousResult(all_results)


    def _db_lookup(self, location):
        """
        Given a location dict as returned by parse(), looks up the address in
        the DB. Always returns a list of Address dictionaries (or an empty list
        if no results are found).
        """
#        if not location['number']:
#            return []

        # Query the blocks database.
        try:
            # Defer this to avoid import cycle.
            from ebpub.streets.models import Block
            from ebpub.db.models import Location

            # Also searches in cities with matching normalized name.
            cities = [location['city']] + list(Location.objects.filter(normalized_name=location['city']).values_list('name', flat=True))
            blocks = []
            for city in cities:
                blocks.extend(Block.objects.search(
                    street=location['street'],
                    number=location['number'],
                    predir=location['pre_dir'],
                    prefix=location['prefix'],
                    suffix=location['suffix'],
                    postdir=location['post_dir'],
                    city=city,
                    state=location['state'],
                    zipcode=location['zip'],
                ))
        except:
            # TODO: replace with Block-specific exception?
            raise
        return [self._build_result(location, block, geocoded_pt) for block, geocoded_pt in blocks]


    def _build_result(self, location, block, geocoded_pt):
        return Address({
            'address': unicode(" ".join([str(s) for s in [location['number'], block.predir, block.street_pretty_name, block.postdir] if s])),
            'city': block.city.title(),
            'state': block.state,
            'zip': block.zip,
            'block': block,
            'intersection_id': None,
            'point': geocoded_pt,
            'url': block.url(),
            'wkt': str(block.location),
        })


class BlockGeocoder(AddressGeocoder):
    """
    Geocodes the location_string as a streets.Block.
    """

    def _do_geocode(self, location_string):
        m = block_re.search(location_string)
        if not m:
            raise ParsingError("BlockGeocoder somehow got an address it can't parse: %r" % location_string)
        new_location_string = ' '.join(m.groups())
        return AddressGeocoder._do_geocode(self, new_location_string)


class IntersectionGeocoder(Geocoder):
    """
    Geocodes the location_string as a streets.Intersection.
    """

    def _do_geocode(self, location_string):
        sides = intersection_re.split(location_string)
        if len(sides) != 2:
            raise ParsingError("Couldn't parse intersection: %r" % location_string)

        # Parse each side of the intersection to a list of possibilities.
        # Let the ParseError exception propagate, if it's raised.
        left_side = parse(sides[0])
        right_side = parse(sides[1])

        all_results = []
        seen_intersections = set()
        # Defer to avoid cyclical import.
        from ebpub.streets.models import StreetMisspelling
        for street_a in left_side:
            street_a['street'] = StreetMisspelling.objects.make_correction(street_a['street'])
            for street_b in right_side:
                street_b['street'] = StreetMisspelling.objects.make_correction(street_b['street'])
                for result in self._db_lookup(street_a, street_b):
                    if result["intersection_id"] not in seen_intersections:
                        seen_intersections.add(result["intersection_id"])
                        all_results.append(result)

        if not all_results:
            raise DoesNotExist("Geocoder db couldn't find this intersection: %r" % location_string)
        elif len(all_results) == 1:
            return all_results.pop()
        else:
            raise AmbiguousResult(list(all_results), "Intersections DB returned %s results" % len(all_results))


    def _db_lookup(self, street_a, street_b):
        # Avoid circular import.
        from ebpub.streets.models import Intersection
        try:
            intersections = Intersection.objects.search(
                predir_a=street_a["pre_dir"],
                street_a=street_a["street"],
                suffix_a=street_a["suffix"],
                postdir_a=street_a["post_dir"],
                predir_b=street_b["pre_dir"],
                street_b=street_b["street"],
                suffix_b=street_b["suffix"],
                postdir_b=street_b["post_dir"]
            )
        except Exception, e:
            raise DoesNotExist("Intersection db query failed: %r" % e)
        return [self._build_result(i) for i in intersections]


    def _build_result(self, intersection):
        return Address({
            'address': intersection.pretty_name,
            'city': intersection.city,
            'state': intersection.state,
            'zip': intersection.zip,
            'intersection_id': intersection.id,
            'intersection': intersection,
            'block': None,
            'point': intersection.location,
            'url': intersection.url(),
            'wkt': str(intersection.location),
        })

# THIS IS NOT YET FINISHED
#
# class SegmentGeocoder(Geocoder):
#     def _do_geocode(self, location_string):
#         bits = segment_re.findall(location_string)
#         g = IntersectionGeocoder()
#         try:
#             point1 = g.geocode('%s and %s' % (bits[0], bits[1]))
#             point2 = g.geocode('%s and %s' % (bits[0], bits[2]))
#         except DoesNotExist, e:
#             raise DoesNotExist("Segment query failed: %r" % e)
#         # TODO: Make a line from the two points, and return that.


class SmartGeocoder(Geocoder):
    """
    Checks whether the location_string looks like an Intersection, Block,
    or Address, and delegates to the appropriate Geocoder subclass.
    """
    def _do_geocode(self, location_string):
        if intersection_re.search(location_string):
            logger.debug('%r looks like an intersection' % location_string)
            geocoder = IntersectionGeocoder()
        elif block_re.search(location_string):
            logger.debug('%r looks like a block' % location_string)
            geocoder = BlockGeocoder()
        else:
            logger.debug('%r assumed to be an address' % location_string)
            geocoder = AddressGeocoder()
        return geocoder._do_geocode(location_string)


def full_geocode(query, search_places=True, convert_to_block=True, guess=False,
                 **disambiguation_kwargs):
    """
    Tries the full geocoding stack on the given query (a string):

    * Normalizes whitespace/capitalization
    * Searches the Misspelling table to corrects location misspellings
    * Searches the Location table
    * Failing that, searches the Place table (if search_places is True)
    * Failing that, uses the SmartGeocoder to parse this as an address, block,
      or intersection
    * Failing that, raises whichever error is raised by the geocoder --
      except AmbiguousResult, in which case all possible results are
      returned

    Returns a dictionary of {type, result, ambiguous}, where ambiguous is True
    or False, and type can be on of these strings:

    * 'location' -- in which case result is a Location object.
    * 'place' -- in which case result is a Place object. (This is only
      possible if search_places is True.)
    * 'address' -- in which case result is an Address object as returned
      by geocoder.geocode().
    * 'block' -- in which case result is an Address object based on the block.

    When ``ambiguous`` is True in the output dict, ``result`` will be
    a list of objects, and vice versa.

    You can control behavior with ambiguous results in several ways:

    * By passing guess=True, only the first result will be returned.

    * By passing additional kwargs such as ``zipcode``, ``city``, or
      ``state``, they will be used to attempt to disambiguate address
      or block results as needed.  Keys should be keys in each Address result;
      invalid keys have no effect.

    * By default, *if* the exact address is not matched, it will be
      rounded down to the nearest 100, eg.  '123 Main St' will be
      converted to '100 block of Main St', and tried again with
      BlockGeocoder.  This is enabled by default; you can pass
      ``convert_to_block=False`` to turn it off.

    """
    # Local import to avoid circular imports.
    from ebpub.db.models import Location, LocationSynonym
    from ebpub.streets.models import Place, PlaceSynonym

    # Search the Location table.
    canonical_loc = LocationSynonym.objects.get_canonical(query)
    locs = Location.objects.filter(normalized_name=canonical_loc)
    if len(locs) == 1:
        logger.debug(u'geocoded %r to Location %s' % (query, locs[0]))
        return {'type': 'location', 'result': locs[0], 'ambiguous': False}
    elif len(locs) > 1:
        logger.debug(u'geocoded %r to multiple Locations: %s' % (query, unicode(locs)))
        return {'type': 'location', 'result': locs, 'ambiguous': True}

# The current Place search implementation is error-prone & should be revisited.
#    # Search the Place table, for stuff like "Sears Tower".
#    if search_places:
#        canonical_place = PlaceSynonym.objects.get_canonical(query)
#        places = Place.objects.filter(normalized_name=canonical_place)
#        if len(places) == 1:
#            logger.debug(u'geocoded %r to Place %s' % (query, places[0]))
#            return {'type': 'place', 'result': places[0], 'ambiguous': False}
#        elif len(places) > 1:
#            # TODO: Places don't know about city, state, zip...
#            # so we can't disambiguate.
#            logger.debug(u'geocoded %r to multiple Places: %s' % (query, unicode(places)))
#            return {'type': 'place', 'result': places, 'ambiguous': True}

    # Try geocoding this as an address.
    geocoder = SmartGeocoder(use_cache=getattr(settings, 'EBPUB_CACHE_GEOCODER', False))
    try:
        result = geocoder.geocode(query)
    except AmbiguousResult, e:
        logger.debug('Multiple addresses for %r' % query)
        # The disambiguation args (zipcode, city, state,...)
        # are not included in the initial pass because it
        # is often too picky, yielding no results when there is a
        # legitimate nearby zipcode or city identified in either the address
        # or street number data.
        results = disambiguate(e.choices, guess=guess, **disambiguation_kwargs)
        if not results:
            logger.debug("Disambiguate returned nothing, should not happen")
            results = e.choices
        if len(results) > 1:
            return {'type': 'address', 'result': results, 'ambiguous': True}
        else:
            return {'type': 'address', 'result': results[0], 'ambiguous': False}

    except InvalidBlockButValidStreet, e:
        result = {
            'type': 'block',
            'ambiguous': True,
            'result': e.block_list,
            'street_name': e.street_name,
            'block_number': e.block_number,
            }
        if convert_to_block:
            # If the exact address couldn't be geocoded, try using the
            # normalized block name.
            block_name = address_to_block(query)
            if block_name != query:
                try:
                    result['result'] = BlockGeocoder()._do_geocode(block_name)
                    result['result']['address'] = block_name
                    result['ambiguous'] = False
                    logger.debug('Resolved %r to block %r' % (query, block_name))
                except (InvalidBlockButValidStreet, AmbiguousResult):
                    pass
        if result['ambiguous']:
            logger.debug('Invalid block for %r, returning all possible blocks' % query)
        return result

    except:
        raise

    logger.debug('SmartGeocoder for %r returned %s' % (query, result))
    return {'type': 'address', 'result': result, 'ambiguous': False}


def disambiguate(geocoder_results, guess=False, **kwargs):
    """Disambiguate a list of geocoder results based on city, state, zip.
    Result will be a list, which may be the original list or a subset of it.

    If guess==True, returns the first remaining result, which could be
    wildly off from what you expect (eg. in the case of 'invalid block
    but valid street')... so use with caution.
    """
    if not kwargs:
        if guess and geocoder_results:
            logger.debug("Nothing to disambiguate on, returning first result.")
            return [geocoder_results[0]]
        else:
            logger.debug("Nothing to disambiguate or guess, returning all.")
            return geocoder_results

    filtered_results = geocoder_results[:]

    for key, target_val in kwargs.items():
        # special case: allow passing either form
        if key == 'zipcode':
            key = 'zip'

        if not target_val:
            continue

        target_val = target_val.lower()
        previous_results = filtered_results[:]
        filtered_results = [r for r in previous_results
                            if r.get(key, '').lower() == target_val]
        if not filtered_results:
            # Oops, too restrictive. Ignore this disambiguator and
            # try again with any remaining  disambiguators.
            logger.debug(
                "Ambiguous results, but none are in %s %r" % (key, target_val))
            filtered_results = previous_results
            continue

        if len(filtered_results) > 1:
            # Still too many.
            logger.debug(
                "Still ambiguous results in %s %r..." % (key, target_val))
            continue

        elif len(filtered_results) == 1:
            # Yay, successfully disambiguated.
            return filtered_results

    if len(filtered_results) > 1 and guess:
        logger.debug("Still ambiguous results, guessing first.")
        return [filtered_results[0]]

    return filtered_results
