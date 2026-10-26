"""Explainable work-fit priorities, not a qualification probability or exclusion gate.

No network/model calls, discovery, retries or submissions in this module.
The shared daily eligibility/duplicate guards remain authoritative.
"""
import html
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .dedupe import canonical_url
from .policy import local_office

VERSION = 'work-fit-v1'
TRACKS = {
    'data_analytics': 'Emphasize existing data pipelines, SQL/Python, Databricks, data quality and reporting achievements when relevant. Do not turn systems work into data science or add technologies.',
    'systems_integration': 'Emphasize existing systems integration, APIs, requirements translation, validation, operational reliability and automation when relevant. Preserve the actual energy/trading context; do not claim ERP, clinical or other specialist platform experience.',
}
# Each independent responsibility counts once. These are supported areas of
# Example role taxonomy, not evidence of applicant experience.
WORK = {
    'data pipelines': r'\b(?:etl|elt|data pipelines?|pipelines? de donn[ée]es|data ingestion|ingestion de donn[ée]es)\b',
    'systems integration': r'\b(?:systems? integration|integrat\w* (?:systems?|apis?)|rest|soap|api integration|int[ée]gration des? syst[èe]mes)\b',
    'data quality': r'\b(?:data quality|data validation|reconciliation|reconcil\w* data|qualit[ée] des donn[ée]es|deduplication)\b',
    'reporting and modelling': r'\b(?:data model\w*|power\s*bi|dashboards?|tableaux de bord|reporting automation)\b',
    'requirements and process automation': r'\b(?:requirements gathering|business requirements|requirements analysis|process automation|automati\w* workflows?|analyse des besoins)\b',
}
TOOLS = r'\b(?:sql|python|pyspark|databricks|alteryx|power\s*bi)\b'


def assess(job):
    job = dict(job)
    title = job.get('title', '')
    text = html.unescape(re.sub('<[^>]+>', ' ', job.get('description', '')))
    systems = bool(re.search(r'systems?|syst[èe]mes?|integration|int[ée]gration|application.*analyst', title, re.I))
    family = ('systems_integration' if systems else 'data_analytics' if re.search(
        r'data|donn[ée]es|analytics|analytique|intelligence|reporting|\bbi\b', title, re.I) else 'adjacent')
    track = 'systems_integration' if systems else 'data_analytics'
    evidence = []
    for label, pattern in WORK.items():
        match = re.search(pattern, text, re.I)
        if match:
            evidence.append({'work': label, 'excerpt': text[max(0, match.start()-70):match.end()+100].strip()})
    tools = sorted({m.group().lower() for m in re.finditer(TOOLS, text, re.I)})
    risks = []
    # A specialist title is a concern even when its JD lists generic SQL/API work.
    if re.search(r'\b(?:SAP|Epic|Guidewire|ServiceNow|Salesforce|Workday|Oracle|cybersecurity)\b', title, re.I):
        risks.append('Specialist platform/domain in title; relevant professional experience not established')
    if re.search(r'\b(?:architect|architecte|responsable|lead|manager|director|staff|principal)\b', title, re.I):
        risks.append('Scope/seniority needs closer comparison with demonstrated work')
    if re.search(r'\b(?:must|required|minimum)\b[^.!?\n]{0,90}\b(?:SAP|Epic|Guidewire|ServiceNow|Salesforce|RCA certification)\b|\b(?:SAP|Epic|Guidewire|ServiceNow|Salesforce)\b[^.!?\n]{0,90}\b(?:required|mandatory)\b', text, re.I):
        risks.append('Potential required specialist platform experience; inspect requirement')
    if len(text.strip()) < 500:
        risks.append('Incomplete description; fit is provisional')
    if re.search(r'\b(?:minimum(?: of)?|at least|au moins)\s+(?:(?:seven|sept)\s*\(7\)|[7-9]|seven|eight|nine|sept|huit|neuf)\+?\s*(?:years?|ans|ann[ée]es)', text, re.I):
        risks.append('Minimum experience may exceed demonstrated tenure; verify before calling this a strong fit')
    score = len(evidence) * 10 + min(len(tools), 3) * 3 + (10 if family != 'adjacent' else 0)
    if re.search(r'\b(?:energy|renewable|electricity|utilities|[ée]nergie)\b', title + ' ' + text, re.I):
        score += 4  # A small domain preference cannot overpower responsibility fit.
    score -= len(risks) * 15
    band = 'strong' if family != 'adjacent' and len(evidence) >= 3 and tools and not risks else 'adjacent'
    location = job.get('location', '')
    country = job.get('country', '').upper()
    workplace = job.get('workplace_type', '').lower()
    ca = country in {'CA', 'CAN', 'CANADA'} or bool(re.search(r'\bCanada\b', location, re.I))
    local = local_office(location)
    remote = workplace == 'remote' or bool(re.search(r'\b(?:remote|t[ée]l[ée]travail)\b', location, re.I))
    geography = 'canada_priority' if (ca and remote) or local else 'secondary_or_unverified'
    # This label never establishes work authorization or eligibility; existing
    # guards still catch Quebec exclusions and US-only/residency restrictions.
    salary_max = job.get('salary_max')
    currency = job.get('salary_currency', '').upper()
    salary_priority = 0
    if salary_max and job.get('salary_interval', 'year') == 'year' and not job.get('salary_estimated'):
        floor = {'CAD': 120000, 'USD': 150000}.get(currency)
        if floor:
            salary_priority = 1 if salary_max >= floor else -1
    return {'version': VERSION, 'family': family, 'resume_track': track, 'band': band,
            'priority_points': score, 'geography': geography, 'evidence': evidence,
            'tools_mentioned': tools, 'concerns': risks, 'salary_priority': salary_priority}


def priority_key(job):
    a = assess(job)
    return (a['geography'] == 'canada_priority', a['band'] == 'strong', a['salary_priority'], a['priority_points'],
            dict(job).get('posted_at') or dict(job).get('created_at') or '')


def resume_focus(job):
    a = assess(job)
    return {'track': a['resume_track'], 'guidance': TRACKS[a['resume_track']],
            'rule': 'Only change wording grounded in the original resume and relevant JD. Never remove skills, change employment titles, combine projects or invent facts. This guidance is emphasis, not evidence.'}


def _key(job):
    # Conservative URL dedupe, not title/company merging of separate vacancies.
    return canonical_url(job.get('canonical_url') or job.get('url') or '') or 'id:' + str(job['id'])


def build_report(repo, days=14, limit=60):
    from .daily import DailyService, candidate_reason
    service = DailyService(repo)
    ready, links, skipped = service.candidates()
    rows = [dict(r) for r in repo.jobs(limit=100000)]
    by_id = {r['id']: r for r in rows}
    with repo.connect() as db:
        submissions = [dict(r) for r in db.execute('SELECT * FROM submissions ORDER BY id')]
        queue = {r['job_id']: dict(r) for r in db.execute('SELECT * FROM daily_queue')}
        attachments = {r['submission_id']: dict(r) for r in db.execute('SELECT * FROM submission_resumes')}
        artifact_rows = [dict(r) for r in db.execute("SELECT job_id,docx_hash,change_report FROM resume_artifacts WHERE state='ready' ORDER BY id")]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        outreach = [dict(r) for r in db.execute('SELECT * FROM outreach_items')] if 'outreach_items' in tables else []
    history = defaultdict(list)
    artifact_tracks = {}
    for artifact in artifact_rows:
        try:
            change = json.loads(artifact['change_report'])
        except (ValueError, TypeError):
            continue
        if change.get('resume_track') in TRACKS:
            artifact_tracks[(artifact['job_id'], artifact['docx_hash'])] = (
                change['resume_track'] if change.get('mode') == 'tailored' else 'original_fallback')
    for s in submissions:
        if s['job_id'] in by_id:
            history[_key(by_id[s['job_id']])].append(s)
    def card(row):
        return {k: row.get(k) for k in ('id', 'title', 'company', 'canonical_url', 'location', 'salary_min',
                'salary_max', 'salary_currency', 'source')} | {'fit': assess(row), 'details_url': f"/jobs/{row['id']}"}
    ranked, seen = [], set()
    for row in sorted(ready + links, key=priority_key, reverse=True):
        key = _key(row)
        # The strategy report additionally protects same-URL source duplicates.
        if key in seen or history[key]:
            continue
        seen.add(key)
        ranked.append(card(row) | {'route': 'automatic' if row in ready else 'manual_link'})
    rescue, reconciliation, seen_rescue = [], [], set()
    for row in sorted(rows, key=priority_key, reverse=True):
        key = _key(row)
        if key in seen_rescue or assess(row)['band'] != 'strong':
            continue
        attempts = history[key]
        if not attempts or any(s['state'] == 'submitted' for s in attempts):
            continue
        if row['status'] not in {'new', 'reviewing'} or candidate_reason(repo.job_input(row['id']), service.policy):
            continue
        seen_rescue.add(key)
        latest = attempts[-1]
        held = any(s['state'] in {'confirmation_unknown', 'captcha_waiting'} for s in attempts) or any(
            queue.get(s['job_id'], {}).get('state') in {'interrupted', 'in_progress', 'running'} for s in attempts)
        attachment = next((attachments[s['id']] | {'submission_id': s['id']} for s in reversed(attempts) if s['id'] in attachments), None)
        item = card(row) | {'blocker': latest['detail'], 'state': latest['state'],
                          'resume_url': f"/submissions/{attachment['submission_id']}/resume" if attachment else None,
                          'resume_note': 'Exact archived attempt resume' if attachment else 'No archived attempt resume; open job to prepare one',
                          'action': 'Reconcile existing attempt first; do not resubmit' if held else 'Check posting and existing attempt before manual completion'}
        (reconciliation if held else rescue).append(item)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = [s for s in submissions if s['attempted_at'] >= cutoff and s['job_id'] in by_id]
    def distinct(items):
        return len({_key(by_id[s['job_id']]) for s in items})
    grouped = {}
    for family in ('systems_integration', 'data_analytics', 'adjacent'):
        cohort = [s for s in recent if assess(by_id[s['job_id']])['family'] == family]
        grouped[family] = {'attempted_jobs': distinct(cohort),
                           'app_recorded_confirmations': distinct([s for s in cohort if s['state'] == 'submitted']),
                           'known_interview_or_offer_jobs': len({_key(by_id[s['job_id']]) for s in cohort if by_id[s['job_id']]['status'] in {'interviewing', 'offer'}})}
    variants = Counter()
    by_resume_track = defaultdict(lambda: {'attempted': set(), 'confirmed': set(), 'interview': set()})
    for s in recent:
        attachment = attachments.get(s['id'])
        track = artifact_tracks.get((s['job_id'], attachment['docx_hash']), 'historical_or_untracked') if attachment else 'historical_or_untracked'
        group = by_resume_track[track]
        key = _key(by_id[s['job_id']])
        group['attempted'].add(key)
        if by_id[s['job_id']]['status'] in {'interviewing', 'offer'}:
            group['interview'].add(key)
        if s['state'] == 'submitted':
            group['confirmed'].add(key)
            variants[attachment['docx_hash'] if attachment else 'no_archived_resume'] += 1
    return {'version': VERSION, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'window_days': days, 'targets': {'confirmed_applications': [40, 60], 'targeted_outreach': [10, 15]},
            'note': 'Targets, not guarantees. Heuristic work-fit priorities, not semantic certification or qualification probabilities. Posting availability not live-checked. Existing eligibility filters remain.',
            'new_candidates': len(ranked), 'strong_candidates': sum(r['fit']['band'] == 'strong' for r in ranked),
            'ranked': ranked[:limit], 'manual_rescue_count': len(rescue), 'manual_rescue': rescue[:10],
            'reconciliation_count': len(reconciliation), 'reconciliation': reconciliation[:10],
            'selection_exclusions': dict(skipped), 'metrics': {
                'attempts': len(recent), 'distinct_attempted_jobs': distinct(recent),
                'app_recorded_confirmations': distinct([s for s in recent if s['state'] == 'submitted']),
                'unconfirmed_jobs': distinct([s for s in recent if s['state'] == 'confirmation_unknown']),
                'by_role_family': grouped, 'confirmed_attempts_by_exact_resume_hash': dict(variants),
                'by_resume_track': {k: {label: len(ids) for label, ids in v.items()} for k, v in by_resume_track.items()},
                'outreach_sent': sum(bool(r['first_contacted_at'] and r['first_contacted_at'] >= cutoff) for r in outreach),
                'confirmation_caveat': 'Uses recorded employer confirmations; not independently reconciled against Gmail. Exact URL dedupe only; different URLs may still be one requisition. Historical resume track unknown; exact file hashes retained.'}}


def prepare_outreach(repo, report, limit=10):
    from .outreach import OutreachQueue
    queue = OutreachQueue(repo)
    selected = [r for r in report['ranked'] + report['manual_rescue'] if r['fit']['band'] == 'strong'
                and r['fit']['geography'] == 'canada_priority' and r['fit']['salary_priority'] >= 0]
    ids = []
    for row in selected:
        item_id = queue.add(row['id'])
        if item_id not in ids:
            ids.append(item_id)
        if len(ids) >= limit:
            break
    return ids  # Drafts only. Contact verification and posting checks precede sending.
