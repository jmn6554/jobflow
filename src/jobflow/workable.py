"""Public Workable career widget feed; no employer API key or submission API."""
import json
import re
from urllib.parse import quote

from .models import JobInput
from .providers import (DiscoveryProvider, strip_html, _country_code_from_location,
                        _workplace_from_text, infer_compensation, normalize_employment_type,
                        public_http_url, prohibited_automation_url)


class WorkableProvider(DiscoveryProvider):
    name = 'workable'

    def __init__(self, boards, client=None, prefilter=False):
        super().__init__(client)
        self.boards = boards
        self.board_results = []

    @staticmethod
    def _job(company, item):
        url = item.get('url', '')
        if not public_http_url(url) or prohibited_automation_url(url):
            raise ValueError('Invalid public posting URL')
        description = strip_html(' '.join(str(item.get(k) or '') for k in ('description', 'requirements', 'benefits')))
        location = ', '.join(str(item[k]) for k in ('city', 'state', 'country') if item.get(k))
        country = _country_code_from_location(location)
        # Do not equate a remote flag with worldwide or Quebec eligibility.
        workplace = 'remote' if item.get('telecommuting') is True else _workplace_from_text(description)
        return JobInput(title=item['title'], company=company, location=location,
                        country=country, workplace_type=workplace, url=url, canonical_url=url,
                        description=description, source='workable', external_id=item['shortcode'],
                        employment_type=normalize_employment_type(item.get('employment_type', '')),
                        posted_at=item.get('published_on', ''),
                        compensation=infer_compensation(description, 'CAD' if country == 'CA' else 'USD'),
                        raw_payload=json.dumps(item))

    def fetch(self):
        self.board_results = []
        for company, token in self.boards.items():
            if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
                raise ValueError('Invalid Workable account token')
            result = dict(company=company, board_token=token, state='completed', raw_count=0,
                          index_count=0, emitted_count=0, error='')
            try:
                response = self.client.get('https://apply.workable.com/api/v1/widget/accounts/' + quote(token),
                                           params={'details': 'true'})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list):
                    raise ValueError('Malformed Workable board response')
                result['raw_count'] = len(payload['jobs'])
                seen = set()
                for item in payload['jobs']:
                    if not isinstance(item, dict) or not item.get('shortcode') or not item.get('title'):
                        raise ValueError('Malformed Workable posting')
                    if item['shortcode'] in seen:
                        continue
                    seen.add(item['shortcode'])
                    job = self._job(company, item)
                    result['index_count'] += 1
                    result['emitted_count'] += 1
                    yield job
                if payload.get('next') or payload.get('next_page'):
                    result.update(state='partial', error='Additional page advertised; not all postings retrieved')
            except Exception as exc:
                result.update(state='partial' if result['emitted_count'] else 'failed',
                              error='HTTP {}'.format(exc.response.status_code) if hasattr(exc, 'response') else type(exc).__name__)
            self.board_results.append(result)
