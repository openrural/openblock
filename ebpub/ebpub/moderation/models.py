#   Copyright 2011 OpenPlans, and contributors
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
Users can flag NewsItems or comments as spam, offensive, or 'other'.
"""

from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from ebpub.db.models import NewsItem

class Flag(models.Model):
    class Meta:
        abstract = True
        ordering = ('news_item',)

    news_item = models.ForeignKey(NewsItem, null=True, blank=True,
                                  related_name="%(app_label)s_%(class)s_related")

    reason = models.CharField(max_length=128, db_index=True,
                              choices=(('spam', u'Spam'),
                                       ('inappropriate', u'Inappropriate'),
                                       ('other', u'other (please explain below)')),
                              help_text=u'Why are you flagging this item?')

    email = models.CharField(max_length=128, null=True, blank=True,
                             default='anonymous')

    comment = models.CharField(max_length=512)

    submitted = models.DateTimeField(auto_now_add=True)

    updated = models.DateTimeField(auto_now=True)

    state = models.CharField(max_length=64, db_index=True,
                             choices=(('new', u'New'),
                                      ('approved', u'Item Approved'),
                                      ('deleted', u'Item Deleted')),
                             blank=True,
                             default='new',
                             )



    def clean(self):
        if self.news_item_id is None and self.state != 'deleted':
            raise ValidationError(u'A NewsItem must be specified if state is not Deleted')

    # Extra stuff for admin convenience.
    @property
    def item_title(self):
        return self.news_item.title

    @property
    def item_schema(self):
        return self.news_item.schema.slug

    @property
    def item_description(self):
        return self.news_item.description

    @property
    def item_url(self):
        return self.news_item.url

    @property
    def item_pub_date(self):
        return self.news_item.pub_date


class NewsItemFlag(Flag):
    """
    Flags on NewsItems.
    """

class CommentFlag(Flag):
    """
    Flags on Comments.
    """

