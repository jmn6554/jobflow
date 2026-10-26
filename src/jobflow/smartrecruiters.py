"""Public SmartRecruiters Posting API, with source-wide pagination and parsing.

Reference: https://developers.smartrecruiters.com/docs/endpoints
Only PUBLIC listings; no credentials, internal postings or application writes.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import httpx

from .models import JobInput
from .providers import DiscoveryProvider, strip_html, normalize_employment_type, infer_compensation
from .titles import relevant_title, excluded_title


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value):
        raise ValueError('Invalid SmartRecruiters identifier')
    return value


class SmartRecruitersProvider(DiscoveryProvider):
    name = 'smartrecruiters'

    def __init__(self, boards, client=None, prefilter=True, max_pages=100, workers=4):
        super().__init__(client)
        self.boards = {company: identifier(token) for company, token in boards.items()}
        self.max_pages = max(1, min(int(max_pages), 1000))
        self.workers = 1 if client is not None else max(1, min(int(workers), 8))
        self.board_results = []
        # Country feeds plus global remote prevent a country-only primary
        # location from hiding multi-country remote postings. Final policy
        # still checks actual geography; remote does not imply worldwide.
        self.filters = ({'country': 'ca'}, {'country': 'us'}, {'locationType': 'REMOTE'})

    @staticmethod
    def _job(company, token, item):
        posting_id = identifier(str(item['id']))
        location = item.get('location') or {}
        job_ad = item.get('jobAd') or {}
        if not isinstance(job_ad, dict):
            raise ValueError('Malformed job advertisement')
        sections = job_ad.get('sections') or {}
        if not isinstance(location, dict) or not isinstance(sections, dict):
            raise ValueError('Malformed posting detail')
        description_parts = []
        for key, heading in [('companyDescription', 'Company Description'), ('jobDescription', 'Job Description'),
                             ('qualifications', 'Qualifications'), ('additionalInformation', 'Additional Information')]:
            section = sections.get(key) or {}
            content = section.get('text') if isinstance(section, dict) else section
            if isinstance(content, str) and content.strip():
                description_parts.append(heading + '\n' + strip_html(content))
        description = '\n\n'.join(description_parts)
        if not description.strip():
            raise ValueError('Posting detail has no description')
        fallback = f'https://jobs.smartrecruiters.com/{token}/{posting_id}'
        url = item.get('postingUrl') or fallback
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.hostname != 'jobs.smartrecruiters.com'
                or parsed.username or parsed.password or parsed.port
                or parsed.path.strip('/').split('/')[0].casefold() != token.casefold()):
            raise ValueError('Unexpected public posting URL')
        country = str(location.get('country') or '').upper()
        place = location.get('fullLocation') or ', '.join(str(location[k]) for k in ('city', 'region', 'country') if location.get(k))
        mode = str(location.get('locationType') or item.get('locationType') or '').lower()
        if mode == 'onsite':
            mode = 'on-site'
        if mode not in {'remote', 'hybrid', 'on-site'}:
            mode = 'hybrid' if location.get('hybrid') is True else 'remote' if location.get('remote') is True else 'unspecified'
        employment = item.get('typeOfEmployment') or {}
        if not isinstance(employment, dict):
            raise ValueError('Malformed employment type')
        return JobInput(title=str(item.get('name') or ''), company=company, location=place,
            country=country, url=url, canonical_url=url, source='smartrecruiters',
            external_id=token.casefold() + ':' + posting_id, description=description,
            workplace_type=mode, employment_type=normalize_employment_type(
                employment.get('label') or employment.get('id') or '', default='unspecified'),
            posted_at=str(item.get('releasedDate') or ''),
            compensation=infer_compensation(description, 'CAD' if country == 'CA' else 'USD'),
            raw_payload=json.dumps(item, sort_keys=True))

    def fetch(self):
        self.board_results = []

        def board(company, token):
            base = f'https://api.smartrecruiters.com/v1/companies/{token}/postings'
            audit = dict(company=company, board_token=token, state='completed', raw_count=0,
                index_count=0, emitted_count=0, pages=0, detail_failures=0, error='', queries=[])
            listings, jobs, errors, stop = {}, [], [], False
            for filters in self.filters:
                seen, offset, total = set(), 0, None
                query = dict(filters, complete=False, returned=0)
                audit['queries'].append(query)
                try:
                    for _ in range(self.max_pages):
                        response = self.client.get(base, params={'limit': 100, 'offset': offset,
                            'destination': 'PUBLIC', **filters})
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict) or not isinstance(payload.get('content'), list):
                            raise ValueError('Missing posting list')
                        items = payload['content']
                        total = payload.get('totalFound')
                        if not isinstance(total, int) or total < 0:
                            raise ValueError('Invalid listing total')
                        audit['pages'] += 1
                        query['total'] = total
                        new = 0
                        for item in items:
                            if not isinstance(item, dict):
                                raise ValueError('Malformed listing item')
                            posting_id = identifier(str(item.get('id') or ''))
                            if posting_id not in seen:
                                seen.add(posting_id)
                                new += 1
                            if item.get('visibility', 'PUBLIC') == 'PUBLIC':
                                listings[posting_id] = item
                        query['returned'] = len(seen)
                        if len(seen) >= total:
                            query['complete'] = True
                            break
                        if not items or not new:
                            raise ValueError('Listing ended or repeated before total was reached')
                        offset += len(items)
                    if not query['complete']:
                        errors.append('listing page cap reached')
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    errors.append('HTTP ' + str(exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__)
                    stop = True
                if stop:
                    break
            audit['raw_count'] = len(listings)
            relevant = [(pid, item) for pid, item in listings.items()
                        if relevant_title(str(item.get('name') or '')) and not excluded_title(str(item.get('name') or ''))]
            audit['index_count'] = len(relevant)
            if not stop:
                for posting_id, summary in relevant:
                    try:
                        response = self.client.get(base + '/' + posting_id)
                        response.raise_for_status()
                        item = response.json()
                        if not isinstance(item, dict) or str(item.get('id')) != posting_id:
                            raise ValueError('Posting identity mismatch')
                        if item.get('visibility', 'PUBLIC') != 'PUBLIC':
                            continue
                        jobs.append(self._job(company, token, item))
                    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                        audit['detail_failures'] += 1
                        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                        errors.append('detail HTTP ' + str(status) if status else 'detail ' + type(exc).__name__)
                        if status in {401, 403, 429}:
                            break
            audit['emitted_count'] = len(jobs)
            if errors:
                audit['state'] = 'partial' if audit['pages'] else 'failed'
                audit['error'] = '; '.join(dict.fromkeys(errors))
            return audit, jobs

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            tasks = [executor.submit(board, c, t) for c, t in self.boards.items()]
            for future in as_completed(tasks):
                audit, jobs = future.result()
                self.board_results.append(audit)
                yield from jobs
