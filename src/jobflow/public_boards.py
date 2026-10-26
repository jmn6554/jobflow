"""Read-only public BambooHR and Rippling career-board adapters.

Only public posting data is consumed. Application questionnaire metadata is
retained for inspection; these adapters never send candidate answers or files.
"""
import json
import re
from html.parser import HTMLParser

from .models import JobInput
from .providers import (DiscoveryProvider, strip_html, _country_code_from_location,
                        infer_compensation, normalize_employment_type)
from .titles import relevant_title


class NextData(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.active = False
        self.parts = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__':
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, text):
        if self.active:
            self.parts.append(text)

    def page_props(self):
        return json.loads(''.join(self.parts))['props']['pageProps']


def safe_token(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
        raise ValueError('Invalid public career board token')
    return token


def read_json(client, url):
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def read_props(client, url, params=None):
    response = client.get(url, params=params, follow_redirects=True)
    response.raise_for_status()
    return NextData(response.text).page_props()


def error_text(exc):
    return 'HTTP {}'.format(exc.response.status_code) if hasattr(exc, 'response') else type(exc).__name__


class PublicBoard(DiscoveryProvider):
    def __init__(self, boards, client=None, prefilter=False):
        super().__init__(client)
        self.boards = boards
        self.prefilter = prefilter
        self.board_results = []

    def fetch(self):
        self.board_results = []
        for company, token in self.boards.items():
            result = dict(company=company, board_token=token, state='completed',
                          raw_count=0, index_count=0, emitted_count=0, error='')
            errors = []
            try:
                safe_token(token)
                for item in self.index(token, result):
                    if self.prefilter and not relevant_title(self.title(item)):
                        continue
                    result['index_count'] += 1
                    try:
                        job = self.detail(company, token, item)
                        if job is not None:
                            result['emitted_count'] += 1
                            yield job
                    except Exception as exc:
                        errors.append('detail: ' + error_text(exc))
            except Exception as exc:
                errors.append('index: ' + error_text(exc))
            if errors:
                result.update(state='partial' if result['raw_count'] else 'failed',
                              error='; '.join(sorted(set(errors))))
            self.board_results.append(result)


class BambooHRProvider(PublicBoard):
    name = 'bamboohr'

    def title(self, item):
        return item['jobOpeningName']

    def index(self, token, result):
        payload = read_json(self.client, 'https://{}.bamboohr.com/careers/list'.format(token))
        if not isinstance(payload, dict) or not isinstance(payload.get('result'), list):
            raise ValueError('Malformed BambooHR index')
        items = payload['result']
        seen = set()
        for item in items:
            key = str(item['id'])
            if not key.isdigit() or not item.get('jobOpeningName'):
                raise ValueError('Malformed BambooHR posting')
            if key not in seen:
                seen.add(key)
                result['raw_count'] += 1
                yield item
        total = payload.get('meta', {}).get('totalCount')
        if total is None or int(total) != len(seen):
            raise ValueError('BambooHR index completeness unverified')

    def detail(self, company, token, item):
        url = 'https://{}.bamboohr.com/careers/{}'.format(token, item['id'])
        payload = read_json(self.client, url + '/detail')['result']
        job = payload['jobOpening']
        if job.get('jobOpeningStatus') == 'Closed':
            return None
        if job.get('jobOpeningStatus') != 'Open' or not job.get('description'):
            raise ValueError('Unverified or missing BambooHR job detail')
        if job.get('jobOpeningShareUrl', '').rstrip('/') != url:
            raise ValueError('BambooHR posting identity mismatch')
        # Preserve structured workplace values rather than treating company-wide
        # remote benefits in a hybrid job description as remote eligibility.
        workplace = {'0': 'on-site', '1': 'remote', '2': 'hybrid'}.get(str(job.get('locationType')), 'unspecified')
        loc, ats = job.get('location') or {}, job.get('atsLocation') or {}
        location = ', '.join(str(v) for v in [ats.get('city') or loc.get('city'),
                            ats.get('state') or loc.get('state'),
                            ats.get('country') or loc.get('addressCountry')] if v)
        country = _country_code_from_location(location)
        description = strip_html(job['description'])
        pay = job.get('compensation') or ''
        return JobInput(title=job['jobOpeningName'], company=company, location=location,
                        url=url, canonical_url=url, source=self.name, external_id=str(item['id']),
                        description=description, country=country, workplace_type=workplace,
                        employment_type=normalize_employment_type(job.get('employmentStatusLabel', '')),
                        posted_at=job.get('datePosted') or '',
                        compensation=infer_compensation(pay, 'CAD' if country == 'CA' else 'USD'),
                        raw_payload=json.dumps(payload))


class RipplingProvider(PublicBoard):
    name = 'rippling'
    max_pages = 100

    def title(self, item):
        return item['name']

    def index(self, token, result):
        seen = set()
        for page in range(self.max_pages):
            props = read_props(self.client, 'https://ats.rippling.com/{}/jobs'.format(token), {'page': page})
            if props['apiData']['jobBoard']['slug'] != token:
                raise ValueError('Rippling employer mismatch')
            queries = props['dehydratedState']['queries']
            batches = [q['state']['data'] for q in queries
                       if q.get('queryKey', [])[:3] == ['board', token, 'job-posts']]
            if len(batches) != 1:
                raise ValueError('Missing or ambiguous Rippling index')
            batch = batches[0]
            if batch['page'] != page or not isinstance(batch['items'], list):
                raise ValueError('Rippling pagination did not advance')
            fresh = 0
            for item in batch['items']:
                safe_token(item['id'])
                if not item.get('name'):
                    raise ValueError('Missing Rippling title')
                if item['id'] not in seen:
                    seen.add(item['id'])
                    result['raw_count'] += 1
                    fresh += 1
                    yield item
            if page + 1 >= batch['totalPages']:
                if len(seen) != batch['totalItems']:
                    raise ValueError('Rippling listing count incomplete')
                return
            if not fresh:
                raise ValueError('Repeated Rippling page')
        raise ValueError('Rippling page limit reached')

    def detail(self, company, token, item):
        url = 'https://ats.rippling.com/{}/jobs/{}'.format(token, item['id'])
        payload = read_props(self.client, url)['apiData']
        job = payload['jobPost']
        if payload['jobBoard']['slug'] != token or job['uuid'] != item['id']:
            raise ValueError('Rippling posting identity mismatch')
        description = job.get('description')
        if not isinstance(description, dict) or not description.get('role'):
            raise ValueError('Missing Rippling description')
        text = strip_html(' '.join(v for v in description.values() if isinstance(v, str)))
        locations = item.get('locations') or []
        location = '; '.join(str(loc.get('name') or '') for loc in locations)
        countries = {loc.get('countryCode') for loc in locations if loc.get('countryCode')}
        country = next(iter(countries)) if len(countries) == 1 else ''
        types = {loc.get('workplaceType') for loc in locations}
        workplace = {'REMOTE': 'remote', 'HYBRID': 'hybrid', 'ON_SITE': 'on-site'}.get(
            next(iter(types)) if len(types) == 1 else '', 'unspecified')
        employment = job.get('employmentType') or ''
        if isinstance(employment, dict):
            employment = employment.get('id') or employment.get('label') or ''
        employment = normalize_employment_type(employment, default='unknown')
        if re.search(r'\bfull-time\b', employment):
            employment = 'full-time'
        elif re.search(r'\bpart-time\b', employment):
            employment = 'part-time'
        return JobInput(title=job['name'].strip(), company=company, location=location,
                        url=url, canonical_url=url, source=self.name, external_id=item['id'],
                        description=text, country=country, workplace_type=workplace,
                        # Unstated type is not proof of permanent full-time employment.
                        employment_type=employment,
                        compensation=infer_compensation(text, 'CAD' if country == 'CA' else 'USD'),
                        raw_payload=json.dumps({'jobPost': job, 'locations': locations}))
