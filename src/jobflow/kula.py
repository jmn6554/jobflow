"""Public Kula career-page discovery; no applicant or private API requests."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .models import JobInput
from .public_boards import PublicBoard
from .providers import strip_html, infer_compensation, normalize_employment_type


class CareerHTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.scripts, self.links = [], []
        self.current = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.current = []
        elif tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.scripts.append(''.join(self.current))
            self.current = None


def flight_records(scripts):
    """Decode JSON wrappers and UTF-8-length-prefixed text, never execute JS.

    React may split a text record across scripts. Its length is bytes, not
    Python characters (important for French text and emoji).
    """
    chunks = []
    for script in scripts:
        match = re.fullmatch(r'\s*self\.__next_f\.push\((.*)\)\s*;?\s*', script, re.S)
        if match:
            payload = json.loads(match[1])
            if len(payload) == 2 and payload[0] == 1 and isinstance(payload[1], str):
                chunks.append(payload[1])
    data = ''.join(chunks).encode('utf-8')
    records = {}
    while data:
        header = re.match(rb'([0-9a-f]*):', data)
        if not header:
            raise ValueError('Unsupported Kula flight record')
        key = header[1].decode()
        data = data[header.end():]
        text_header = re.match(rb'T([0-9a-f]+),', data)
        if text_header:
            length = int(text_header[1], 16)
            start = text_header.end()
            if len(data) < start + length:
                raise ValueError('Truncated Kula text record')
            value = data[start:start + length].decode('utf-8')
            data = data[start + length:]
        else:
            line, separator, data = data.partition(b'\n')
            if not separator:
                raise ValueError('Truncated Kula record')
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                # Module/import/hint records are not job data.
                continue
        if key in records and records[key] != value:
            raise ValueError('Conflicting Kula records')
        records[key] = value
    return records


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


class KulaProvider(PublicBoard):
    name = 'kula'

    def title(self, item):
        return item['title']

    def index(self, token, result):
        base = 'https://careers.kula.ai/' + token
        response = self.client.get(base, params={'jobs': 'true'})
        response.raise_for_status()
        page = CareerHTML(response.text)
        records = flight_records(page.scripts)
        props = [obj for value in records.values() for obj in objects(value)]
        if not any(obj.get('accountName') == token for obj in props):
            raise ValueError('Kula employer identity unverified')
        lists = [obj['jobs'] for obj in props if isinstance(obj.get('jobs'), list)]
        if len(lists) != 1:
            raise ValueError('Missing or ambiguous Kula job index')
        routes = {}
        for href in page.links:
            url = urlparse(urljoin(base, href))
            match = re.fullmatch('/' + re.escape(token) + r'/(\d+)-[^/]+', url.path)
            if url.scheme == 'https' and url.hostname == 'careers.kula.ai' and match:
                routes[match[1]] = 'https://careers.kula.ai' + url.path
        seen = set()
        for item in lists[0]:
            key = str(item['id'])
            if not key.isdigit() or not item.get('title'):
                raise ValueError('Malformed Kula job')
            if not item.get('listed') or item.get('is_confidential') or item.get('kind') not in ('external', 'internal_and_external'):
                raise ValueError('Kula index contains non-public posting')
            if key in seen:
                continue
            seen.add(key)
            result['raw_count'] += 1
            if key not in routes:
                raise ValueError('Kula posting destination missing')
            description = item['ats_job']['job_description']
            if isinstance(description, str) and re.fullmatch(r'\$[0-9a-f]+', description):
                description = records.get(description[1:])
            yield {**item, '_url': routes[key], '_description': description}
        if set(routes) != seen:
            raise ValueError('Kula index and visible job links differ')

    def detail(self, company, token, item):
        description = item['_description']
        if not isinstance(description, str) or not description.strip() or description.startswith('$'):
            raise ValueError('Kula description unresolved')
        job = item['ats_job']
        offices = job.get('offices') or []
        countries = {o['country_code'] for o in offices if o.get('country_code')}
        country = next(iter(countries)) if len(countries) == 1 else ''
        text = strip_html(description)
        return JobInput(title=item['title'], company=company,
                        location='; '.join(o.get('location') or o.get('name', '') for o in offices),
                        url=item['_url'], canonical_url=item['_url'], source=self.name,
                        external_id=str(item['id']), description=text, country=country,
                        workplace_type={'remote': 'remote', 'hybrid': 'hybrid', 'on_site': 'on-site'}.get(job.get('workplace'), 'unspecified'),
                        employment_type=normalize_employment_type(job.get('employment_type', '').replace('_', '-')),
                        posted_at=item.get('launch_at') or '',
                        compensation=infer_compensation(text, 'CAD' if country == 'CA' else 'USD'),
                        raw_payload=json.dumps(item))
