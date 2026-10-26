"""Paginated public iCIMS career-site JSON search, shared across employers."""
from dataclasses import replace
from urllib.parse import urlparse
import json
import re

from .providers import DiscoveryProvider, public_http_url, prohibited_automation_url, strip_html, normalize_employment_type, infer_compensation, _workplace_from_text
from .models import JobInput
from .titles import relevant_title
from .public_boards import error_text


class ICIMSPublicProvider(DiscoveryProvider):
    name = 'icims_public'
    queries = ('data', 'analyst', 'analytics', 'intelligence', 'implementation', 'integration', 'consultant', 'functional', 'implantation', 'onboarding')

    def __init__(self, boards, client=None, prefilter=True, max_pages=100):
        super().__init__(client)
        self.boards = boards
        self.prefilter, self.max_pages = prefilter, max_pages
        self.board_results = []

    @staticmethod
    def job(company, root, data):
        key = str(data['slug'])
        if not re.fullmatch(r'[A-Za-z0-9_-]+', key) or not data.get('title') or not data.get('description'):
            raise ValueError('Missing iCIMS posting identity or detail')
        location = data.get('location_name') or ', '.join(data.get(k) or '' for k in ('city', 'state', 'country'))
        text = strip_html(data['description'])
        country = data.get('country_code') or ''
        job = JobInput(title=data['title'], company=company, source='icims_public',
            external_id=urlparse(root).hostname + ':' + key, url=root + '/jobs/' + key,
            canonical_url=root + '/jobs/' + key, description=text, location=location, country=country,
            employment_type=normalize_employment_type(data.get('employment_type') or '', default='unspecified'),
            compensation=infer_compensation(text, 'CAD' if country == 'CA' else 'USD'),
            raw_payload=json.dumps(data))
        workplace = _workplace_from_text(' '.join(data.get('tags7') or []))
        if workplace == 'unspecified':
            from .eligibility import _workplace
            workplace = _workplace(job)
        return replace(job, workplace_type=workplace)

    def fetch(self):
        self.board_results = []
        for company, configured in self.boards.items():
            root = configured.rstrip('/')
            result = dict(company=company, board_token=configured, state='completed', raw_count=0,
                index_count=0, emitted_count=0, queries={}, error='', coverage_scope='configured_role_queries')
            all_seen, errors, stop = set(), [], False
            try:
                if not public_http_url(root) or prohibited_automation_url(root) or urlparse(root).path not in ('', '/'):
                    raise ValueError('Expected reviewed public careers origin')
                for term in self.queries:
                    seen, expected = set(), None
                    audit = {'pages': 0, 'total': None, 'unique': 0, 'complete': False}
                    result['queries'][term] = audit
                    try:
                        for page in range(1, self.max_pages + 1):
                            r = self.client.get(root + '/api/jobs', params={'keywords': term, 'limit': 100, 'page': page})
                            r.raise_for_status()
                            payload = r.json()
                            items, total = payload.get('jobs'), payload.get('totalCount')
                            if not isinstance(items, list) or type(total) is not int or total < 0:
                                raise ValueError('Missing iCIMS search totals')
                            if expected is not None and expected != total:
                                raise ValueError('iCIMS inventory changed during pagination')
                            expected = total
                            fresh = 0
                            audit.update(pages=page, total=total)
                            for item in items:
                                data = item['data']
                                key = str(data['slug'])
                                if key not in seen:
                                    seen.add(key)
                                    fresh += 1
                                if key in all_seen:
                                    continue
                                all_seen.add(key)
                                result['raw_count'] += 1
                                if self.prefilter and not relevant_title(data.get('title', '')):
                                    continue
                                result['index_count'] += 1
                                try:
                                    job = self.job(company, root, data)
                                    result['emitted_count'] += 1
                                    yield job
                                except Exception as exc:
                                    errors.append('detail: ' + error_text(exc))
                            audit['unique'] = len(seen)
                            if len(seen) == expected:
                                audit['complete'] = True
                                break
                            if not fresh or len(seen) > expected or not items:
                                raise ValueError('iCIMS pagination stalled or contradicted total')
                        if not audit['complete']:
                            errors.append(term + ': page limit reached')
                    except Exception as exc:
                        errors.append(term + ': ' + error_text(exc))
                        if hasattr(exc, 'response') and exc.response.status_code in (401, 403, 404, 429):
                            stop = True
                    if stop:
                        break
            except Exception as exc:
                errors.append(error_text(exc))
            if errors:
                result.update(state='partial' if result['raw_count'] else 'failed', error='; '.join(sorted(set(errors))))
            self.board_results.append(result)
