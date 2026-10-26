"""Deterministic alert resolution with bounded public employer-link discovery."""

import re
import unicodedata
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .providers import public_http_url, prohibited_automation_url, aggregator_url


def normalized(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    value = re.sub(r'\bsr\.?\b', 'senior', value)
    return ' '.join(re.findall(r'\w+', value))


def compatible_location(alert, employer):
    if not alert['country'] or alert['country'].upper() != employer['country'].upper():
        return False
    if alert['workplace_type'] == employer['workplace_type'] == 'remote':
        return True
    return bool(alert['location']) and normalized(alert['location']) == normalized(employer['location'])


class LinkedInEmployerResolver:
    def __init__(self, repo):
        self.repo = repo
        with repo.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS linkedin_employer_matches (
                linkedin_job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                employer_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                state TEXT NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL
            )''')

    def run(self, limit=1000, web_limit=20):
        if limit < 1:
            raise ValueError('limit must be positive')
        rows = self.repo.jobs(limit=100000)
        index = defaultdict(list)
        for row in rows:
            url = row['canonical_url']
            if (row['source'] in {'greenhouse', 'lever', 'ashby', 'employer_link'} and
                    public_http_url(url) and not prohibited_automation_url(url) and not aggregator_url(url)):
                index[(normalized(row['company']), normalized(row['title']))].append(row)
        alerts = sorted((r for r in rows if r['source'] == 'linkedin'),
                        key=lambda r: (r['created_at'], r['id']), reverse=True)[:limit]
        counts, results = Counter(), []
        from .employer_links import EmployerLinkFinder
        finder = EmployerLinkFinder()
        web_checked = 0
        try:
            for alert in alerts:
                matches = [r for r in index[(normalized(alert['company']), normalized(alert['title']))]
                           if compatible_location(alert, r)]
                target = matches[0] if len(matches) == 1 else None
                state = 'matched' if target is not None else 'ambiguous' if matches else 'unresolved'
                detail = ('Exact normalized company/title and compatible location in ATS inventory'
                          if target is not None else 'Multiple ATS records; no automatic choice' if matches
                          else 'No exact compatible ATS record; pending next discovery')
                if not matches and web_checked < web_limit and alert['status'] in {'new', 'reviewing'}:
                    web_checked += 1
                    with self.repo.connect() as db:
                        snapshots = db.execute('SELECT raw_payload FROM job_snapshots WHERE job_id=? ORDER BY id DESC LIMIT 1', (alert['id'],)).fetchone()
                    try:
                        email_links = json.loads(snapshots[0]).get('employer_link_candidates', []) if snapshots else []
                    except (ValueError, TypeError):
                        email_links = []
                    discovered, detail = finder.find(alert, email_links)
                    if len(discovered) == 1:
                        # Reuse an existing canonical record without overwriting its source/history.
                        with self.repo.connect() as db:
                            existing = db.execute('SELECT id FROM jobs WHERE canonical_url=?', (discovered[0].canonical_url,)).fetchone()
                        target_id = existing[0] if existing else self.repo.upsert_job(discovered[0])[0]
                        target = self.repo.job(target_id)
                        state = 'matched'
                        detail = 'Employer page verified title, company and location. ' + detail
                    elif len(discovered) > 1:
                        state = 'ambiguous'
                        detail = 'Multiple verified employer postings; no automatic choice. ' + detail
                with self.repo.connect() as db:
                    db.execute('''INSERT INTO linkedin_employer_matches VALUES (?,?,?,?,?)
                    ON CONFLICT(linkedin_job_id) DO UPDATE SET
                    employer_job_id=excluded.employer_job_id,state=excluded.state,
                    detail=excluded.detail,checked_at=excluded.checked_at''',
                           (alert['id'], target['id'] if target is not None else None, state, detail,
                            datetime.now(timezone.utc).isoformat()))
                counts[state] += 1
                results.append({'id': alert['id'], 'title': alert['title'], 'company': alert['company'],
                                'state': state, 'employer_job_id': target['id'] if target is not None else None,
                                'url': target['canonical_url'] if target is not None else '', 'detail': detail})
        finally:
            finder.client.close()
        return {'checked': len(alerts), 'web_checked': web_checked, 'counts': dict(counts), 'results': results}
