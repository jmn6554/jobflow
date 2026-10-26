"""Employer-owned JSON link feeds that reference a shared Ashby recruiter board.

Only IDs linked by the employer's configured public listing are included. Never
assign every job on a recruitment agency's board to that employer.
"""
from urllib.parse import urlparse

from .providers import DiscoveryProvider, AshbyProvider, public_http_url, prohibited_automation_url
from .public_boards import safe_token, read_json, error_text


class ScopedAshbyProvider(DiscoveryProvider):
    name = 'ashby_scoped'

    def __init__(self, boards, listing_urls, client=None):
        super().__init__(client)
        self.boards = boards
        self.listing_urls = listing_urls
        self.board_results = []

    def fetch(self):
        self.board_results = []
        for company, token in self.boards.items():
            result = dict(company=company, board_token=token, state='completed', raw_count=0,
                          index_count=0, emitted_count=0, error='')
            try:
                safe_token(token)
                url = self.listing_urls[company]
                if not public_http_url(url) or prohibited_automation_url(url):
                    raise ValueError('Invalid employer listing URL')
                links = read_json(self.client, url)
                if not isinstance(links, dict) or not isinstance(links.get('jobs'), list):
                    raise ValueError('Malformed employer listing feed')
                ids = set()
                for entry in links['jobs']:
                    dest = urlparse(entry['url'])
                    parts = dest.path.strip('/').split('/')
                    if dest.scheme != 'https' or dest.hostname != 'jobs.ashbyhq.com' or len(parts) != 2 or parts[0].casefold() != token.casefold():
                        raise ValueError('Employer listing changed application platform')
                    ids.add(parts[1])
                result['raw_count'] = len(ids)
                # An explicit, valid empty employer feed is distinct from failure.
                if ids:
                    adapter = AshbyProvider({company: token}, client=self.client, prefilter=False)
                    found = set()
                    for job in adapter.fetch():
                        if job.external_id in ids:
                            found.add(job.external_id)
                            result['index_count'] += 1
                            result['emitted_count'] += 1
                            yield job
                    if found != ids or any(r['state'] != 'completed' for r in adapter.board_results):
                        result.update(state='partial', error='Employer-linked postings missing or shared board incomplete')
            except Exception as exc:
                result.update(state='partial' if result['emitted_count'] else 'failed', error=error_text(exc))
            self.board_results.append(result)
