"""Conservative first-pass screening of raw cards, not application approval."""
import re
from urllib.parse import urlsplit, parse_qs
from .dedupe import canonical_url
from .eligibility import role_exclusion, _country, _workplace
from .models import JobInput
from .policy import local_office, us_office
from .titles import relevant_title, title_review_reason


def posting_key(url):
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    gh_id = query.get('gh_jid', [''])[0]
    if gh_id.isdigit():
        return 'greenhouse:'+gh_id
    if parts.hostname in {'boards.greenhouse.io','job-boards.greenhouse.io','boards.eu.greenhouse.io','job-boards.eu.greenhouse.io'}:
        match = re.search(r'/jobs/(\d+)(?:/|$)',parts.path)
        if match:
            return 'greenhouse:'+match[1]
    if parts.hostname in {'jobs.ashbyhq.com', 'jobs.lever.co', 'jobs.eu.lever.co'}:
        match = re.search(r'/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:/|$)', parts.path, re.I)
        if match:
            return parts.hostname.lower() + ':' + match[1].lower()
    return canonical_url(url)


def screen(row):
    # Source title-filter waivers from earlier application batches do not turn
    # irrelevant LinkedIn cards into matches in this fresh criteria audit.
    job = JobInput(title=row['title'], company=row['company'], location=row.get('location',''),
                   url=row.get('url',row.get('linkedin_url','')), description='', source='raw_audit')
    if re.search(r'\b(?:contract|contractor|part[- ]time|temporary|student worker)\b',job.title,re.I):
        return 'excluded', 'Explicit non-permanent/full-time title'
    exclusion = role_exclusion(job)
    if exclusion:
        return 'excluded', exclusion
    adjacent = bool(re.search(
        r'\b(?:data analytics consultant|technical analyst|decision intelligence analyst|'
        r'(?:product|operations|operational|customer|financial) (?:analytics|insights)|'
        r'data (?:integration|migration|modeling|modelling|quality|governance))\b', job.title, re.I))
    if not relevant_title(job.title) and not adjacent:
        return 'excluded', 'Outside target role families'
    country, mode = _country(job), _workplace(job)
    if country == 'US' and re.search(r'\b(?:top secret|active.*clearance|clearance required)\b',job.title,re.I):
        return 'excluded', 'Explicit US clearance requirement'
    if country == 'OTHER':
        return 'excluded', 'Location explicitly outside Canada/US'
    mixed = bool(re.search(r'\bremote\b', job.location,re.I) and re.search(r'\bhybrid\b|on[- ]site|optional',job.location,re.I))
    if not mixed and mode in {'hybrid','on-site'} and country in {'CA','US'}:
        if not (local_office(job.location) if country == 'CA' else us_office(job.location)):
            return 'excluded', 'Explicit office requirement outside approved areas'
    if adjacent and not relevant_title(job.title):
        return 'review', 'Adjacent title: inspect actual responsibilities'
    if re.search(r'\b(?:acquisition|federal|defense|fedd\d|us only|usa only)\b',job.title,re.I):
        return 'review', 'Domain or US eligibility restriction needs JD check'
    if title_review_reason(job.title) or re.search(r'\b(?:software|full[- ]stack|architect)\b',job.title,re.I):
        return 'review', 'Engineering scope may exceed data/analytics role; inspect responsibilities'
    if mixed:
        return 'review', 'Mixed remote/office options; verify available location'
    if re.search(r'\b(?:global|worldwide|north america)\b',job.location,re.I):
        return 'review', 'Broad region does not prove Canadian eligibility'
    if not country or (mode != 'remote' and not local_office(job.location) and not us_office(job.location)):
        return 'review', 'Country or work arrangement needs evidence'
    if country == 'US':
        return 'review', 'US sponsorship and current-residency requirements unverified'
    if job.company.casefold() in {'jobright.ai','ladders','wiraa','detailpage','torentify','haystack'}:
        return 'review', 'Intermediary listing: employer and original posting need verification'
    return 'potential', 'Title/location plausible; JD, experience, employment type and sponsorship unverified'
