"""Public Taleo Business Edition v2 listings; never candidate/login endpoints."""
import json
import re
from dataclasses import replace
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from .models import JobInput
from .providers import (DiscoveryProvider, strip_html, _job_posting, _location_text,
                        _country_code_from_location, normalize_employment_type, infer_compensation)
from .public_boards import error_text
from .titles import relevant_title


class Listing(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links, self.next, self.text, self.active = [], '', [], None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            if 'viewJobLink' in attrs.get('class', '').split():
                self.active = [attrs.get('href', ''), '']
            if 'jscroll-next' in attrs.get('class', '').split():
                self.next = attrs.get('href', '')

    def handle_data(self, text):
        self.text.append(text)
        if self.active is not None:
            self.active[1] += text

    def handle_endtag(self, tag):
        if tag == 'a' and self.active is not None:
            self.links.append(tuple(self.active))
            self.active = None


class TaleoPublicProvider(DiscoveryProvider):
    name = 'taleo_public'

    def __init__(self, boards, client=None, prefilter=False, max_pages=100):
        super().__init__(client)
        self.boards, self.prefilter, self.max_pages = boards, prefilter, max_pages
        self.board_results = []

    @staticmethod
    def identity(url):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.scheme != 'https' or not (parts.hostname or '').endswith('.tbe.taleo.net'):
            raise ValueError('Expected public Taleo Business Edition origin')
        if not all(len(query.get(key, [])) == 1 for key in ('org', 'cws')):
            raise ValueError('Missing Taleo employer/site identity')
        return parts, query['org'][0], query['cws'][0]

    def fetch(self):
        self.board_results = []
        for company, root in self.boards.items():
            result = dict(company=company, board_token=root, state='completed', raw_count=0,
                          index_count=0, emitted_count=0, pages=0, total=None, error='')
            errors = []
            try:
                base, org, site = self.identity(root)
                if not base.path.endswith('/ats/careers/v2/searchResults'):
                    raise ValueError('Expected public Taleo v2 search page')
                url, seen_pages, postings, expected = root, set(), {}, None
                for page in range(self.max_pages):
                    if url in seen_pages:
                        raise ValueError('Repeated Taleo search page')
                    seen_pages.add(url)
                    response = self.client.get(url)
                    response.raise_for_status()
                    listing = Listing(response.text)
                    total = re.search(r'Positions\s+Matched\s+(\d+)', ' '.join(listing.text), re.I)
                    if page == 0 and not total:
                        raise ValueError('Taleo total missing; cannot confirm inventory')
                    if total:
                        count = int(total[1])
                        if expected is not None and expected != count:
                            raise ValueError('Taleo inventory changed during pagination')
                        expected = count
                    before = len(postings)
                    for href, title in listing.links:
                        destination = urljoin(root, href)
                        parts, job_org, job_site = self.identity(destination)
                        rid = parse_qs(parts.query).get('rid', [''])[0]
                        if (parts.netloc, job_org, job_site) != (base.netloc, org, site) or not rid.isdigit() or not parts.path.endswith('/viewRequisition'):
                            raise ValueError('Taleo posting identity mismatch')
                        postings[rid] = (destination, title.strip())
                    result.update(raw_count=len(postings), pages=page + 1, total=expected)
                    if len(postings) > expected:
                        raise ValueError('Taleo index exceeds declared total')
                    if not listing.next:
                        if len(postings) != expected:
                            raise ValueError('Taleo index ended before declared total')
                        break
                    if len(postings) == before:
                        raise ValueError('Taleo pagination stalled')
                    nxt = urlsplit(urljoin(root, listing.next))
                    offset = parse_qs(nxt.query).get('rowFrom', [''])[0]
                    if nxt.netloc != base.netloc or nxt.path != base.path or not offset.isdigit():
                        raise ValueError('Unverified Taleo next-page destination')
                    # Preserve employer identity even when session-based next
                    # links omit it. Ignore tracking timestamps, never follow login.
                    url = urlunsplit((base.scheme, base.netloc, base.path,
                                     urlencode({'org': org, 'cws': site, 'next': '', 'rowFrom': offset}), ''))
                else:
                    raise ValueError('Taleo page limit reached')
                for rid, (url, title) in postings.items():
                    if self.prefilter and not relevant_title(title):
                        continue
                    result['index_count'] += 1
                    try:
                        response = self.client.get(url)
                        response.raise_for_status()
                        posting = _job_posting(response.text)
                        if not posting or not posting.get('description') or posting.get('title') != title:
                            raise ValueError('Taleo job detail missing or mismatched')
                        detail_parts, detail_org, detail_site = self.identity(posting.get('url', ''))
                        if (detail_parts.netloc, detail_org, detail_site, parse_qs(detail_parts.query).get('rid')) != (base.netloc, org, site, [rid]):
                            raise ValueError('Taleo detail identity mismatch')
                        locations = posting.get('jobLocation') or []
                        if isinstance(locations, dict):
                            locations = [locations]
                        countries = set()
                        clean_locations = []
                        for loc in locations:
                            address = dict(loc.get('address') or {})
                            country_value = address.get('addressCountry') or ''
                            if isinstance(country_value, dict):
                                country_value = country_value.get('name') or ''
                            countries.add(country_value)
                            address['addressCountry'] = country_value
                            clean_locations.append({'address': address})
                        location = '; '.join(_location_text(loc) for loc in clean_locations)
                        country = next(iter(countries)) if len(countries) == 1 and countries <= {'CA', 'US'} else _country_code_from_location(location)
                        description = strip_html(posting['description'])
                        job = JobInput(title=title, company=company, source=self.name,
                            external_id=org + ':' + site + ':' + rid, url=url, canonical_url=url,
                            location=location, country=country, description=description,
                            employment_type=normalize_employment_type(posting.get('employmentType') or '', default='unspecified'),
                            posted_at=posting.get('datePosted') or '', closes_at=posting.get('validThrough') or '',
                            compensation=infer_compensation(description, 'CAD' if country == 'CA' else 'USD'),
                            raw_payload=json.dumps(posting))
                        from .eligibility import _workplace
                        result['emitted_count'] += 1
                        yield replace(job, workplace_type='remote' if posting.get('jobLocationType') == 'TELECOMMUTE' else _workplace(job))
                    except Exception as exc:
                        errors.append('detail: ' + error_text(exc))
                        result.setdefault('detail_failures', []).append({
                            'requisition': rid,
                            'reason': str(exc) if isinstance(exc, ValueError) else error_text(exc),
                        })
                        if hasattr(exc, 'response') and exc.response.status_code in (401, 403, 429):
                            break
            except Exception as exc:
                errors.append('index: ' + error_text(exc))
            if errors:
                result.update(state='partial' if result['raw_count'] else 'failed', error='; '.join(sorted(set(errors))))
            self.board_results.append(result)
