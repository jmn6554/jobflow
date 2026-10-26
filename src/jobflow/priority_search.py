"""Read-only focused discovery, with honest per-employer coverage reporting."""
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from html import unescape

import httpx

from .models import JobInput, Compensation
from .policy import settings
from .titles import relevant_title, SYSTEMS_ADJACENT_SEARCH_TERMS
from .eligibility import evaluate, score, application_guard, role_exclusion, _workplace
from .providers import (GreenhouseProvider, AshbyProvider, strip_html, _job_posting,
    _location_text, _country_code_from_location, _workplace_from_text, infer_compensation,
    normalize_employment_type)


WORKDAY = {
    'Salesforce': 'https://salesforce.wd12.myworkdayjobs.com/External_Career_Site',
    'Adobe': 'https://adobe.wd5.myworkdayjobs.com/external_experienced',
    'GE Vernova': 'https://gevernova.wd5.myworkdayjobs.com/Vernova_ExternalSite',
    'Ciena': 'https://ciena.wd5.myworkdayjobs.com/careers',
    'Hydro Ottawa': 'https://hydroottawa.wd3.myworkdayjobs.com/hydro_ottawa_careersite',
    'Enbridge': 'https://enbridge.wd3.myworkdayjobs.com/enbridge_careers',
    'TC Energy': 'https://tcenergy.wd3.myworkdayjobs.com/CAREER_SITE_TC',
    'Itron': 'https://itron.wd5.myworkdayjobs.com/Itron',
}
SEARCH_PAGES = {
    'Google': 'https://www.google.com/about/careers/applications/jobs/results/',
    'NextEra Energy': 'https://jobs.nexteraenergy.com/search/',
    'TransAlta': 'https://jobs.transalta.com/search/',
    'Fortis': 'https://careers.fortisbc.com/search/',
    'Kinaxis': 'https://join.kinaxis.com/jobs',
    'Schneider Electric': 'https://careers.se.com/jobs?lang=en-US',
    'Atlassian': 'https://www.atlassian.com/company/careers/all-jobs',
    'Adobe': 'https://careers.adobe.com/us/en/search-results',
    'Innergex': 'https://fa-ewqm-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs',
}


class CareerMicrodata(HTMLParser):
    """Read visible schema.org fields used by SuccessFactors, without executing JS."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack, self.values = [], {}
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        prop = attrs.get('itemprop', '')
        if prop and attrs.get('content'):
            self.values.setdefault(prop, []).append(attrs['content'])
        if tag not in {'meta', 'link', 'br', 'hr', 'img', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'track'}:
            self.stack.append((tag, prop))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        for prop in {p for _, p in self.stack if p}:
            self.values.setdefault(prop, []).append(value)

    def posting(self):
        fields = {key: ' '.join(value).strip() for key, value in self.values.items()}
        if not fields.get('title') or not fields.get('description'):
            return None
        return {'title': fields['title'], 'description': fields['description'],
                'jobLocation': {'address': {k: fields.get(k, '') for k in ('addressLocality', 'addressRegion', 'addressCountry')}},
                'employmentType': fields.get('employmentType', 'full-time')}


def target_title(title):
    return relevant_title(title) and not role_exclusion(
        JobInput(title=title, company='', location='', url='', description=''))


def priority_salary(description, currency):
    # Google publishes ungrouped six-digit ranges, which the legacy parser misses.
    salary = re.search(r'\bUS:\s*\$(\d[\d,]+)\s*[-–]\s*\$(\d[\d,]+)\s*\(USD\)', description)
    if salary:
        return Compensation(float(salary[1].replace(',', '')), float(salary[2].replace(',', '')), 'USD')
    return infer_compensation(description, currency)


class PriorityDiscovery:
    def __init__(self, repo):
        self.repo = repo

    def _amazon(self, company, client, audit):
        seen = set()
        for term in ('data analyst', 'data engineer', 'systems analyst', 'analytics engineer', 'business intelligence', 'reporting analyst', 'business analyst', 'business systems analyst') + SYSTEMS_ADJACENT_SEARCH_TERMS:
            for offset in range(0, 300, 100):
                r = client.get('https://www.amazon.jobs/en/search.json', params={'base_query': term, 'offset': offset, 'result_limit': 100})
                r.raise_for_status()
                data = r.json()
                if data.get('error'):
                    raise ValueError('Amazon public search reported an error')
                items = data.get('jobs', [])
                fresh = 0
                for item in items:
                    key = item['id']
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh += 1
                    audit['listed'] += 1
                    if not target_title(item['title']):
                        continue
                    places = [json.loads(loc) if isinstance(loc, str) else loc for loc in item.get('locations', [])]
                    location = '; '.join(place.get('locationNonStemming', '') for place in places) or item.get('location', '')
                    codes = {place.get('countryIso2a', '') for place in places}
                    country = 'CA' if 'CA' in codes else 'US' if 'US' in codes else item.get('country_code', '')
                    description = '\n'.join(strip_html(item.get(k, '')) for k in ('description', 'basic_qualifications', 'preferred_qualifications'))
                    url = urljoin('https://www.amazon.jobs', item['job_path'])
                    job = JobInput(title=item['title'], company=company, location=location, country=country,
                        url=url, canonical_url=url, description=description, source='priority_careers', external_id=str(item['id_icims']),
                        employment_type=normalize_employment_type(item.get('job_schedule_type', 'full-time')))
                    types = {place.get('type', '').lower() for place in places}
                    workplace = 'remote' if types == {'remote'} else 'on-site' if types == {'onsite'} else _workplace(job)
                    yield replace(job, workplace_type=workplace, compensation=priority_salary(description, 'CAD' if country == 'CA' else 'USD'))
                if offset + len(items) >= data.get('hits', 0) or not items:
                    break
                if not fresh or offset == 200:
                    audit['truncated'] = True
                    break

    def _icims(self, company, root, client, audit):
        from .icims_public import ICIMSPublicProvider
        provider = ICIMSPublicProvider({company: root}, client=client)
        for job in provider.fetch():
            if target_title(job.title):
                # Canonical URL preserves existing records; IDs are now scoped
                # to the employer host rather than colliding across tenants.
                yield replace(job, source='priority_careers')
        result = provider.board_results[0]
        audit.update(listed=result['raw_count'], queries=result['queries'],
                     coverage_scope=result['coverage_scope'],
                     truncated=result['state'] != 'completed')
        if result['state'] != 'completed':
            audit.update(state=result['state'], error=result['error'])

    def _google(self, company, client, audit):
        root = SEARCH_PAGES['Google']
        seen = set()
        audit['truncated'] = True  # Bounded first pages, explicit rather than silently claiming exhaustive search.
        for location in ('Canada', 'United States'):
            for term in ('data analyst', 'data engineer', 'systems analyst', 'business intelligence', 'business analyst', 'business systems analyst') + SYSTEMS_ADJACENT_SEARCH_TERMS:
                r = client.get(root, params={'q': term, 'location': location})
                r.raise_for_status()
                match = re.search(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel:", r.text, re.S)
                if not match:
                    raise ValueError('Google public listing format changed')
                entries = json.loads(match[1])[0] or []
                for entry in entries:
                    if entry[0] in seen:
                        continue
                    seen.add(entry[0])
                    audit['listed'] += 1
                    if not target_title(entry[1]):
                        continue
                    links = re.findall(r'href="((?:https://[^" ]+/)?jobs/results/' + re.escape(entry[0]) + r'[^"?]*)', r.text)
                    if not links:
                        audit['unparsed'] = audit.get('unparsed', 0) + 1
                        continue
                    places = entry[9] or []
                    countries = {place[5] for place in places}
                    country = 'CA' if 'CA' in countries else 'US' if 'US' in countries else next(iter(countries), '')
                    description = '\n'.join(strip_html(entry[n][1]) for n in (3, 4, 10) if entry[n] and len(entry[n]) > 1)
                    job = JobInput(title=entry[1], company=company, location='; '.join(place[0] for place in places),
                        country=country, url=urljoin('https://www.google.com/about/careers/applications/', links[0]), canonical_url=urljoin('https://www.google.com/about/careers/applications/', links[0]),
                        source='priority_careers', external_id=entry[0], description=description)
                    yield replace(job, workplace_type=_workplace(job), compensation=priority_salary(description, 'CAD' if country == 'CA' else 'USD'))

    def _workday(self, company, root, client, audit):
        from .workday import WorkdayProvider
        provider = WorkdayProvider({company: root}, client=client)
        for job in provider.fetch():
            # Canonical URLs retain existing history while tenant-scoped IDs
            # prevent equally numbered requisitions at different employers
            # from overwriting each other.
            yield replace(job, source='priority_workday')
        result = provider.board_results[0]
        audit['listed'] = result['raw_count']
        audit['queries'] = result['queries']
        audit['truncated'] = result['state'] != 'completed'
        if result['state'] != 'completed':
            audit['state'] = result['state']
            audit['error'] = result['error']

    def _pages(self, company, root, client, audit):
        links = set()
        pages = []
        if '/search/' in root:
            for term in ('data', 'analyst', 'analytics', 'intelligence'):
                r = client.get(root, params={'q': term, 'startrow': 0})
                r.raise_for_status()
                pages.append(r)
            audit['truncated'] = True  # First search page only; never claim complete coverage.
        elif company == 'Google':
            for term in ('data analyst', 'data engineer', 'systems analyst', 'business intelligence', 'business analyst', 'business systems analyst') + SYSTEMS_ADJACENT_SEARCH_TERMS:
                r = client.get(root, params={'q': term})
                r.raise_for_status()
                pages.append(r)
            audit['truncated'] = True
        else:
            r = client.get(root)
            r.raise_for_status()
            pages.append(r)
        for r in pages:
            for match in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r.text, re.I | re.S):
                aria = re.search(r'aria-label=["\']([^"\']+)', match[0])
                title = strip_html(match.group(2)) or (unescape(aria[1]) if aria else '')
                url = urljoin(str(r.url), match.group(1))
                if target_title(title) and urlparse(url).hostname == urlparse(str(r.url)).hostname:
                    links.add(url)
        audit['listed'] = len(links)
        for url in sorted(links)[:50]:
            r = client.get(url)
            r.raise_for_status()
            posting = _job_posting(r.text) or CareerMicrodata(r.text).posting()
            if not posting:
                audit['unparsed'] = audit.get('unparsed', 0) + 1
                continue
            location = posting.get('applicantLocationRequirements') or posting.get('jobLocation') or []
            location = ', '.join(_location_text(x) for x in (location if isinstance(location, list) else [location]))
            description = strip_html(posting.get('description', ''))
            country = _country_code_from_location(location, '')
            yield JobInput(title=posting.get('title', ''), company=company, location=location,
                url=str(r.url), canonical_url=str(r.url), source='priority_careers', description=description,
                country=country, workplace_type='remote' if posting.get('jobLocationType') == 'TELECOMMUTE' else _workplace(JobInput(title=posting.get('title', ''), company=company, location=location, url=url, description=description)),
                employment_type=normalize_employment_type(posting.get('employmentType', 'full-time')),
                closes_at=posting.get('validThrough', ''),
                compensation=infer_compensation(description, 'CAD' if country == 'CA' else 'USD'))

    def collect(self, entry):
        company = entry['company']
        audit = {'company': company, 'careers_url': entry['careers_url'], 'listed': 0, 'state': 'checked', 'truncated': False}
        if company == 'Fortis':
            audit['coverage_note'] = 'FortisBC only, not every Fortis subsidiary'
        jobs = []
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            try:
                if company in WORKDAY:
                    audit['adapter'] = 'Workday public listings'
                    jobs.extend(self._workday(company, WORKDAY[company], client, audit))
                elif company in {'Kinaxis', 'Schneider Electric'}:
                    audit['adapter'] = 'iCIMS public career search'
                    root = 'https://join.kinaxis.com' if company == 'Kinaxis' else 'https://careers.se.com'
                    jobs.extend(self._icims(company, root, client, audit))
                elif company == 'Google':
                    audit['adapter'] = 'Google public career search'
                    jobs.extend(self._google(company, client, audit))
                elif company == 'Amazon':
                    audit['adapter'] = 'Amazon public job search'
                    jobs.extend(self._amazon(company, client, audit))
                elif company == 'Databricks':
                    audit['adapter'] = 'Greenhouse public feed'
                    jobs.extend(GreenhouseProvider({company: 'databricks'}, client, prefilter=False).fetch())
                    audit['listed'] = len(jobs)
                elif company == 'Snowflake':
                    audit['adapter'] = 'Ashby registry feed; official site also needs coverage'
                    jobs.extend(AshbyProvider({company: 'snowflake'}, client, prefilter=False).fetch())
                    audit['listed'] = len(jobs)
                    audit['state'] = 'partial'
                    if not jobs:
                        audit['state'] = 'adapter_needed'
                else:
                    audit['adapter'] = 'Official careers HTML (limited)'
                    jobs.extend(self._pages(company, SEARCH_PAGES.get(company, entry['careers_url']), client, audit))
                    audit['state'] = 'partial' if jobs else 'adapter_needed'
                if audit['truncated'] and audit['state'] == 'checked':
                    audit['state'] = 'partial'
            except Exception as exc:
                audit['state'] = 'partial' if jobs else 'failed'
                audit['error'] = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    audit['http_status'] = exc.response.status_code
        return audit, jobs

    def run(self):
        registry = json.loads(Path(settings()['priority_company_registry']).read_text())
        report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'sources': [], 'shortlist': [], 'review': [], 'excluded': {}, 'excluded_jobs': [], 'applications_started': 0}
        excluded = Counter()
        seen = set()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for audit, jobs in pool.map(self.collect, registry):
                relevant = [j for j in jobs if target_title(j.title)]
                audit['target_titles'] = len(relevant)
                report['sources'].append(audit)
                print(audit['company'] + ': ' + audit['state'] + ', ' + str(len(relevant)) + ' target-title postings', flush=True)
                for job in relevant:
                    decision = evaluate(job, ignore_salary=True)
                    guard = application_guard(job)
                    job_id, is_new, _ = self.repo.upsert_job(job)
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    match = score(job)
                    self.repo.save_evaluation(job_id, decision, match)
                    if decision.queue == 'rejected':
                        excluded.update(decision.reasons)
                        report['excluded_jobs'].append({'id': job_id, 'company': job.company, 'title': job.title,
                            'location': job.location, 'url': job.canonical_url or job.url, 'reasons': list(decision.reasons)})
                        continue
                    # Keep uncertain geography separate, never an application candidate.
                    uncertain = guard or ('Full description unavailable' if len(job.description) < 500 else '')
                    if not uncertain and re.search(r'\b(?:principal|staff)\b', job.title, re.I):
                        uncertain = 'Principal/staff scope needs review against the no-lead-role preference'
                    attempted = self.repo.submissions(job_id)
                    if any(r['state'] in {'submitted', 'confirmation_unknown', 'captcha_waiting'} for r in attempted):
                        excluded['Already submitted or confirmation pending'] += 1
                        continue
                    item = {'id': job_id, 'company': job.company, 'title': job.title, 'location': job.location,
                            'workplace': job.workplace_type, 'url': job.canonical_url, 'salary': 'Not published',
                            'warnings': list(decision.warnings), 'review_reason': uncertain,
                            'new': is_new, 'matched_skills': list(match.matched_skills), 'missing_skills': list(match.missing_skills),
                            'application_support': 'Existing Greenhouse worker; approval required' if job.source == 'greenhouse' else 'Manual/assisted; approval required'}
                    if job.compensation:
                        item['salary'] = '{}–{} {}'.format(job.compensation.minimum, job.compensation.maximum, job.compensation.currency)
                        if job.compensation.maximum and job.compensation.maximum < 1000:
                            item['salary'] += ' (pay period needs verification)'
                    report['review' if uncertain else 'shortlist'].append(item)
        report['excluded'] = dict(excluded)
        self.write_report(report)
        return report

    def write_report(self, report):
        """Render an existing completed pull without repeating network requests."""
        output = self.repo.path.parent / 'priority-shortlist.json'
        output.write_text(json.dumps(report, indent=2))
        lines = ['# Priority tech and energy search', '', 'Generated: ' + report['generated_at'], '',
                 'No applications submitted. Unresolved sources are coverage gaps, not zero-job results.', '',
                 '## Potential matches — pass filters, not confirmed eligibility', '',
                 'U.S. sponsorship is unconfirmed unless stated. For multi-location postings, only an approved location is acceptable. Pay ranges can vary by location. Skill overlap is not proof that every qualification is met.', '']
        for item in report['shortlist']:
            lines.extend(['- [{} — {}]({}) — {}; salary: {}. Matched skills: {}. Caveats: {}.'.format(
                item['company'], item['title'], item['url'], item['location'], item['salary'],
                ', '.join(item['matched_skills']) or 'Not established', '; '.join(item['warnings']) or 'None detected'), ''])
        if not report['shortlist']:
            lines.append('No confirmed matches from the sources parsed so far.')
        lines.extend(['', '## Needs evidence', ''])
        for item in report['review']:
            lines.append('- [{} — {}]({}): {}'.format(item['company'], item['title'], item['url'], item['review_reason']))
        lines.extend(['', '## Source coverage', ''])
        for audit in report['sources']:
            lines.append('- {}: {}; {} target-title postings parsed. {}'.format(audit['company'], audit['state'], audit['target_titles'], audit.get('coverage_note', '')))
        output.with_suffix('.md').write_text('\n'.join(lines) + '\n')
        return report
