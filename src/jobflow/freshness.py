"""Publication time is separate from observation time; edits are not new jobs."""
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .dedupe import application_posting_keys, canonical_url


def posting_identity(job):
    row = asdict(job) if not isinstance(job, dict) else job
    keys = application_posting_keys(row)
    ats = sorted(k for k in keys if k[0] != 'url')
    return json.dumps(ats[0] if ats else ('url', canonical_url(row.get('canonical_url') or row['url'])))


def publication(value, source, now):
    """Never pretend date-only values have hour-level precision."""
    if not value:
        return {'kind':'unknown', 'value':'', 'within_hour':False, 'within_day':False}
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return {'kind':'invalid', 'value':value, 'within_hour':False, 'within_day':False}
    kind = 'last_published' if source == 'ashby' else 'first_published' if source == 'greenhouse' else 'posted'
    if len(value) <= 10 or parsed.tzinfo is None:
        return {'kind':kind, 'value':value, 'precision':'date' if len(value)<=10 else 'timezone_unknown',
                'within_hour':False, 'within_day':False}
    age = (now-parsed).total_seconds()
    return {'kind':kind, 'value':value, 'precision':'timestamp',
            'within_hour':0 <= age <= 3600, 'within_day':0 <= age <= 86400}


class FreshnessStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sightings (
                identity TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                publication TEXT NOT NULL, baseline INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS boards (board TEXT PRIMARY KEY, checked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (job_id INTEGER PRIMARY KEY, discovered_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')

    def observe(self, job, board, now):
        key = posting_identity(job)
        prior = self.db.execute('SELECT * FROM sightings WHERE identity=?', (key,)).fetchone()
        initialized = self.db.execute('SELECT 1 FROM boards WHERE board=?',(board,)).fetchone() is not None
        published = publication(job.posted_at, job.source, now)
        baseline = prior['baseline'] if prior else not initialized
        first_seen = prior['first_seen'] if prior else now.isoformat()
        self.db.execute('INSERT INTO sightings VALUES (?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET '
            'last_seen=excluded.last_seen,publication=excluded.publication',
            (key,first_seen,now.isoformat(),json.dumps(published),baseline))
        return dict(identity=key, first_seen=first_seen, publication=published,
                    newly_observed=prior is None, first_detected_after_baseline=prior is None and initialized,
                    baseline=bool(baseline))

    def complete_board(self, board, now):
        self.db.execute('INSERT OR REPLACE INTO boards VALUES (?,?)',(board,now.isoformat()))
        self.db.commit()

    def add_pending(self, jid, now):
        self.db.execute('INSERT OR IGNORE INTO pending VALUES (?,?)',(jid,now.isoformat()))
        self.db.commit()

    def close(self):
        self.db.commit(); self.db.close()
