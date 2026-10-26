"""Read-only Workday public careers discovery, shared by every configured board."""
import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx

from .models import JobInput
from .providers import (DiscoveryProvider, strip_html, _country_code_from_location,
                        _workplace_from_text, normalize_employment_type, infer_compensation)
from .titles import relevant_title, excluded_title


def board_root(url):
    """Normalize a public board/job URL without accepting off-platform hosts."""
    p = urlparse(url)
    if p.scheme != 'https' or p.username or p.password or p.port or not re.fullmatch(
        r'[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com', p.hostname or '', re.I
    ):
        raise ValueError('Expected a public HTTPS Workday careers URL')
    parts = p.path.strip('/').split('/')
    if parts and re.fullmatch(r'[a-z]{2}-[A-Z]{2}', parts[0]):
        parts = parts[1:]
    if not parts or not re.fullmatch(r'[A-Za-z0-9_-]+', parts[0]) or parts[0] in {'job', 'wday'}:
        raise ValueError('Workday careers board name is missing')
    return 'https://' + p.hostname.lower() + '/' + parts[0]


def load_registry(path):
    boards = {}
    if not Path(path).is_file():
        return boards
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            company, root = row.get('company', '').strip(), row.get('careers_url', '').strip()
            if company and root:
                boards[company] = board_root(root)
    return boards


def _descriptor(value):
    return value if isinstance(value, str) else str(value.get('descriptor') or '') if isinstance(value, dict) else ''


def _path(value):
    if not isinstance(value, str) or not value.startswith('/job/'):
        raise ValueError('Invalid Workday job path')
    if any(segment in {'.', '..'} for segment in unquote(value).split('/')) or any(c in value for c in ('?', '#', '\\')):
        raise ValueError('Unsafe Workday job path')
    return value


class WorkdayProvider(DiscoveryProvider):
    name = 'workday'
    queries = ('data', 'analyst', 'analyste', 'analytics', 'intelligence', 'reporting', 'sql', 'etl', 'databricks', 'implementation', 'integration', 'consultant', 'functional', 'implantation', 'onboarding')

    def __init__(self, boards, client=None, max_pages=100, workers=4):
        super().__init__(client)
        self.boards = {company: board_root(root) for company, root in boards.items()}
        self.max_pages = max(1, min(int(max_pages), 1000))
        self.workers = 1 if client is not None else max(1, min(workers, 8))
        self.board_results = []

    @staticmethod
    def _job(company, root, path, info, summary):
        places = [_descriptor(info.get('location')) or str(summary.get('locationsText') or '')]
        places += [_descriptor(p) for p in (info.get('additionalLocations') or [])]
        location = '; '.join(dict.fromkeys(p for p in places if p))
        primary = info.get('jobRequisitionLocation') or {}
        country_data = primary.get('country') or {} if isinstance(primary, dict) else {}
        country = country_data.get('alpha2Code') if isinstance(country_data, dict) else ''
        country = country or _country_code_from_location(_descriptor(info.get('country')), '')
        # A secondary Canadian/US location is a genuine location option, not
        # a query hint. Prefer eligible explicit locations over a foreign primary.
        location_country = _country_code_from_location(location, '')
        if location_country in {'CA', 'US'}:
            country = location_country
        description = strip_html(info.get('jobDescription') or '')
        url = root + path
        metadata_workplace = _descriptor(info.get('remoteType')) or _descriptor(info.get('workplaceType'))
        workplace = _workplace_from_text(metadata_workplace or location)
        tenant = urlparse(root).hostname.split('.')[0]
        # Requisition IDs are only unique within a tenant, not across employers.
        external_id = tenant + ':' + str(info.get('jobReqId') or info.get('id') or path)
        job = JobInput(title=str(info.get('title') or summary.get('title') or ''), company=company,
            location=location, country=str(country or ''), url=url, canonical_url=url,
            description=description, source='workday', external_id=external_id,
            workplace_type=workplace,
            employment_type=normalize_employment_type(_descriptor(info.get('timeType')), default='unspecified'),
            posted_at=str(info.get('startDate') or ''), closes_at=str(info.get('endDate') or ''),
            compensation=infer_compensation(description, 'CAD' if country == 'CA' else 'USD'),
            raw_payload=json.dumps(info, sort_keys=True))
        from .eligibility import _workplace
        return replace(job, workplace_type=_workplace(job))

    def fetch(self):
        self.board_results = []

        def fetch_board(company, root):
            host = urlparse(root).hostname
            api = 'https://' + host + '/wday/cxs/' + host.split('.')[0] + '/' + root.rsplit('/', 1)[1]
            audit = dict(company=company, board_token=root, state='completed', raw_count=0,
                         index_count=0, emitted_count=0, error='', pages=0, queries={}, detail_failures=0)
            found, listed, jobs, errors = {}, set(), [], []
            stopped = False
            for query in self.queries:
                seen_query, offset, expected_total = set(), 0, None
                query_audit = {'pages': 0, 'total': None, 'unique': 0, 'complete': False}
                audit['queries'][query] = query_audit
                try:
                    for _ in range(self.max_pages):
                        r = self.client.post(api + '/jobs', json={'appliedFacets': {}, 'limit': 20,
                                                                  'offset': offset, 'searchText': query})
                        r.raise_for_status()
                        payload = r.json()
                        if not isinstance(payload, dict) or not isinstance(payload.get('jobPostings'), list):
                            raise ValueError('Missing jobPostings list')
                        items, total = payload['jobPostings'], payload.get('total')
                        if not isinstance(total, int) or total < 0:
                            raise ValueError('Missing listing total')
                        # Workday returns the full total on page zero, but often
                        # returns total=0 on subsequent non-empty pages. Do not
                        # mistake that sentinel for the end of the inventory.
                        if expected_total is None:
                            if total == 0 and items:
                                raise ValueError('Initial listing total contradicts items')
                            expected_total = total
                        elif total > 0:
                            expected_total = max(expected_total, total)
                        audit['pages'] += 1
                        query_audit.update(pages=query_audit['pages'] + 1, total=expected_total,
                                           last_response_total=total)
                        fresh = 0
                        for item in items:
                            path = _path(item.get('externalPath'))
                            fresh += int(path not in seen_query)
                            seen_query.add(path)
                            listed.add(path)
                            if relevant_title(str(item.get('title') or '')) and not excluded_title(str(item.get('title') or '')):
                                found[path] = item
                        query_audit['unique'] = len(seen_query)
                        offset += len(items)
                        if offset >= expected_total:
                            if len(seen_query) < expected_total:
                                raise ValueError('Repeated listings prevented complete pagination')
                            query_audit['complete'] = True
                            break
                        if not items or not fresh:
                            raise ValueError('Pagination stalled before reported total')
                    if not query_audit['complete']:
                        errors.append(query + ': page limit reached')
                except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    errors.append(query + ': ' + ('HTTP ' + str(status) if status else type(exc).__name__))
                    if status in {401, 403, 404, 429}:
                        # Do not bypass or repeatedly retry a denied/rate-limited board.
                        stopped = True
                        break
            audit['raw_count'], audit['index_count'] = len(listed), len(found)
            if not stopped:
                for path, summary in found.items():
                    try:
                        r = self.client.get(api + path)
                        r.raise_for_status()
                        payload = r.json()
                        info = payload.get('jobPostingInfo') if isinstance(payload, dict) else None
                        if not isinstance(info, dict):
                            raise ValueError('Missing jobPostingInfo')
                        if info.get('canApply') is False or info.get('posted') is False:
                            continue
                        job = self._job(company, root, path, info, summary)
                        if not job.title or not job.description:
                            raise ValueError('Missing title or description')
                        jobs.append(job)
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                        audit['detail_failures'] += 1
                        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                        errors.append(path + ': ' + ('HTTP ' + str(status) if status else type(exc).__name__))
                        if status in {401, 403, 429}:
                            break
            audit['emitted_count'] = len(jobs)
            if errors:
                audit['state'] = 'partial' if listed else 'failed'
                audit['error'] = '; '.join(errors)
            return audit, jobs

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            pending = [executor.submit(fetch_board, company, root) for company, root in self.boards.items()]
            for future in as_completed(pending):
                audit, jobs = future.result()
                self.board_results.append(audit)
                yield from jobs
