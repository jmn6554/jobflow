"""Read-only live posting checks. Inaccessible is unknown, never assumed open."""
from datetime import datetime, timezone
import re
from threading import Lock
from urllib.parse import urlsplit, urljoin, quote, unquote, parse_qs

import httpx

from .providers import _job_posting, public_http_url, strip_html


class PostingStatus:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.boards = {}
        self.lock = Lock()

    def get(self, url):
        for _ in range(5):
            if not public_http_url(url) or urlsplit(url).username or urlsplit(url).password:
                raise ValueError('Unsafe URL')
            host = urlsplit(url).hostname or ''
            if host == 'linkedin.com' or host.endswith('.linkedin.com'):
                raise ValueError('LinkedIn-only link; employer posting not resolved')
            r = self.client.get(url, follow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, r.headers.get('location', ''))
                continue
            return r
        raise ValueError('Redirect limit')

    @staticmethod
    def result(state, detail, url):
        return dict(state=state, detail=detail, url=url, checked_at=datetime.now(timezone.utc).isoformat())

    @staticmethod
    def expired(value):
        if not value:
            return False
        try:
            date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if len(str(value)) == 10:
                return date.date() < datetime.now(timezone.utc).date()
            return date.replace(tzinfo=date.tzinfo or timezone.utc) < datetime.now(timezone.utc)
        except ValueError:
            return False

    def check(self, job):
        url = job.get('canonical_url') or job['url']
        def done(state, detail, evidence_url=url):
            return self.result(state, detail, evidence_url)
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            parts = [unquote(p) for p in parsed.path.strip('/').split('/')]
            greenhouse = re.fullmatch(r'(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io', host)
            custom_id = parse_qs(parsed.query).get('gh_jid', [''])[0]
            if not greenhouse and custom_id.isdigit() and job.get('greenhouse_board'):
                parts = [job['greenhouse_board'], 'jobs', custom_id]
                greenhouse = True
            if greenhouse and len(parts) >= 3 and parts[1] == 'jobs' and parts[2].isdigit():
                # EU-hosted boards also use the global public Job Board API.
                api_host = 'boards-api.greenhouse.io'
                api = f'https://{api_host}/v1/boards/{quote(parts[0], safe="")}/jobs/{parts[2]}'
                r = self.get(api)
                if r.status_code in (404, 410):
                    return done('closed', 'Posting unavailable in employer Greenhouse API', api)
                r.raise_for_status()
                data = r.json()
                if str(data.get('id')) != parts[2] or not data.get('title'):
                    return done('unknown', 'Unexpected Greenhouse response', api)
                if self.expired(data.get('application_deadline')):
                    return done('closed', 'Published application deadline passed', api)
                return done('open', 'Exact posting ID returned by employer Greenhouse API', api)
            if host in ('jobs.lever.co', 'jobs.eu.lever.co') and len(parts) >= 2:
                api_host = 'api.eu.lever.co' if '.eu.' in host else 'api.lever.co'
                api = f'https://{api_host}/v0/postings/{quote(parts[0], safe="")}/{quote(parts[1], safe="")}'
                r = self.get(api)
                if r.status_code in (404, 410):
                    return done('closed', 'Posting unavailable in employer Lever API', api)
                r.raise_for_status()
                data = r.json()
                if str(data.get('id')) == parts[1] and data.get('text'):
                    return done('open', 'Exact posting ID returned by employer Lever API', api)
                return done('unknown', 'Unexpected Lever response', api)
            if host == 'jobs.ashbyhq.com' and len(parts) >= 2:
                api = f'https://api.ashbyhq.com/posting-api/job-board/{quote(parts[0], safe="")}'
                with self.lock:
                    if api not in self.boards:
                        r = self.get(api)
                        r.raise_for_status()
                        data = r.json()
                        if not isinstance(data.get('jobs'), list):
                            raise ValueError('Invalid Ashby feed')
                        self.boards[api] = data['jobs']
                    jobs = self.boards[api]
                match = next((j for j in jobs if str(j.get('id')) == parts[1] or
                              urlsplit(j.get('jobUrl', '')).path.strip('/').split('/')[-1] == parts[1]), None)
                if match and match.get('isListed') is not False:
                    return done('open', 'Exact posting ID listed in current Ashby feed', api)
                return done('closed', 'Exact posting no longer listed in current Ashby feed', api)
            r = self.get(url)
            if r.status_code in (404, 410):
                return done('closed', f'Employer page HTTP {r.status_code}', str(r.url))
            r.raise_for_status()
            text = strip_html(re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', r.text, flags=re.I | re.S))
            if re.search(r'(?:this (?:job|position|posting) (?:is|has been) (?:no longer available|closed|filled|removed)|'
                         r'this (?:job|position) is no longer accepting applications)', text, re.I):
                return done('closed', 'Employer explicitly states this posting is closed', str(r.url))
            posting = _job_posting(r.text)
            normalize = lambda s: ' '.join(re.findall(r'\w+', str(s).casefold()))
            if posting and normalize(posting.get('title')) == normalize(job['title']):
                if self.expired(posting.get('validThrough')):
                    return done('closed', 'Structured posting expiration date passed', str(r.url))
                return done('open', 'Matching JobPosting metadata on live employer page', str(r.url))
            return done('unknown', 'Page reachable, but current matching vacancy not established', str(r.url))
        except httpx.HTTPStatusError as exc:
            return done('unknown', f'Posting check HTTP {exc.response.status_code}')
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            return done('unknown', 'Posting inaccessible or response could not establish current vacancy')
