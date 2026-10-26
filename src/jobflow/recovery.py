"""Recover audit candidates through live public providers and normal discovery.

An audit is a locator, never authoritative posting content. No applications,
search API calls, model calls, or changes to submission history occur here.
"""
from collections import Counter
from dataclasses import fields
from datetime import datetime, timezone

from .daily import candidate_reason
from .dedupe import canonical_url
from .discovery import DiscoveryService, excluded_job_urls
from .models import JobInput, Compensation
from .policy import settings
from .providers import GreenhouseProvider, LeverProvider, AshbyProvider
from .smartrecruiters import SmartRecruitersProvider


PROVIDERS = {'greenhouse': GreenhouseProvider, 'lever': LeverProvider, 'ashby': AshbyProvider,
             'smartrecruiters': SmartRecruitersProvider}


def job_input(item):
    value = {f.name: item[f.name] for f in fields(JobInput) if f.name in item}
    if isinstance(value.get('compensation'), dict):
        value['compensation'] = Compensation(**value['compensation'])
    return JobInput(**value)


def identity(job):
    return job.source, str(job.external_id or ''), canonical_url(job.canonical_url or job.url)


def known_job(repo, job):
    source, external, url = identity(job)
    with repo.connect() as db:
        return db.execute(
            'SELECT id,status FROM jobs WHERE (source=? AND external_id=? AND external_id != ?) '
            'OR canonical_url=? OR url=? ORDER BY id LIMIT 1',
            (source, external, '', url, url)).fetchone()


class RecoveryProvider:
    """Only emit targeted, live, policy-passing jobs to DiscoveryService."""
    def __init__(self, provider, targets, repo, policy):
        self.provider, self.targets, self.repo, self.policy = provider, targets, repo, policy
        self.name = provider.name
        self.results = {}

    @property
    def board_results(self):
        return self.provider.board_results

    def fetch(self):
        by_external = {str(j['external_id']): j for j in self.targets if j.get('external_id')}
        by_url = {canonical_url(j.get('canonical_url') or j['url']): j for j in self.targets}
        for live in self.provider.fetch():
            target = by_external.get(str(live.external_id or '')) or by_url.get(identity(live)[2])
            if not target:
                continue
            target_url = canonical_url(target.get('canonical_url') or target['url'])
            if target_url in self.results:
                continue
            result = {'title': live.title, 'company': live.company, 'url': live.canonical_url or live.url,
                      'source': live.source, 'state': 'live_candidate', 'reason': ''}
            self.results[target_url] = result
            existing = known_job(self.repo, live)
            if existing:
                result.update(state='already_stored', job_id=existing['id'], status=existing['status'])
                continue
            reason = candidate_reason(live, self.policy)
            if reason:
                result.update(state='no_longer_passes', reason=reason)
                continue
            yield live


def recover_candidates(repo, report, provider_factory=None):
    """Import missing candidates only after live feed and current policy checks."""
    if report.get('source_report_state') != 'completed':
        raise ValueError('Recovery requires a completed policy-comparison audit')
    policy = settings()
    excluded = {canonical_url(url) for url in excluded_job_urls()}
    factory = provider_factory or (lambda source, boards: PROVIDERS[source](boards, prefilter=False))
    groups, outcomes, seen = {}, [], set()
    for item in report['results']:
        if not item.get('after_pass') or item.get('stored_job'):
            continue
        job = job_input(item)
        key = identity(job)
        if key in seen:
            continue
        seen.add(key)
        base = {'title': job.title, 'company': job.company, 'url': job.canonical_url or job.url,
                'source': job.source}
        if any(canonical_url(url) in excluded for url in (job.url, job.canonical_url) if url):
            outcomes.append(dict(base, state='excluded_by_user'))
            continue
        existing = known_job(repo, job)
        if existing:
            outcomes.append(dict(base, state='already_stored', job_id=existing['id'], status=existing['status']))
            continue
        reason = candidate_reason(job, policy)
        if reason:
            outcomes.append(dict(base, state='audit_no_longer_passes', reason=reason))
            continue
        token = item.get('board_token')
        if job.source not in PROVIDERS or not isinstance(token, str) or not token.strip():
            outcomes.append(dict(base, state='unsupported_locator'))
            continue
        groups.setdefault((job.source, token), []).append(item)
    runs = []
    for (source, token), targets in groups.items():
        provider = factory(source, {targets[0]['company']: token})
        wrapper = RecoveryProvider(provider, targets, repo, policy)
        # Use the same storage/deduplication/evaluation path as ordinary discovery.
        # Never re-evaluate or reset already tracked jobs during this catch-up.
        run_id = DiscoveryService(repo, [wrapper], reevaluate_stored=False).run()
        run = dict(repo.discovery_run(run_id))
        runs.append({'id': run_id, 'source': source, 'board': token, 'state': run['state'],
                     'error': run['error_summary']})
        board_ok = bool(wrapper.board_results) and all(r['state'] == 'completed' for r in wrapper.board_results)
        for target in targets:
            url = canonical_url(target.get('canonical_url') or target['url'])
            result = wrapper.results.get(url)
            if result is None:
                result = {'title': target['title'], 'company': target['company'], 'url': url,
                          'source': source, 'state': 'not_in_live_feed' if board_ok and not run['error_summary'] else 'source_unverified'}
            elif result['state'] == 'live_candidate':
                # Confirm persistence rather than reporting a yielded record as imported.
                stored = known_job(repo, job_input(target))
                result.update(state='imported' if stored else 'persistence_unverified')
                if stored:
                    result['job_id'] = stored['id']
            outcomes.append(result)
    return {'completed_at': datetime.now(timezone.utc).isoformat(),
            'counts': dict(Counter(row['state'] for row in outcomes)), 'results': outcomes,
            'discovery_runs': runs, 'applications_submitted': 0, 'paid_search_calls': 0,
            'model_calls': 0}
