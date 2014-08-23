# encoding: utf-8

import os
import re
import math
import copy
import urlparse
import functools
from collections import namedtuple
import pytz
from datetime import datetime
import utils
from utils.api import GraphAPI
from utils.functional import immutable, memoize


class Selection(object):
    def __init__(self, edge):
        self.edge = edge
        self.graph = edge.graph
        self.meta = {
            'since': utils.date.COMMON_ERA, 
            'until': datetime.now(), 
        }
        self.params = {
            'page': False, 
        }

    def clone(self):
        selection = self.__class__(self.edge)
        selection.meta = copy.copy(self.meta)
        selection.params = copy.copy(self.params)
        return selection

    @immutable
    def range(self, since, until=None):
        if not until:
            until = datetime.today().isoformat()

        self.meta['since'] = utils.date.parse_utc(since)
        self.meta['until'] = utils.date.parse_utc(until)
        self.params['page'] = True
        self.params['since'] = utils.date.timestamp(since)
        self.params['until'] = utils.date.timestamp(until)
        return self

    @immutable
    def since(self, date):
        return self.range(date)

    def __getitem__(self, key):
        return self.get()[key]

    def __iter__(self):
        if not hasattr(self, '_results'):
            self._results = self.get()
        return self._results.__iter__()


class PostSelection(Selection):
    @immutable
    def latest(self, n=1):
        self.params['limit'] = n
        return self

    def find(self, q):
        return self.graph.find(q, 'post')

    def get(self):
        pages = self.graph.get('posts', **self.params)
        if not self.params['page']:
            pages = [pages]

        posts = []
        for page in pages:
            for post in page['data']:
                post = Post(self.edge, post)

                # For date ranges, we can't rely on pagination 
                # because `since` and `until` parameters serve 
                # both as paginators and as range delimiters, 
                # so there will always be a next page.
                if post.created_time >= self.meta['since']:
                    posts.append(post)
                else:
                    return posts

        return posts


class InsightsSelection(Selection):
    @immutable
    def daily(self, metrics=None):
        self.params['period'] = 'day'
        return self._metrics(metrics)

    @immutable
    def weekly(self, metrics=None):
        self.params['period'] = 'week'
        return self._metrics(metrics)

    @immutable
    def monthly(self, metrics=None):
        self.params['period'] = 'days_28'
        return self._metrics(metrics)

    @immutable
    def lifetime(self, metrics=None):
        self.params['period'] = 'lifetime'
        return self._metrics(metrics)

    def _metrics(self, ids=None):
        if ids:
            if isinstance(ids, list):
                self.meta['single'] = False
            else:
                self.meta['single'] = True
                ids = [ids]
            self.meta['metrics'] = ids
        return self

    @property
    def _has_daterange(self):
        return 'since' in self.params or 'until' in self.params

    def get(self):
        # by default, Facebook returns three days 
        # worth' of insights data
        if self._has_daterange:
            seconds = (self.meta['until'] - self.meta['since']).total_seconds()
            days = math.ceil(seconds / 60 / 60 / 24)
        else:
            days = 3

        # TODO: for large date ranges, chunk them up and request
        # the subranges in a batch request (this is a little bit
        # more complex because multiple metrics are *also* batched
        # and so these two things have to work together)
        # 
        # TODO: investigate whether this applies too when asking for 
        # weekly or monthly metrics (that is, whether the limit is 93
        # result rows or truly 93 days)
        if days > 31 * 3:
            raise NotImplementedError(
                "Can only fetch date ranges smaller than 3 months.")

        if 'metrics' in self.meta:
            metrics = []
            for metric in self.meta['metrics']:
                metrics.append({'relative_url': metric})
            results = self.graph.all('insights', 
                metrics, **self.params)
        else:
            results = [self.graph.get('insights', 
                **self.params)]

        datasets = []
        for result in results:
            datasets.extend(result['data'])

        fields = ['end_time']
        data = {}
        for dataset in datasets:
            metric = dataset['name']
            rows = dataset['values']
            fields.append(metric)

            for row in rows:
                value = row['value']
                end_time = row.get('end_time')
                if end_time:
                    end_time = utils.date.parse(end_time)
                else:
                    end_time = 'lifetime'

                data.setdefault(end_time, {})[metric] = value

        Row = namedtuple('Row', fields)

        rows = []
        for time, values in data.items():
            rows.append(Row(end_time=time, **values))

        # when a single metric is requested (and not 
        # wrapped in a list), we return a simplified 
        # data format
        if self.meta['single']:
            metric = self.meta['metrics'][0]
            return [getattr(row, metric) for row in rows]
        else:
            return rows

    def serialize(self):
        return [row._asdict() for row in self.get()]

    def __repr__(self):
        if 'metrics' in self.meta:
            metrics = ", ".join(self.meta['metrics'])
        else:
            metrics = 'all available metrics'

        if self._has_daterange:
            date = ' from {} to {}'.format(
                self.meta['since'].date().isoformat(), 
                self.meta['until'].date().isoformat(), 
                )
        else:
            date = ''

        return u"<Insights for '{}' ({}{})>".format(
            repr(self.edge.name), metrics, date)
        


class Picture(object):
    def __init__(self, post, raw):
        self.post = post
        self.graph = post.graph
        self.raw = self.url = raw
        self.parsed_url = urlparse.urlparse(self.raw)
        self.qs = urlparse.parse_qs(self.parsed_url.query)
        
        if 'url' in self.qs:
            self.origin = self.qs['url'][0]
            self.width = self.qs['w'][0]
            self.height = self.qs['h'][0]
        else:
            self.origin = self.url

        self.basename = self.origin.split('/')[-1]

    def __repr__(self):
        return u"<Picture: {} ({}x{})>".format(
            self.basename, self.width, self.height)


class Post(object):
    def __init__(self, account, raw):
        self.account = account
        self.raw = raw
        # most fields aside from id, type, ctime 
        # and mtime are optional
        self.graph = account.graph.partial(raw['id'])
        self.id = raw['id']
        self.type = raw['type']
        self.created_time = utils.date.parse(raw['created_time'])
        self.updated_time = utils.date.parse(raw['updated_time'])
        self.name = raw.get('name')
        self.story = raw.get('story')
        self.link = raw.get('link')
        self.description = raw.get('description')
        self.shares = raw.get('shares')
        # TODO: figure out if *all* comments and likes are included 
        # when getting post data, or just some
        self.comments = utils.api.getdata(raw, 'comments')
        self.likes = utils.api.getdata(raw, 'likes')
        self.quotes = utils.extract_quotes(self.description or '')
        if 'picture' in raw:
            self.picture = Picture(self, raw['picture'])
        else:
            self.picture = None

    @property
    def insights(self):
        return InsightsSelection(self)

    def resolve_link(self, clean=False):
        if not self.link:
            return None

        url = utils.url.resolve(self.link)

        if clean:
            url = utils.url.base(url)

        return url

    def __repr__(self):
        time = self.created_time.date().isoformat()
        return u"<Post: {} ({})>".format(self.id, time)


class Page(object):
    def __init__(self, token):
        self.graph = GraphAPI(token).partial('me')
        data = self.graph.get()
        self.raw = data
        self.id = data['id']
        self.name = data['name']

    @property
    def token(self):
        return self.graph.oauth_token

    @property
    def insights(self):
        return InsightsSelection(self)

    @property
    def posts(self):
        return PostSelection(self)

    def __repr__(self):
        return u"<Page {}: {}>".format(self.id, self.name)
