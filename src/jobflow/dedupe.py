import hashlib
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from .models import JobInput


TRACKING_PARAMS = {"fbclid", "gclid", "ref", "refid", "source", "jclickid"}


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def identity_key(job: JobInput) -> str:
    if job.external_id:
        raw = "external|{}|{}".format(normalize_text(job.source), job.external_id.strip())
    elif canonical_url(job.url):
        raw = "url|{}".format(canonical_url(job.url))
    else:
        raw = "fields|{}|{}|{}".format(
            normalize_text(job.title), normalize_text(job.company), normalize_text(job.location)
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def application_posting_keys(row):
    """Posting identities for history matching, never company/title similarity.

    Keep URL aliases as well as recognized requisition IDs. Numeric Greenhouse
    IDs are employer-scoped; Workday IDs are tenant-scoped. Unknown sources use
    their canonical URL, not guessed IDs or names. Empty URLs cannot match.
    """
    row = dict(row)
    keys = set()
    for field in ('canonical_url', 'url', 'employer_url', 'linkedin_url'):
        url = row.get(field) or ''
        if not url.strip():
            continue
        parts = urlsplit(url.strip())
        host = (parts.hostname or '').lower()
        if parts.scheme not in {'http', 'https'} or not host:
            continue
        keys.add(('url', canonical_url(url)))
        path = parts.path
        query = {k.lower(): v for k, v in parse_qs(parts.query).items()}
        if host == 'linkedin.com' or host.endswith('.linkedin.com'):
            match = re.match(r'/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)', path)
            if match:
                keys.add(('linkedin', match[1]))
        # Locale and application-form aliases must not multiply one vacancy.
        # Scope every identifier to its ATS tenant, never just the job title.
        uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
        if host.endswith('.pinpointhq.com'):
            match = re.search(r'/postings/(' + uuid + r')(?:/|$)', path, re.I)
            if match:
                keys.add(('pinpoint', host, match[1].lower()))
        if host in {'jobs.dayforcehcm.com', 'jobs.dayforce.com'}:
            match = re.match(r'/[a-z]{2}-[a-z]{2}/([^/]+)/[^/]+/jobs/(\d+)', path, re.I)
            if match:
                keys.add(('dayforce', match[1].lower(), match[2]))
        if host == 'jobs.smartrecruiters.com':
            match = re.match(r'/([^/]+)/(\d+)(?:-|/|$)', path)
            if match:
                keys.add(('smartrecruiters', match[1].lower(), match[2]))
        if host.endswith('.oraclecloud.com'):
            match = re.search(r'/CandidateExperience/[^/]+/(?:sites/[^/]+/)?job/([^/?]+)', path, re.I)
            if match:
                keys.add(('oracle', host, match[1]))
        if host.endswith('.bamboohr.com'):
            match = re.search(r'/careers/(\d+)(?:/|$)', path)
            if match:
                keys.add(('bamboohr', host, match[1]))
        if host in {'recruiting.ultipro.com', 'recruiting.ultipro.ca'}:
            posting = query.get('opportunityid', [''])[0]
            if re.fullmatch(uuid, posting, re.I):
                keys.add(('ultipro', host, path.strip('/').split('/')[0].lower(), posting.lower()))
        gh_id = parse_qs(parts.query).get('gh_jid', [''])[0]
        gh_hosts = {'boards.greenhouse.io', 'job-boards.greenhouse.io',
                    'boards.eu.greenhouse.io', 'job-boards.eu.greenhouse.io'}
        if host in gh_hosts:
            match = re.search(r'/jobs/(\d+)(?:/|$)', path)
            gh_id = match[1] if match else gh_id
        if gh_id.isdigit():
            employer = normalize_text(row.get('company') or '')
            scope = employer or (host + '/' + path.strip('/').split('/')[0]
                                if host in gh_hosts else host)
            keys.add(('greenhouse', scope, gh_id))
        if host in {'jobs.ashbyhq.com', 'jobs.lever.co', 'jobs.eu.lever.co'}:
            match = re.match(r'/([^/]+)/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:/|$)', path, re.I)
            if match:
                ats = 'ashby' if host == 'jobs.ashbyhq.com' else 'lever'
                keys.add((ats, match[1].lower(), match[2].lower()))
        if host.endswith('.myworkdayjobs.com'):
            match = re.search(r'/job/.+_([^/]+)(?:/apply)?/?$', path, re.I)
            if match:
                keys.add(('workday', host.split('.')[0], match[1]))
    return keys


def prefer_canadian_regional_variants(candidates, protected=()):
    """Defer a near-identical US variant when a Canadian option exists.

    Keep both database records: matching wording is not proof of one vacancy.
    This is application selection, not destructive deduplication. Previously
    submitted or unresolved Canadian attempts also protect against sending the
    same application through the US variant on a later run.
    """
    def country(row):
        explicit = (row.get('country') or '').upper()
        if explicit in {'CA', 'CAN', 'CANADA'}:
            return 'CA'
        if explicit in {'US', 'USA', 'UNITED STATES'}:
            return 'US'
        location = row.get('location') or ''
        if re.search(r'\bCanada\b', location, re.I):
            return 'CA'
        if re.search(r'\b(?:US|USA|United States)\b', location):
            return 'US'
        return ''

    def family(row):
        return normalize_text(row['company']), normalize_text(row['title'])

    canadian = {}
    for row in list(protected) + list(candidates):
        if country(row) == 'CA' and row.get('workplace_type') == 'remote':
            canadian.setdefault(family(row), []).append(row)
    retained, deferred = [], []
    for row in candidates:
        alternative = None
        if country(row) == 'US' and row.get('workplace_type') == 'remote':
            text = normalize_text(row.get('description') or '')
            if len(text) >= 500:
                for ca in canadian.get(family(row), []):
                    other = normalize_text(ca.get('description') or '')
                    if SequenceMatcher(None, text, other).ratio() >= .97:
                        alternative = ca
                        break
        if alternative:
            deferred.append({'id': row['id'], 'title': row['title'], 'company': row['company'],
                'url': row.get('canonical_url') or row['url'], 'preferred_id': alternative['id'],
                'reason': 'Near-identical US/Canada regional alternatives; prefer Canadian posting'})
        else:
            retained.append(row)
    return retained, deferred
