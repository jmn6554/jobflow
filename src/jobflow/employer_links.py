"""Bounded public employer-link discovery, without login or application actions."""
import html
import json
import os
import re
import sqlite3
from urllib.parse import urlparse, urljoin, parse_qs
from urllib.robotparser import RobotFileParser

import httpx

from .models import JobInput
from .search_budget import SearchBudget
from .providers import (public_http_url, prohibited_automation_url, aggregator_url,
                        _job_posting, _location_text, _country_code_from_location,
                        strip_html, _sponsorship_from_text)
from .providers import DIRECT_ATS_HOSTS


def direct(url):
    return public_http_url(url) and not prohibited_automation_url(url) and not aggregator_url(url)


def unwrap(url):
    url = html.unescape(url)
    for _ in range(3):
        query = parse_qs(urlparse(url).query)
        target = next((query[k][0] for k in ('url', 'redirect', 'redirectUrl', 'destination')
                       if query.get(k) and public_http_url(query[k][0])), None)
        if not target:
            break
        url = target
    return url


class EmployerLinkFinder:
    def __init__(self, client=None, api_key=None, budget=None):
        self.client = client or httpx.Client(timeout=12, follow_redirects=False)
        self.api_key = os.environ.get('TAVILY_API_KEY', '') if api_key is None else api_key
        self.budget = budget if budget is not None else SearchBudget()
        self.robots = {}
        self.search_blocked = ''

    def get(self, url, linkedin=False):
        for _ in range(5):
            host = urlparse(url).hostname or ''
            if not direct(url) and not (linkedin and host in {'www.linkedin.com', 'linkedin.com'}):
                raise ValueError('Non-employer or unsafe redirect')
            response = self.client.get(url, headers={'User-Agent': 'JobFlowLocal/0.4'}, follow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers['location'])
                continue
            response.raise_for_status()
            return response
        raise ValueError('Too many redirects')

    def public_apply_links(self, url):
        origin = 'https://www.linkedin.com'
        if origin not in self.robots:
            response = self.client.get(origin + '/robots.txt', follow_redirects=False)
            response.raise_for_status()
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch('JobFlowLocal', url):
            return [], 'LinkedIn public access disallowed by robots policy'
        response = self.get(url, linkedin=True)
        links = []
        for match in re.finditer(r'<a\b([^>]*href=["\']([^"\']+)["\'][^>]*)>(.*?)</a>', response.text, re.I | re.S):
            if re.search(r'apply|offsite', match.group(1) + strip_html(match.group(3)), re.I):
                target = unwrap(urljoin(str(response.url), match.group(2)))
                if direct(target):
                    links.append(target)
        return links, 'Public LinkedIn page checked'

    def verify(self, url, alert):
        from .linkedin_resolver import normalized, compatible_location
        response = self.get(url)
        posting = _job_posting(response.text)
        if not posting:
            return None
        host = (urlparse(str(response.url)).hostname or '').removeprefix('www.')
        organization = posting.get('hiringOrganization')
        if isinstance(organization, str):
            organization = {'name': organization}
        if not isinstance(organization, dict):
            return None
        organization_url = organization.get('url') or ''
        if not isinstance(organization_url, str):
            return None
        organization_host = (urlparse(organization_url).hostname or '').removeprefix('www.')
        trusted_ats = any(host == domain or host.endswith('.' + domain) for domain in DIRECT_ATS_HOSTS)
        if not trusted_ats and not (organization_host and
                (host == organization_host or host.endswith('.' + organization_host))):
            return None
        company = organization.get('name', '')
        title = posting.get('title', '')
        if not isinstance(company, str) or not isinstance(title, str):
            return None
        if (normalized(company), normalized(title)) != (normalized(alert['company']), normalized(alert['title'])):
            return None
        location = posting.get('applicantLocationRequirements') or posting.get('jobLocation') or []
        if not isinstance(location, list):
            location = [location]
        location = ', '.join(_location_text(item) for item in location)
        country = _country_code_from_location(location, '')
        workplace = 'remote' if posting.get('jobLocationType') == 'TELECOMMUTE' else 'unspecified'
        if not compatible_location(alert, dict(country=country, workplace_type=workplace, location=location)):
            return None
        description = strip_html(posting.get('description', ''))
        if len(description) < 300:
            return None
        # Expired structured postings are not actionable links.
        from datetime import date
        expiry = str(posting.get('validThrough') or '')
        if expiry and re.match(r'^\d{4}-\d{2}-\d{2}', expiry) and expiry[:10] < date.today().isoformat():
            return None
        host = urlparse(str(response.url)).hostname or ''
        source = next((name for name, domain in [('greenhouse', 'greenhouse.io'), ('lever', 'lever.co'), ('ashby', 'ashbyhq.com')]
                       if host == domain or host.endswith('.' + domain)), 'employer_link')
        return JobInput(title=title, company=company, location=location, country=country,
                        source=source, url=str(response.url), canonical_url=str(response.url),
                        description=description, workplace_type=workplace,
                        sponsorship=_sponsorship_from_text(description, 'unknown'),
                        closes_at=expiry, raw_payload=json.dumps(posting))

    def find(self, alert, email_links=()):
        from .linkedin_resolver import normalized
        candidates, notes = list(email_links), []
        def verified(urls):
            found = {}
            for url in list(dict.fromkeys(unwrap(u) for u in urls))[:10]:
                if not direct(url):
                    continue
                try:
                    job = self.verify(url, alert)
                    if job:
                        found[job.canonical_url] = job
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    notes.append('Candidate page unavailable or invalid')
            return list(found.values())

        free_matches = verified(candidates)
        if free_matches:
            return free_matches, 'Verified email link; no search credit used'
        try:
            links, note = self.public_apply_links(alert['url'])
            candidates.extend(links)
            notes.append(note)
        except (httpx.HTTPError, ValueError, KeyError):
            notes.append('Public LinkedIn unavailable; no login attempted')
        free_matches = verified(candidates)
        if free_matches:
            return free_matches, 'Verified public Apply link; no search credit used'
        query = '"{}" "{}" {} careers apply'.format(normalized(alert['company']), normalized(alert['title']), alert['country'])
        try:
            cached = self.budget.cached(query)
        except (OSError, sqlite3.Error, ValueError):
            return [], 'Search cache unavailable; no search credit used'
        if cached is not None:
            return verified(cached), 'Reused search cache (or recent attempt); no search credit used'
        if self.search_blocked:
            notes.append(self.search_blocked)
        elif self.api_key:
            try:
                if not self.budget.reserve(query):
                    self.search_blocked = 'Tavily monthly cap reached or search already claimed; no request sent'
                    notes.append(self.search_blocked)
                else:
                    response = self.client.post('https://api.tavily.com/search',
                        json={'query': query,
                              'max_results': 8, 'search_depth': 'basic', 'auto_parameters': False,
                              'include_answer': False, 'include_raw_content': False},
                        headers={'Authorization': 'Bearer ' + self.api_key}, follow_redirects=False)
                    response.raise_for_status()
                    urls = [r.get('url', '') for r in response.json().get('results', [])]
                    self.budget.save_results(query, urls)
                    candidates.extend(urls)
            except httpx.HTTPStatusError as exc:
                note = 'Employer search HTTP {}'.format(exc.response.status_code)
                notes.append(note)
                if exc.response.status_code in {401, 402, 403, 422, 429, 432, 433}:
                    self.search_blocked = note + '; further searches paused for this run'
            except (httpx.HTTPError, ValueError):
                notes.append('Employer search request failed')
            except (OSError, sqlite3.Error):
                self.search_blocked = 'Search budget unavailable; searches paused safely'
                notes.append(self.search_blocked)
        else:
            notes.append('Employer search paused: TAVILY_API_KEY missing; no paid fallback')
        return verified(candidates), '; '.join(notes)
