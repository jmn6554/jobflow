"""Explicit employer coverage: discovery health is not submission readiness.

Read-only audit by default. No browser, account creation, AI, or submissions.
Unknown companies stay in the report instead of silently disappearing.
"""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .applications import provider_for_url, SUPPORTED_ATS_HOSTS
from .eligibility import evaluate
from .providers import GreenhouseProvider, LeverProvider, AshbyProvider
from .titles import relevant_title
from .policy import settings
from .workable import WorkableProvider
from .public_boards import BambooHRProvider, RipplingProvider
from .scoped_ashby import ScopedAshbyProvider
from .kula import KulaProvider
from .teamtailor import TeamtailorProvider
from .workday import WorkdayProvider
from .icims_public import ICIMSPublicProvider
from .taleo_public import TaleoPublicProvider
from .react_router_board import ReactRouterBoardProvider
from .smartrecruiters import SmartRecruitersProvider


FACTORIES = {'greenhouse': GreenhouseProvider, 'lever': LeverProvider, 'ashby': AshbyProvider,
             'workable': WorkableProvider, 'bamboohr': BambooHRProvider, 'rippling': RipplingProvider,
             'ashby_scoped': ScopedAshbyProvider, 'kula': KulaProvider,
             'teamtailor': TeamtailorProvider, 'workday': WorkdayProvider,
             'icims_public': ICIMSPublicProvider, 'taleo_public': TaleoPublicProvider,
             'react_router_board': ReactRouterBoardProvider,
             'smartrecruiters': SmartRecruitersProvider}
DEFAULT_MANIFEST = 'config/canada-remote-targets.json'


def load_targets(path=DEFAULT_MANIFEST):
    rows = json.loads(Path(path).read_text())['companies']
    names, boards = set(), set()
    for row in rows:
        name = row['company'].casefold()
        if not name or name in names:
            raise ValueError('Target company names must be unique and nonempty')
        names.add(name)
        if row['provider']:
            key = (row['provider'], row['board_token'].casefold())
            if not row['board_token'] or key in boards:
                raise ValueError('Target boards must be explicit and unique')
            boards.add(key)
    return rows


def target_providers(rows, client=None):
    """Only exact reviewed mappings; never infer boards from substrings."""
    result = []
    for source, factory in FACTORIES.items():
        boards = {r['company']: r['board_token'] for r in rows if r['provider'] == source}
        if boards:
            if source == 'ashby_scoped':
                listing_urls = {r['company']: r['listing_url'] for r in rows if r['provider'] == source}
                result.append(factory(boards, listing_urls, client=client))
            elif source == 'teamtailor':
                sources = [r for r in rows if r['provider'] == source]
                result.append(factory(boards, {r['company']: r['careers_url'] for r in sources},
                                      {r['company']: r['api_root'] for r in sources}, client=client))
            elif source == 'workday':
                result.append(factory(boards, client=client))
            else:
                result.append(factory(boards, client=client, prefilter=False))
    return result


def audit_targets(repo, rows, providers=None):
    owns_clients = providers is None
    providers = target_providers(rows) if providers is None else providers
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'applications_started': 0,
              'companies': [], 'note': 'Existing form adapter is not an end-to-end success guarantee.'}
    evidence, postings = {}, {}
    for provider in providers:
        try:
            for job in provider.fetch():
                postings.setdefault(job.company, []).append(job)
            for board in provider.board_results:
                evidence[(provider.name, board['board_token'].casefold())] = dict(board)
        except Exception as exc:
            for company, token in getattr(provider, 'boards', getattr(provider, 'sites', {})).items():
                evidence[(provider.name, token.casefold())] = {'state': 'failed', 'error': type(exc).__name__}
        finally:
            if owns_clients and hasattr(provider, 'client'):
                provider.client.close()
    with repo.connect() as conn:
        confirmed = list(conn.execute("SELECT j.company, COUNT(DISTINCT s.job_id) AS n FROM submissions s JOIN jobs j ON j.id=s.job_id WHERE s.state='submitted' GROUP BY j.company"))
    normalize = lambda name: re.sub(r'[^a-z0-9]', '', name.lower())
    for row in rows:
        board = evidence.get((row['provider'], row['board_token'].casefold()))
        jobs = postings.get(row['company'], [])
        candidates = []
        for job in jobs:
            if not relevant_title(job.title):
                continue
            decision = evaluate(job, ignore_salary=settings()['ignore_salary'])
            candidates.append({'title': job.title, 'url': job.canonical_url or job.url,
                               'location': job.location, 'queue': decision.queue,
                               'reasons': list(decision.reasons), 'warnings': list(decision.warnings),
                               'form_route': provider_for_url(job.canonical_url or job.url)})
        successes = sum(r['n'] for r in confirmed if normalize(r['company']) == normalize(row['company']))
        report['companies'].append({**row,
            'discovery_state': board['state'] if board else 'integration_missing',
            'board_evidence': board,
            'target_title_count': len(candidates),
            'non_rejected_count': sum(j['queue'] != 'rejected' for j in candidates),
            'confirmed_historical_submissions': successes,
            'application_state': ('historical_confirmation_not_current_form_verification' if successes
                                  else 'existing_adapter_unverified_for_employer' if row.get('application_provider', row['provider']) in SUPPORTED_ATS_HOSTS
                                  else 'employer_route_unresolved'),
            'jobs': candidates})
    report['summary'] = dict(Counter(r['discovery_state'] for r in report['companies']))
    return report


def write_report(report, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    lines = ['# Canada remote target integration coverage', '', report['note'], '',
             'This is a read-only audit. No applications were started.', '',
             '| Company | Discovery | Target titles | Not rejected* | Historical confirmations |',
             '|---|---|---:|---:|---:|']
    for r in report['companies']:
        lines.append('| {} | {} | {} | {} | {} |'.format(r['company'], r['discovery_state'],
                     r['target_title_count'], r['non_rejected_count'], r['confirmed_historical_submissions']))
    lines += ['', '*Not rejected includes evidence-review jobs; it does not mean ready to apply.',
              'All current geography, language, role and submission holds still apply.',
              'See the JSON report for board failures and per-job evidence.']
    output.with_suffix('.md').write_text('\n'.join(lines) + '\n')
