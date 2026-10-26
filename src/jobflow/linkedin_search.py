"""Saved LinkedIn search plan and deduplicated employer-link shortlist; never apply."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .dedupe import application_posting_keys
from .linkedin_browser import LinkedInBrowserCollector, card_reason, employer_url
from .linkedin_application_sites import application_site

CONFIG = Path(__file__).resolve().parents[2] / 'config/linkedin-search.json'


def search_plan(config=None):
    config = config or json.loads(CONFIG.read_text())
    if config.get('applications_enabled'):
        raise ValueError('LinkedIn search is discovery-only')
    exclusions = ' NOT (' + ' OR '.join('"' + phrase + '"' for phrase in config.get('excluded_search_phrases', [])) + ')' if config.get('excluded_search_phrases') else ''
    return [dict(name=place['name'] + ' — ' + keywords, url='https://www.linkedin.com/jobs/search/?' + urlencode({
        'keywords': keywords + exclusions, 'location': place['location'], 'geoId': place['geo_id'], 'f_WT': place['workplace_types'],
        **({'f_TPR': 'r' + str(int(config['posted_within_days']) * 86400)}
           if config.get('posted_within_days') else {}),
        'f_JT': 'F', 'sortBy': config['sort_by'],
    })) for place in config['locations'] for keywords in config['keyword_groups']]


def shortlist(repo, ids, config=None, deferred=None):
    config = json.loads(CONFIG.read_text()) if config is None else config
    with repo.connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM linkedin_browser_jobs') if row['external_id'] in ids]
        protected = list(db.execute("SELECT DISTINCT j.company,j.title,j.canonical_url,j.url FROM jobs j JOIN submissions s ON j.id=s.job_id WHERE s.state IN ('submitted','confirmation_unknown','captcha_waiting')"))
    protected_keys = set().union(*(application_posting_keys(row) for row in protected))
    result, seen, held = [], set(), 0
    for row in rows:
        if row['state'] != 'employer_link_found' or card_reason(row) or not employer_url(row['employer_url']):
            continue
        keys = application_posting_keys({'url': row['employer_url'], 'company': row['company']})
        if keys & protected_keys:
            held += 1
            continue
        if keys & seen:
            continue
        seen.update(keys)
        item = {k: row[k] for k in ('external_id','title','company','location','linkedin_url','employer_url','reason')}
        route = application_site(row['employer_url'], config)
        item['application_site'] = route
        if not route['preferred']:
            if deferred is not None:
                deferred.append(item)
            continue
        result.append(item)
    order = config.get('application_site_focus', {}).get('preferred_providers', [])
    result.sort(key=lambda row: order.index(row['application_site']['provider'])
                if row['application_site']['provider'] in order else len(order))
    return result, held, dict(Counter(row['state'] for row in rows))


def save_report(repo, report):
    """Recompute the shortlist under current preferences, then export phone-friendly links."""
    ids = {job_id for search in report['searches'] for job_id in search['result'].get('job_ids', [])}
    report['other_application_sites'] = []
    report['shortlist'], report['already_applied_or_held'], report['counts'] = shortlist(
        repo, ids, deferred=report['other_application_sites'])
    report['unique_postings'] = len(ids)
    (repo.path.parent / 'linkedin-preferred-search-latest.json').write_text(json.dumps(report, indent=2))
    lines = ['# LinkedIn preferred search', '', 'Discovery only — no applications submitted.', '']
    for job in report['shortlist']:
        lines.append('- [{} — {}]({}) — {}'.format(job['title'], job['company'], job['employer_url'], job['location']))
    if report['other_application_sites']:
        lines += ['', '## Other application sites — not queued', '']
        for job in report['other_application_sites']:
            lines.append('- [{} — {}]({}) — {}'.format(job['title'], job['company'], job['employer_url'], job['application_site']['provider']))
    (repo.path.parent / 'linkedin-preferred-search-latest.md').write_text('\n'.join(lines) + '\n')
    return report


def run(repo, endpoint, limit=None):
    config = json.loads(CONFIG.read_text())
    plan = search_plan(config)
    if limit is not None:
        if not 1 <= limit <= len(plan):
            raise ValueError('Search limit outside saved plan')
        plan = plan[:limit]
    collector = LinkedInBrowserCollector(repo)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'searches': [],
              'applications_submitted': 0, 'state': 'running', 'shortlist': []}
    output = repo.path.parent / 'linkedin-preferred-search-latest.json'
    ids = set()
    for search in plan:
        print('SEARCH: ' + search['name'], flush=True)
        result = collector.run(endpoint, search['url'], config['pages_per_search'],
                               config['details_per_search'], config['delay_seconds'])
        ids.update(result.get('job_ids', []))
        report['searches'].append(dict(search, result=result))
        report['shortlist'], report['already_applied_or_held'], report['counts'] = shortlist(repo, ids)
        report['unique_postings'] = len(ids)
        output.write_text(json.dumps(report, indent=2))
        if result['state'] == 'stopped':
            report['state'] = 'stopped'
            report['error'] = result.get('error', '')
            break
    else:
        report['state'] = 'completed'
    output.write_text(json.dumps(report, indent=2))
    return save_report(repo, report)
