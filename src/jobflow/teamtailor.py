"""Read the public jobs widget embedded on an employer's careers page.

The widget key is obtained from public HTML on every run. It is never persisted,
logged, or used for candidate/account/private relationship endpoints.
"""
import json
from html.parser import HTMLParser
from urllib.parse import urlparse

from .models import JobInput, Compensation
from .providers import public_http_url, prohibited_automation_url, strip_html, _country_code_from_location, normalize_employment_type
from .public_boards import PublicBoard


class Widgets(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.keys = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'teamtailor-jobs-widget' in attrs.get('class', '').split():
            key = attrs.get('data-teamtailor-api-key')
            if key:
                self.keys.append(key)


class TeamtailorProvider(PublicBoard):
    name = 'teamtailor'
    max_pages = 100

    def __init__(self, boards, listing_urls, api_roots, client=None, prefilter=False):
        super().__init__(boards, client=client, prefilter=prefilter)
        self.listing_urls, self.api_roots = listing_urls, api_roots

    def title(self, item):
        return item['attributes']['title']

    def index(self, token, result):
        company = result['company']
        url, api = self.listing_urls[company], self.api_roots[company]
        if not public_http_url(url) or prohibited_automation_url(url):
            raise ValueError('Invalid employer widget page')
        if api not in ('https://api.teamtailor.com/v1/jobs', 'https://api.na.teamtailor.com/v1/jobs'):
            raise ValueError('Unverified Teamtailor public jobs endpoint')
        r = self.client.get(url)
        r.raise_for_status()
        keys = set(Widgets(r.text).keys)
        if len(keys) != 1:
            raise ValueError('Missing or ambiguous public jobs widget')
        key = keys.pop()
        seen, expected = set(), None
        for page in range(1, self.max_pages + 1):
            r = self.client.get(api, params={'api_key': key, 'api_version': '20161108',
                'filter[feed]': 'public', 'include': 'locations', 'page[size]': 30, 'page[number]': page})
            r.raise_for_status()
            payload = r.json()
            items, meta = payload.get('data'), payload.get('meta', {})
            total, pages = meta.get('record-count'), meta.get('page-count')
            if not isinstance(items, list) or type(total) is not int or type(pages) is not int or total < 0 or pages < 0:
                raise ValueError('Malformed Teamtailor listing totals')
            if expected is not None and expected != total:
                raise ValueError('Teamtailor inventory changed during pagination')
            expected = total
            locations = {i['id']: i['attributes'] for i in payload.get('included', []) if i.get('type') == 'locations'}
            fresh = 0
            for item in items:
                identifier = str(item['id'])
                if item.get('type') != 'jobs' or not identifier.isdigit():
                    raise ValueError('Malformed Teamtailor posting')
                if identifier in seen:
                    continue
                seen.add(identifier)
                fresh += 1
                result['raw_count'] += 1
                attrs = item['attributes']
                if attrs.get('internal') is not False or attrs.get('status') != 'open' or attrs.get('human-status') != 'published':
                    raise ValueError('Teamtailor feed includes non-public posting')
                refs = item.get('relationships', {}).get('locations', {}).get('data', [])
                # Missing location relationships must not silently become global remote.
                places = [locations[ref['id']] for ref in refs]
                yield {'id': identifier, 'attributes': attrs,
                       'url': item['links']['careersite-job-url'], 'locations': places}
            if page >= pages:
                if len(seen) != total:
                    raise ValueError('Teamtailor listing count incomplete')
                return
            if not fresh:
                raise ValueError('Teamtailor pagination stalled')
        raise ValueError('Teamtailor page limit reached')

    def detail(self, company, token, item):
        dest = urlparse(item['url'])
        if dest.scheme != 'https' or dest.hostname not in (token + '.teamtailor.com', token + '.na.teamtailor.com') or not dest.path.startswith('/jobs/' + item['id'] + '-'):
            raise ValueError('Teamtailor employer/posting identity mismatch')
        attrs = item['attributes']
        if not attrs.get('title') or not attrs.get('body'):
            raise ValueError('Missing Teamtailor job description')
        countries = {_country_code_from_location(loc.get('country', ''), '') for loc in item['locations']}
        country = next(iter(countries)) if len(countries) == 1 else ''
        # A remote label may still restrict the worker to Toronto; retain it.
        location = '; '.join(dict.fromkeys((loc.get('name') or loc.get('city') or '') + ', ' + loc.get('country', '') for loc in item['locations']))
        unit = {'yearly': 'year', 'monthly': 'month', 'hourly': 'hour'}.get(attrs.get('salary-time-unit'))
        pay = Compensation()
        if unit and attrs.get('min-salary') is not None and attrs.get('max-salary') is not None:
            pay = Compensation(float(attrs['min-salary']), float(attrs['max-salary']), attrs.get('currency', ''), unit)
        return JobInput(title=attrs['title'], company=company, source=self.name,
            external_id=token + ':' + item['id'], url=item['url'], canonical_url=item['url'],
            location=location, country=country, description=strip_html(attrs['body']),
            workplace_type={'fully': 'remote', 'hybrid': 'hybrid', 'none': 'on-site', 'onsite': 'on-site'}.get(attrs.get('remote-status'), 'unspecified'),
            employment_type=normalize_employment_type(attrs.get('employment-type', ''), default='unspecified'),
            posted_at=attrs.get('created-at') or '', closes_at=attrs.get('end-date') or '',
            compensation=pay, raw_payload=json.dumps(item))
