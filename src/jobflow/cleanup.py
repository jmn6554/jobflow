"""Explicit, backed-up cleanup of untracked postings with definite policy failures."""
import json
import sqlite3
from collections import Counter
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from .eligibility import evaluate, skill_match
from .models import JobInput
from .policy import settings


def rejection_reason(job):
    decision = evaluate(job, ignore_salary=settings()['ignore_salary'])
    reasons = [r for r in decision.reasons if 'needs confirmation' not in r]
    # Unknown country is incomplete evidence, not proof of ineligibility.
    if not job.country:
        reasons = [r for r in reasons if r != 'role is outside Canada and the United States']
    if reasons:
        return '; '.join(reasons)
    if len(job.description.strip()) >= 500:
        match = skill_match(job)
        if len(match['jd_skills']) >= 3 and match['percent'] < settings()['minimum_skill_match']:
            return 'resume skill overlap below {}%'.format(settings()['minimum_skill_match'])
    return ''


def protected_ids(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    protected = {r[0] for r in db.execute("SELECT id FROM jobs WHERE status NOT IN ('new','reviewing','rejected')")}
    for table in ('submissions', 'submission_approvals', 'resume_artifacts', 'events', 'reminders'):
        if table in tables:
            protected.update(r[0] for r in db.execute('SELECT DISTINCT job_id FROM ' + table))
    if 'daily_queue' in tables:
        protected.update(r[0] for r in db.execute("SELECT job_id FROM daily_queue WHERE state NOT IN ('ready','manual_link','excluded')"))
    return protected


def plan(repo):
    with repo.connect() as db:
        protected = protected_ids(db)
        rows = db.execute('SELECT * FROM jobs').fetchall()
    keys = {f.name for f in fields(JobInput)} - {'compensation', 'raw_payload'}
    targets, preserved_mismatches = [], []
    for row in rows:
        job = JobInput(**{k: row[k] for k in keys})
        reason = rejection_reason(job)
        if not reason:
            continue
        target = {'id': row['id'], 'source': row['source'], 'title': row['title'],
                  'company': row['company'], 'updated_at': row['updated_at'], 'reason': reason}
        (preserved_mismatches if row['id'] in protected else targets).append(target)
    return {'targets': targets, 'preserved_mismatches': preserved_mismatches, 'before': len(rows)}


def apply(repo, planned):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = repo.path.parent / 'backups'
    folder.mkdir(exist_ok=True)
    backup = (folder / ('jobflow-before-prune-' + stamp + '.db')).resolve()
    with sqlite3.connect(repo.path) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup integrity check failed; nothing deleted')
    deleted = []
    with repo.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS cleanup_audit (id INTEGER PRIMARY KEY, job_id INTEGER, source TEXT, title TEXT, company TEXT, reason TEXT, backup_path TEXT, deleted_at TEXT)')
        protected = protected_ids(db)
        has_queue = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_queue'").fetchone())
        for target in planned['targets']:
            row = db.execute('SELECT updated_at FROM jobs WHERE id=?', (target['id'],)).fetchone()
            if not row or target['id'] in protected or row['updated_at'] != target['updated_at']:
                continue
            if has_queue:
                db.execute('DELETE FROM daily_queue WHERE job_id=?', (target['id'],))
            db.execute('DELETE FROM jobs WHERE id=?', (target['id'],))
            db.execute('INSERT INTO cleanup_audit(job_id,source,title,company,reason,backup_path,deleted_at) VALUES (?,?,?,?,?,?,?)',
                       (target['id'], target['source'], target['title'], target['company'], target['reason'], str(backup), stamp))
            deleted.append(target)
        # Keep application evidence but remove definite mismatches from active eligibility lists.
        for target in planned['preserved_mismatches']:
            db.execute("UPDATE jobs SET eligibility_state='rejected',eligibility_queue='rejected',eligibility_reasons=? WHERE id=? AND updated_at=?",
                       (json.dumps([target['reason']]), target['id'], target['updated_at']))
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Foreign-key check failed; cleanup rolled back')
        remaining = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
    return {'deleted': len(deleted), 'by_source': dict(Counter(t['source'] for t in deleted)),
            'primary_reasons': dict(Counter(t['reason'].split(';')[0] for t in deleted)),
            'preserved_application_history': len(planned['preserved_mismatches']),
            'remaining': remaining, 'backup': str(backup)}
