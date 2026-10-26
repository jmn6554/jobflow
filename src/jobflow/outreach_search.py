"""Bounded public search evidence, not LinkedIn messaging or identity verification."""
import html
import os
import re
import sqlite3
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

import httpx

from .search_budget import SearchBudget
from .outreach_safety import search_safety


LABELS = {
    'job_poster': 'Exact-title listing with job-poster evidence',
    'hiring_post': 'Exact-title hiring post — author authority unverified',
    'company_post': 'Company hiring announcement — no person identified',
    'related': 'Related lead — exact opening not established',
}

STRONG_KINDS = {'job_poster', 'hiring_post'}


def clean_query_text(value):
    return ' '.join(str(value).replace('"', ' ').split())[:200]


def role_words(value):
    """Equivalent abbreviations only; never erase seniority or specialisms."""
    text = ' '.join(re.findall(r'\w+', value.casefold()))
    return re.sub(r'\b(?:sr|jr)\b', lambda m: {'sr':'senior', 'jr':'junior'}[m[0]], text)


def search_query(company, title):
    return f'"{clean_query_text(company)}" "{clean_query_text(title)}" site:linkedin.com (hiring OR "job poster" OR "our team")'


def linkedin_evidence_url(value):
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or p.username or p.password or p.port or
                not re.fullmatch(r'(?:www\.|[a-z]{2}\.)?linkedin\.com', p.hostname or '')):
            return ''
        if not re.match(r'^/(?:posts/|feed/update/|jobs/view/|in/)', p.path):
            return ''
        return urlunsplit(('https', p.hostname, p.path, '', ''))
    except (ValueError, TypeError):
        return ''


def classify_results(results, company, title):
    def normalized(value):
        return role_words(value)
    def contains(text, phrase):
        return bool(phrase) and (' ' + phrase + ' ') in (' ' + text + ' ')
    leads, seen = [], set()
    for result in results[:8]:
        if not isinstance(result, dict):
            continue
        url = linkedin_evidence_url(result.get('url'))
        if not url or url in seen:
            continue
        seen.add(url)
        heading = str(result.get('title') or '')[:400]
        snippet = html.unescape(re.sub('<[^>]+>', ' ', str(result.get('content') or '')))[:5000]
        text = normalized(heading + ' ' + snippet)
        company_text, role_text = normalized(company), normalized(title)
        # A company mentioned as a tool, in comments, or in related listings is
        # not evidence that this person is recruiting for that employer.
        employer_context = (contains(normalized(heading), company_text) or
            any(contains(text, phrase) for phrase in (
                'at ' + company_text, 'join ' + company_text,
                company_text + ' is hiring', company_text + ' hiring',
                role_text + ' ' + company_text)))
        title_pattern = r'(?<!\w)' + re.escape(role_text) + r'(?!\w)'
        matches = list(re.finditer(title_pattern, text)) if role_text else []
        def same_level(match):
            before = text[:match.start()].split()[-1:]
            after = text[match.end():].split()[:1]
            # "Analytics Engineer Manager" is not "Analytics Engineer".
            return not ((before and before[0] in {'senior', 'staff', 'principal', 'lead', 'junior'}
                         and before[0] not in role_text.split()) or
                        (after and after[0] in {'manager', 'director', 'lead'}
                         and after[0] not in role_text.split()))
        exact = employer_context and any(same_level(m) for m in matches)
        path = urlsplit(url).path
        kind = 'related'
        poster_employer = any(contains(text, phrase) for phrase in (
            'job poster from ' + company_text, 'job poster at ' + company_text))
        listing_heading = contains(normalized(heading), role_text)
        if exact and path.startswith('/jobs/view/') and 'job poster' in text and listing_heading and (
                poster_employer or contains(normalized(heading), company_text)):
            kind = 'job_poster'
        elif exact and path.startswith(('/posts/', '/feed/update/')) and re.search(r'\b(hiring|looking for|join our|our team)\b', text):
            kind = 'hiring_post'
            if contains(normalized(snippet), company_text + ' s post') or contains(normalized(heading), company_text + ' s post'):
                kind = 'company_post'
        leads.append(dict(url=url, title=heading, evidence=snippet, kind=kind,
                          confidence='unverified', safety=search_safety(heading + ' ' + snippet)))
    return sorted(leads, key=lambda r: list(LABELS).index(r['kind']))


class OutreachSearch:
    def __init__(self, client=None, api_key=None, budget=None, max_requests=10, max_per_company=2):
        self.client = client
        self.api_key = os.environ.get('TAVILY_API_KEY', '') if api_key is None else api_key
        self.budget = budget if budget is not None else SearchBudget()
        self.max_requests = max(0, int(max_requests))
        self.max_per_company = max(0, int(max_per_company))
        self.requests_used = 0
        self.company_requests = Counter()
        self.reused = 0
        self.halted = ''

    def reuse(self, company, title):
        """A single hiring post may name several openings; reclassify, don't guess."""
        needle = '"' + clean_query_text(company).casefold() + '" '
        entries = self.budget.cached_entries('outreach-evidence-')
        found = {}
        for key, results in entries:
            if not key.partition(':')[2].casefold().startswith(needle):
                continue
            for lead in classify_results(results, company, title):
                if lead['kind'] in STRONG_KINDS:
                    found.setdefault(lead['url'], lead)
        return list(found.values())[:8]

    def find(self, company, title):
        query = search_query(company, title)
        # Distinct cache namespace: employer-link searches store URLs, we store snippets.
        key = 'outreach-evidence-v1:' + query.casefold()
        try:
            cached = self.budget.cached(key)
            if cached is None and key != 'outreach-evidence-v1:' + query:
                cached = self.budget.cached('outreach-evidence-v1:' + query)
            if cached is not None:
                self.reused += 1
                return classify_results(cached, company, title), 'Cached results or recent attempt; no search credit used.'
            # Also share case-only title/company variants without changing old cache keys.
            for saved_key, results in self.budget.cached_entries('outreach-evidence-v1:'):
                if saved_key.casefold() == key.casefold():
                    self.reused += 1
                    return classify_results(results, company, title), 'Cached equivalent query; no search credit used.'
            reused = self.reuse(company, title)
            if reused:
                self.reused += 1
                return reused, 'Reused matching hiring-post evidence from another role at this company; no search credit used. Exact vacancy and contact authority remain unverified.'
            if self.halted:
                return [], self.halted
            if not self.api_key:
                return [], 'Search key missing; use the free public-search links. No request sent.'
            company_key = clean_query_text(company).casefold()
            if self.requests_used >= self.max_requests:
                return [], 'Deferred: per-run search budget reached. No request sent.'
            if self.company_requests[company_key] >= self.max_per_company:
                return [], 'Deferred: per-company search budget reached. No request sent.'
            if not self.budget.reserve(key):
                return [], 'Shared monthly search cap reached or search already claimed. No request sent.'
            self.requests_used += 1
            self.company_requests[company_key] += 1
            payload = dict(query=query, max_results=8, search_depth='basic', auto_parameters=False,
                           include_answer=False, include_raw_content=False, include_domains=['linkedin.com'])
            def request(client):
                response = client.post('https://api.tavily.com/search', json=payload,
                    headers={'Authorization': 'Bearer ' + self.api_key}, follow_redirects=False)
                response.raise_for_status()
                results = response.json().get('results', [])
                if not isinstance(results, list):
                    raise ValueError('Invalid search results')
                return [{k: str(r.get(k) or '')[:5000] for k in ('url', 'title', 'content')}
                        for r in results[:8] if isinstance(r, dict)]
            if self.client is not None:
                results = request(self.client)
            else:
                with httpx.Client(timeout=15) as client:
                    results = request(client)
            self.budget.save_results(key, results)
            return classify_results(results, company, title), 'One bounded search completed; results cached for seven days. Contacts are not verified.'
        except httpx.HTTPStatusError as exc:
            self.halted = f'Search HTTP {exc.response.status_code}; no automatic retry or paid fallback.'
            return [], self.halted
        except (httpx.HTTPError, ValueError, TypeError, OSError, sqlite3.Error):
            self.halted = 'Search or budget unavailable; stopped safely without automatic retry.'
            return [], self.halted
