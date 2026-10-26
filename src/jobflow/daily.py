"""One daily intake, selection, application queue, and durable report."""

import fcntl
import json
import os
import re
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from .autopilot import ApplicationProfile, AssistedApplicationService
from .discovery import DiscoveryService
from .dedupe import prefer_canadian_regional_variants
from .eligibility import evaluate, skill_match, cybersecurity_role, application_guard
from .linkedin_alerts import LinkedInAlertImporter
from .linkedin_resolver import LinkedInEmployerResolver
from .providers import configured_providers, public_http_url, prohibited_automation_url, aggregator_url
from .policy import settings
from .policy import local_office, us_office
from .titles import title_review_reason, management_filter_title
from .applications import provider_for_url, application_provider_allowed
from .priority_search import PriorityDiscovery
from .application_budget import application_time_limit, ApplicationTimeLimit


def now():
    return datetime.now(timezone.utc).isoformat()


def init_daily(repo):
    with repo.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS daily_runs (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL, completed_at TEXT,
            state TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS daily_queue (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), run_id INTEGER NOT NULL,
            state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        ''')


def candidate_reason(job, policy):
    """Recheck current criteria; unknown sponsorship is answered truthfully on the form."""
    guard = application_guard(job)
    if guard:
        return guard
    if title_review_reason(job.title, job.description):
        return title_review_reason(job.title, job.description)
    if re.search(r'\b(?:El Salvador|Brazil|India|Mexico|Europe|EMEA|LATAM|Argentina|Chile|Australia|United Kingdom)\b', job.location, re.I) and not re.search(r'Canada|United States|North America|Worldwide|Global', job.location, re.I):
        return 'Outside approved geography'
    if job.country.upper() not in {'CA', 'CAN', 'CANADA', 'US', 'USA', 'UNITED STATES'} and not (
        local_office(job.location) or us_office(job.location) or re.search(
            r'\b(?:Canada|United States|USA|US|Worldwide|Global|North America)\b', job.location, re.I)
    ):
        return 'Country eligibility needs evidence'
    decision = evaluate(job, ignore_salary=policy['ignore_salary'])
    reasons = list(decision.reasons)
    if policy['ignore_salary']:
        reasons = [reason for reason in reasons if not reason.startswith('maximum annual base')]
    if reasons:
        return '; '.join(reasons)
    if any('remote or office status' in warning or 'security-clearance' in warning for warning in decision.warnings):
        return 'Workplace or clearance evidence needs review'
    if any(term in management_filter_title(job.title).lower() for term in ('data scientist', 'lead', 'manager', 'director', 'gtm', 'go-to-market')):
        return 'Excluded role'
    if len(job.description.strip()) < 500:
        return 'Full job description needs review'
    if skill_match(job).get('percent', 0) < policy['minimum_skill_match']:
        return 'Insufficient resume skill overlap'
    if not public_http_url(job.canonical_url) or prohibited_automation_url(job.canonical_url) or aggregator_url(job.canonical_url):
        return 'Verified employer URL needed'
    return ''


class DailyService:
    def __init__(self, repo):
        self.repo = repo
        self.policy = settings()
        init_daily(repo)
        LinkedInEmployerResolver(repo)

    def candidates(self, targets=None, remaining=False, approved_job_ids=(), approved_only=False):
        ready, links, skipped = [], [], Counter()
        # Scoped runs use only the reviewed company manifest. Punctuation and
        # spacing may differ in stored feeds; substrings/brand guesses may not.
        normalize_company = lambda text: re.sub(r'[^a-z0-9]', '', text.casefold())
        target_names = {normalize_company(t['company']) for t in targets} if targets is not None else None
        target_sources = {t['provider'] for t in targets if t['provider']} if targets is not None else set()
        with self.repo.connect() as db:
            attempted = {row[0] for row in db.execute('SELECT DISTINCT job_id FROM submissions')}
            retried = {row[0] for row in db.execute("SELECT job_id FROM daily_queue WHERE state='ready' AND detail='Explicitly requeued: pre-submit blocker'")}
            unsafe = {row[0] for row in db.execute("SELECT DISTINCT job_id FROM submissions WHERE state IN ('submitted','confirmation_unknown','captcha_waiting')")}
            attempted -= retried - unsafe
            queued = {row[0] for row in db.execute("SELECT job_id FROM daily_queue WHERE state != 'ready'")}
            mappings = list(db.execute("SELECT linkedin_job_id,employer_job_id FROM linkedin_employer_matches WHERE state='matched'"))
            resolved_alerts = {r[0] for r in mappings}
            # Preserve attempt/status history across the alert and its employer record.
            tracked_alerts = {r[0] for r in db.execute("SELECT id FROM jobs WHERE source='linkedin' AND status NOT IN ('new','reviewing')")}
            attempted.update(r[1] for r in mappings if r[0] in attempted or r[0] in tracked_alerts)
        stored_jobs = self.repo.jobs(limit=100000)
        for row in stored_jobs:
            if approved_only and row['id'] not in approved_job_ids:
                continue
            if target_names is not None and normalize_company(row['company']) not in target_names:
                skipped['outside target-company list'] += 1
                continue
            if row['status'] not in {'new', 'reviewing'} or row['id'] in attempted or row['id'] in queued:
                skipped['already tracked or attempted'] += 1
                continue
            source = row['source']
            approved = row['id'] in approved_job_ids
            if remaining and not approved and not (
                provider_for_url(row['canonical_url']) in {'ashby', 'greenhouse'} or
                (row['country'].upper() in {'CA', 'CAN', 'CANADA'} and row['workplace_type'].lower() == 'remote')
            ):
                skipped['outside remaining ATS or Canada-remote scope'] += 1
                continue
            if not approved and cybersecurity_role(row['title'], row['description']):
                skipped['cybersecurity-related role excluded'] += 1
                continue
            if not remaining and source not in set(self.policy['discovery_sources'] + ['linkedin', 'linkedin_employer', 'employer_link', 'priority_workday', 'priority_careers']) | target_sources:
                continue
            if source == 'linkedin':
                # LinkedIn is discovery-only. Unresolved links never enter an application queue.
                skipped['LinkedIn mapped to employer' if row['id'] in resolved_alerts else 'LinkedIn employer URL unresolved'] += 1
                continue
            reason = candidate_reason(self.repo.job_input(row['id']), self.policy)
            if reason and not approved:
                skipped[reason] += 1
                continue
            if not application_provider_allowed(row, self.policy):
                skipped['application provider disabled by user'] += 1
                if not remaining and not approved_only:
                    links.append(dict(row))
                continue
            # Explicit target-batch approval allows the common form adapter for
            # target feeds, without changing daily automatic/manual preferences.
            (ready if remaining or targets is not None or source in self.policy['automatic_sources'] else links).append(dict(row))
        protected = [dict(row) for row in stored_jobs if row['id'] in unsafe or row['status'] in
                     {'preparing', 'submitted', 'interviewing', 'offer'}]
        if approved_only:
            retained, self.regional_alternatives = ready + links, []
        else:
            retained, self.regional_alternatives = prefer_canadian_regional_variants(ready + links, protected)
        retained_ids = {row['id'] for row in retained}
        ready = [row for row in ready if row['id'] in retained_ids]
        links = [row for row in links if row['id'] in retained_ids]
        if self.regional_alternatives:
            skipped['regional alternative; Canadian posting preferred'] += len(self.regional_alternatives)
        from .strategy import priority_key
        ready.sort(key=priority_key, reverse=True)
        links.sort(key=priority_key, reverse=True)
        return ready, links, skipped

    def run_targets(self, manifest, confirm=False, limit=50, remaining=False, approved_job_ids=(),
                    approved_only=False, continue_on_holds=False, continue_on_errors=False):
        """Use the shared queue for only this list; no discovery or retries."""
        from .company_coverage import load_targets
        continue_on_holds = continue_on_holds or self.policy.get('skip_captchas', False)
        if remaining and manifest is not None:
            raise ValueError('Choose target-company or remaining-applications scope, not both')
        targets = None if remaining else load_targets(manifest)
        if not 1 <= limit <= 500:
            raise ValueError('Target application limit must be between 1 and 500')
        if approved_only and (not remaining or not approved_job_ids):
            raise ValueError('An exact approved batch requires explicit job IDs and remaining scope')
        ready, _, skipped = self.candidates(targets=targets, remaining=remaining, approved_job_ids=approved_job_ids,
                                            approved_only=approved_only)
        if approved_only:
            # Stable user-approved order, not a fit-score ordering or a new search.
            ordering = {job_id: index for index, job_id in enumerate(approved_job_ids)}
            ready.sort(key=lambda job: ordering[job['id']])
        report = {'state': 'preview', 'manifest': str(manifest),
                  'company_count': len(targets) if targets is not None else None, 'ready': len(ready),
                  'scope': 'exact_approved_batch' if approved_only else 'remaining_ashby_greenhouse_canada_remote' if remaining else 'target_companies',
                  'continue_on_holds': continue_on_holds,
                  'continue_on_errors': continue_on_errors,
                  'explicitly_approved_job_ids': list(approved_job_ids),
                  'selected_ids': [j['id'] for j in ready[:limit]],
                  'candidates': [{'id': j['id'], 'company': j['company'], 'title': j['title']} for j in ready[:limit]],
                  'filtered': dict(skipped), 'outcomes': []}
        if not confirm:
            return report
        os.environ['JOBFLOW_APPLICATION_AI_ENABLED'] = 'false'
        with (self.repo.path.parent / 'daily.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**report, 'state': 'already_running'}
            # Recheck under the same lock all other application workers use.
            current, _, _ = self.candidates(targets=targets, remaining=remaining, approved_job_ids=approved_job_ids,
                                             approved_only=approved_only)
            current_by_id = {j['id']: j for j in current}
            ready = [current_by_id[job_id] for job_id in report['selected_ids'] if job_id in current_by_id]
            run_id = -time.time_ns()
            path = self.repo.path.parent / ('target-applications-' + str(-run_id) + '.json')
            report.update(state='running', run_id=run_id, report_path=str(path))
            def save():
                report['updated_at'] = now()
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(report, indent=2) + '\n')
                temporary.replace(path)
            save()
            if not ready:
                report['state'] = 'completed'
                save()
                return report
            resume = Path(self.policy['resume']).resolve()
            if not resume.is_file():
                raise ValueError('Configured shared resume is missing')
            profile = ApplicationProfile.load(Path(self.policy['profile']))
            if approved_only:
                # Waiving selection does not make an unknown work country
                # Canada. Unspecified authorization questions must stay unknown.
                profile.values['require_job_country_evidence'] = True
            profile.values.setdefault('workplace_preferences', {})['onsite_or_hybrid_locations'] = (
                self.policy['canadian_office_locations'] + self.policy['us_office_locations'])
            # Reuse normal Chrome; do not launch a separate application browser.
            port = (Path(self.policy['browser_profile']) / 'DevToolsActivePort').read_text().splitlines()[0]
            if not port.isdigit():
                raise ValueError('Invalid application Chrome port')
            os.environ['JOBFLOW_EXISTING_CHROME_ENDPOINT'] = 'http://127.0.0.1:' + port
            service = AssistedApplicationService(self.repo, Path(self.policy['browser_profile']))
            restricted_sites = set()
            for job in ready:
                host = (urlsplit(job['canonical_url']).hostname or '').lower()
                restriction_key = 'ashbyhq.com' if host.endswith('.ashbyhq.com') or host == 'ashbyhq.com' else host
                if restriction_key in restricted_sites:
                    detail = 'Site restriction encountered earlier in this pass; no bypass or further attempts on this site'
                    self._queue(job['id'], run_id, 'site_restriction_held', detail)
                    report['outcomes'].append({'id':job['id'], 'company':job['company'],
                        'title':job['title'], 'state':'site_restriction_held', 'detail':detail})
                    save()
                    continue
                with self.repo.connect() as db:
                    protected = db.execute("""SELECT 1 FROM jobs j
                        LEFT JOIN submissions s ON s.job_id=j.id
                        LEFT JOIN daily_queue q ON q.job_id=j.id
                        WHERE j.canonical_url=? AND (j.status IN ('submitted','interviewing','offer')
                        OR s.state IN ('submitted','confirmation_unknown','captcha_waiting')
                        OR q.state IN ('attempting','submitted','confirmation_unknown','captcha_waiting')) LIMIT 1""",
                        (job['canonical_url'],)).fetchone()
                if protected:
                    report['outcomes'].append({'id': job['id'], 'state': 'held', 'detail': 'Protected submission or interrupted claim'})
                    save()
                    continue
                self._queue(job['id'], run_id, 'attempting', 'Explicit target-company run; saved answers only')
                report['current'] = job['id']
                save()
                print('Applying: {} — {} (#{}).'.format(job['company'], job['title'], job['id']), flush=True)
                try:
                    with application_time_limit(self.policy.get('application_attempt_seconds', 900)):
                        decision = service.run(job['id'], resume, profile, self.policy['captcha_wait_seconds'],
                                           override_eligibility=True, native_chrome=True, auto_submit=True,
                                           skip_captcha_wait=continue_on_holds,
                                           explicitly_approved_job_ids=tuple(approved_job_ids))
                except ApplicationTimeLimit:
                    # Never turn a timeout after a submit click into a retry.
                    saved = self.repo.submissions(job['id'])
                    confirmed = next((s for s in saved if s['state'] == 'submitted'), None)
                    states = {s['state'] for s in saved}
                    state = ('submitted' if confirmed else 'captcha_waiting' if 'captcha_waiting' in states
                             else 'confirmation_unknown' if 'confirmation_unknown' in states else 'interrupted')
                    detail = ('Employer confirmation saved before time limit' if confirmed else
                              'Application time limit reached; attempt protected from retry; moving to next job')
                    self._queue(job['id'], run_id, state, detail)
                    report['outcomes'].append({'id':job['id'], 'company':job['company'],
                        'title':job['title'], 'state':state, 'detail':detail,
                        'receipt':confirmed['receipt'] if confirmed else ''})
                    report.pop('current', None)
                    print(state + ': ' + detail, flush=True)
                    save()
                    if state == 'captcha_waiting' and not continue_on_holds:
                        report['state'] = 'held_for_reconciliation'; save(); return report
                    continue
                except Exception as exc:
                    if not continue_on_errors:
                        report['state'] = 'interrupted'
                        save()
                        raise
                    # The point of failure may be after a submit click. Preserve
                    # the claim and prohibit retries, while allowing other jobs.
                    detail = type(exc).__name__ + ': interrupted attempt needs reconciliation; no automatic retry'
                    self._queue(job['id'], run_id, 'interrupted', detail)
                    report['outcomes'].append({'id': job['id'], 'company': job['company'],
                        'title': job['title'], 'state': 'interrupted', 'detail': detail})
                    report.setdefault('interrupted_jobs', []).append(job['id'])
                    report.pop('current', None)
                    print('interrupted: ' + detail, flush=True)
                    save()
                    continue
                except BaseException:
                    report['state'] = 'interrupted'
                    save()  # Keep claim and current id; never blindly retry.
                    raise
                self._queue(job['id'], run_id, decision.state, decision.reason)
                report['outcomes'].append({'id': job['id'], 'company': job['company'], 'title': job['title'],
                                          'state': decision.state, 'detail': decision.reason, 'receipt': decision.receipt})
                if re.search(r'flagged as possible spam|flagged as spam|ACCESS_RESTRICTION|HTTP (?:403|429)\b', decision.reason, re.I):
                    restricted_sites.add(restriction_key)
                    report['restricted_sites'] = sorted(restricted_sites)
                report.pop('current', None)
                print(decision.state + ': ' + decision.reason, flush=True)
                # Uncertain submissions remain protected per job by the queue
                # and submission history. They must not block unrelated jobs.
                # CAPTCHA is different: pause so the user can complete it.
                if decision.state == 'captcha_waiting' and not continue_on_holds:
                    report['state'] = 'held_for_reconciliation'
                    save()
                    return report
                save()
            report['state'] = 'completed'
            save()
            return report

    def _save(self, run_id, report, state='running'):
        report['state'] = state
        with self.repo.connect() as db:
            db.execute('UPDATE daily_runs SET state=?, report=?, completed_at=? WHERE id=?',
                       (state, json.dumps(report), None if state == 'running' else now(), run_id))

    def _queue(self, job_id, run_id, state, detail=''):
        with self.repo.connect() as db:
            db.execute('INSERT INTO daily_queue VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET '
                       'run_id=excluded.run_id,state=excluded.state,detail=excluded.detail,updated_at=excluded.updated_at',
                       (job_id, run_id, state, detail, now()))

    def run(self, preview=False):
        os.environ['JOBFLOW_APPLICATION_AI_ENABLED'] = 'false'
        if preview:
            ready, links, skipped = self.candidates()
            return {'state': 'preview', 'ready': len(ready), 'manual_links': len(links),
                    'regional_alternatives': self.regional_alternatives,
                    'candidates': [{'id': j['id'], 'title': j['title'], 'company': j['company']} for j in ready[:50]],
                    'filtered': dict(skipped)}
        lock_path = self.repo.path.parent / 'daily.lock'
        with lock_path.open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'state': 'already_running'}
            day = datetime.now(ZoneInfo(self.policy['timezone'])).date().isoformat()
            with self.repo.connect() as db:
                previous = db.execute('SELECT * FROM daily_runs WHERE day=?', (day,)).fetchone()
                if previous:
                    result = json.loads(previous['report'])
                    if previous['state'] == 'running':
                        # Lock is free: the former worker stopped. Never re-send its applications.
                        db.execute("UPDATE daily_runs SET state='interrupted',completed_at=? WHERE id=?", (now(), previous['id']))
                        result['state'] = 'interrupted'
                    else:
                        result['state'] = 'already_ran'
                    return result
                run_id = db.execute("INSERT INTO daily_runs(day,started_at,state) VALUES (?,?,'running')", (day, now())).lastrowid
            report = {'run_id': run_id, 'day': day, 'errors': [], 'outcomes': [], 'links': [], 'sources': {}}
            self._save(run_id, report)
            try:
                resume = Path(self.policy['resume']).resolve()
                profile = ApplicationProfile.load(Path(self.policy['profile']))
                if not resume.is_file():
                    raise ValueError('Configured resume is missing')
                profile.values.setdefault('workplace_preferences', {})['onsite_or_hybrid_locations'] = (
                    self.policy['canadian_office_locations'] + self.policy['us_office_locations'])
                try:
                    imported = LinkedInAlertImporter(self.repo).run(14)
                    report['linkedin'] = asdict(imported)
                except Exception as exc:
                    report['errors'].append('Gmail import: ' + type(exc).__name__)
                os.environ['JOBFLOW_DISCOVERY_SOURCES'] = ','.join(self.policy['discovery_sources'])
                providers = [p for p in configured_providers(known_jobs=self.repo.jobs(limit=100000))
                             if p.name in self.policy['discovery_sources']]
                if not providers:
                    report['errors'].append('No ATS company registries configured')
                for provider in providers:
                    print('Collecting ' + provider.name, flush=True)
                    discovery_id = DiscoveryService(self.repo, [provider], reevaluate_stored=False).run()
                    discovery = dict(self.repo.discovery_run(discovery_id))
                    report['sources'][provider.name] = discovery
                    if discovery['error_summary']:
                        report['errors'].append(provider.name + ': ' + discovery['error_summary'])
                    self._save(run_id, report)
                if self.policy.get('priority_company_registry'):
                    focused = PriorityDiscovery(self.repo).run()
                    report['priority_search'] = {key: focused[key] for key in ('sources', 'shortlist', 'review')}
                    self._save(run_id, report)
                try:
                    report['linkedin_employer_resolution'] = LinkedInEmployerResolver(self.repo).run()
                except Exception as exc:
                    # A malformed external page must not block independent ATS jobs.
                    # Unresolved LinkedIn alerts remain excluded by candidates().
                    report['errors'].append('LinkedIn employer resolution: ' + type(exc).__name__)
                ready, links, filtered = self.candidates()
                report['ready'] = len(ready)
                report['regional_alternatives'] = self.regional_alternatives
                report['filtered'] = dict(filtered)
                report['links'] = [{'title': j['title'], 'company': j['company'], 'url': j['canonical_url'] or j['url'],
                                    'source': j['source']} for j in links[:50]]
                for job in links[:50]:
                    self._queue(job['id'], run_id, 'manual_link', 'Listed in daily report; user applies directly')
                for job in ready:
                    self._queue(job['id'], run_id, 'ready')
                self._save(run_id, report)
                if self.policy.get('application_approval_required'):
                    report['awaiting_approval'] = len(ready)
                    self._save(run_id, report, 'awaiting_approval')
                    return report
                service = AssistedApplicationService(self.repo, Path(self.policy['browser_profile']))
                for job in ready[:self.policy['maximum_attempts']]:
                    # Claim BEFORE interacting with a form: crashes cannot produce blind retries.
                    self._queue(job['id'], run_id, 'attempting')
                    print('Applying: {} — {} (#{}).'.format(job['company'], job['title'], job['id']), flush=True)
                    decision = service.run(job['id'], resume, profile, self.policy['captcha_wait_seconds'],
                                           override_eligibility=True, native_chrome=True, auto_submit=True)
                    self._queue(job['id'], run_id, decision.state, decision.reason)
                    report['outcomes'].append({'id': job['id'], 'company': job['company'], 'title': job['title'],
                                               'state': decision.state, 'detail': decision.reason, 'receipt': decision.receipt})
                    self._save(run_id, report)
                self._save(run_id, report, 'partial' if report['errors'] else 'completed')
            except Exception as exc:
                report['errors'].append(type(exc).__name__ + ': daily pipeline failed; inspect local configuration')
                self._save(run_id, report, 'failed')
                raise
            return report

    def status(self):
        with self.repo.connect() as db:
            run = db.execute('SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1').fetchone()
            queue = [dict(row) for row in db.execute('SELECT q.*,j.title,j.company FROM daily_queue q JOIN jobs j ON j.id=q.job_id ORDER BY q.updated_at DESC LIMIT 200')]
        return {'latest': dict(run) if run else None, 'queue': queue, 'policy': self.policy}

    def requeue(self, job_id):
        """Explicit retry only for proven pre-submit failures; keep all history."""
        with self.repo.connect() as db:
            rows = db.execute('SELECT state,detail FROM submissions WHERE job_id=? ORDER BY id DESC', (job_id,)).fetchall()
            queue = db.execute('SELECT run_id FROM daily_queue WHERE job_id=?', (job_id,)).fetchone()
        if not rows or not queue or any(r['state'] in {'submitted', 'confirmation_unknown', 'captcha_waiting'} for r in rows):
            raise ValueError('Cannot requeue a submitted, uncertain, or untracked application')
        if rows[0]['state'] != 'manual_action_required' or not rows[0]['detail'].startswith((
            'Required questions need saved answers:', 'Skipped unanswered required questions:',
            'Required fields not completed:',
            'Application form was not found in regular Chrome',
            'Employer account sign-in or registration is required; application form not reached',
            'No unique application Next/Continue or Submit control found',
            'Application step did not advance: ; ; Required field',
            'Next application step could not be recognized; draft retained',
            'LOGIN_RETRY_PENDING: account action control was not recognized',
            'Regular Chrome handoff ended: BrowserType.connect_over_cdp: Timeout',
            'Regular Chrome handoff ended: Locator.count: Frame was detached')):
            raise ValueError('No proven pre-submit blocker to retry')
        self._queue(job_id, queue['run_id'], 'ready', 'Explicitly requeued: pre-submit blocker')
        return {'job_id': job_id, 'state': 'ready', 'detail': 'Eligible for screening in the next daily run'}
