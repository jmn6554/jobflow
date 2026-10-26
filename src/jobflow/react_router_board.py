"""Public React Router careers boards backed by published Ashby postings.

Only public job fields are retained; loader recruitment-team metadata is not.
"""
import json
from urllib.parse import urlsplit
from uuid import UUID

from .models import JobInput
from .providers import (DiscoveryProvider, strip_html, public_http_url,
                        _country_code_from_location, normalize_employment_type,
                        infer_compensation)
from .react_router_data import LoaderData


class ReactRouterBoardProvider(DiscoveryProvider):
    name = 'react_router_board'

    def __init__(self, boards, client=None, prefilter=False):
        super().__init__(client)
        self.boards = boards
        self.board_results = []

    @staticmethod
    def _job(company, base, item, expected_id):
        if (item.get('id') != expected_id or item.get('status') != 'Published'
                or item.get('isListed') is not True or not item.get('title')):
            raise ValueError('Posting identity or publication state mismatch')
        description = item.get('descriptionPlain') or strip_html(item.get('descriptionHtml', ''))
        if not description:
            raise ValueError('Full posting description missing')
        location = item.get('locationName') or ''
        country = _country_code_from_location(location)
        url = base + '?ashby_jid=' + expected_id
        public = {k: item.get(k) for k in ('id', 'title', 'locationName', 'workplaceType',
                  'employmentType', 'publishedDate', 'applicationDeadline')}
        return JobInput(title=item['title'], company=company, location=location,
                        country=country, workplace_type='remote' if item.get('isRemote') is True
                        or item.get('workplaceType') == 'Remote' else 'unspecified',
                        url=url, canonical_url=url, source='react_router_board', external_id=expected_id,
                        description=description,
                        employment_type=normalize_employment_type(item.get('employmentType', '')),
                        posted_at=item.get('publishedDate') or '',
                        closes_at=item.get('applicationDeadline') or '',
                        compensation=infer_compensation(description, 'CAD' if country == 'CA' else 'USD'),
                        raw_payload=json.dumps(public))

    def fetch(self):
        self.board_results = []
        for company, base in self.boards.items():
            result = dict(company=company, board_token=base, state='completed', raw_count=0,
                          index_count=0, emitted_count=0, error='', detail_failures=[])
            try:
                if not public_http_url(base) or urlsplit(base).query:
                    raise ValueError('Invalid public careers URL')
                response = self.client.get(base, follow_redirects=True)
                response.raise_for_status()
                rows = LoaderData(response.text).field('jobPostingsWithJobs')
                if not isinstance(rows, list):
                    raise ValueError('Malformed posting index')
                result['raw_count'] = len(rows)
                seen = set()
                for row in rows:
                    item = row['jobPosting']
                    identifier = str(UUID(item['id']))
                    if item.get('status') != 'Published' or item.get('isListed') is not True:
                        continue
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    result['index_count'] += 1
                    try:
                        detail = self.client.get(base, params={'ashby_jid': identifier}, follow_redirects=True)
                        detail.raise_for_status()
                        if urlsplit(str(detail.url)).hostname != urlsplit(base).hostname:
                            raise ValueError('Unexpected posting redirect')
                        job = self._job(company, base, LoaderData(detail.text).field('jobPosting'), identifier)
                        result['emitted_count'] += 1
                        yield job
                    except Exception as exc:
                        result['detail_failures'].append({'id': identifier, 'error': type(exc).__name__})
                if result['detail_failures']:
                    result.update(state='partial', error='Some posting details could not be verified')
            except Exception as exc:
                result.update(state='partial' if result['emitted_count'] else 'failed', error=type(exc).__name__)
            self.board_results.append(result)
