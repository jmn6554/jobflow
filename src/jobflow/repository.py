"""SQLite persistence with additive schema migrations and audit evidence."""

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from .dedupe import canonical_url, identity_key
from .models import Compensation, EligibilityDecision, JobInput, MatchScore, ScoreResult


STATUSES = ("new", "reviewing", "preparing", "submitted", "interviewing", "offer", "rejected", "withdrawn")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  identity_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  location TEXT NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL,
  description_hash TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,
  external_id TEXT,
  workplace_type TEXT NOT NULL DEFAULT 'unspecified',
  employment_type TEXT NOT NULL DEFAULT 'full-time',
  country TEXT NOT NULL DEFAULT '',
  language_requirements TEXT NOT NULL DEFAULT '',
  sponsorship TEXT NOT NULL DEFAULT 'unknown',
  security_clearance TEXT NOT NULL DEFAULT '',
  posted_at TEXT NOT NULL DEFAULT '',
  closes_at TEXT NOT NULL DEFAULT '',
  salary_min REAL,
  salary_max REAL,
  salary_currency TEXT NOT NULL DEFAULT '',
  salary_interval TEXT NOT NULL DEFAULT 'year',
  salary_estimated INTEGER NOT NULL DEFAULT 0,
  salary_estimate_source TEXT NOT NULL DEFAULT '',
  salary_estimate_confidence TEXT NOT NULL DEFAULT '',
  salary_includes_variable INTEGER NOT NULL DEFAULT 0,
  eligibility_state TEXT NOT NULL DEFAULT 'pending',
  eligibility_queue TEXT NOT NULL DEFAULT 'pending',
  eligibility_reasons TEXT NOT NULL DEFAULT '[]',
  eligibility_warnings TEXT NOT NULL DEFAULT '[]',
  fit_score INTEGER,
  score_explanation TEXT,
  score_components TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_snapshots (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  description_hash TEXT NOT NULL,
  description TEXT NOT NULL,
  raw_payload TEXT NOT NULL DEFAULT '',
  captured_at TEXT NOT NULL,
  UNIQUE(job_id, description_hash)
);
CREATE TABLE IF NOT EXISTS discovery_runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  state TEXT NOT NULL,
  discovered_count INTEGER NOT NULL DEFAULT 0,
  qualified_count INTEGER NOT NULL DEFAULT 0,
  review_count INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS discovery_provider_runs (
  id INTEGER PRIMARY KEY,
  discovery_run_id INTEGER NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  state TEXT NOT NULL,
  item_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS discovery_board_runs (
  id INTEGER PRIMARY KEY,
  discovery_run_id INTEGER NOT NULL REFERENCES discovery_runs(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  company TEXT NOT NULL,
  board_token TEXT NOT NULL,
  state TEXT NOT NULL,
  raw_count INTEGER NOT NULL DEFAULT 0,
  index_count INTEGER NOT NULL DEFAULT 0,
  emitted_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS resume_artifacts (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  state TEXT NOT NULL,
  base_resume_hash TEXT NOT NULL,
  jd_hash TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  model TEXT NOT NULL,
  docx_path TEXT NOT NULL DEFAULT '',
  pdf_path TEXT NOT NULL DEFAULT '',
  docx_hash TEXT NOT NULL DEFAULT '',
  change_report TEXT NOT NULL DEFAULT '{}',
  ats_terms TEXT NOT NULL DEFAULT '[]',
  gaps TEXT NOT NULL DEFAULT '[]',
  validation_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(job_id, version)
);
CREATE TABLE IF NOT EXISTS saved_answers (
  id INTEGER PRIMARY KEY,
  canonical_question TEXT NOT NULL,
  normalized_question TEXT NOT NULL,
  options_hash TEXT NOT NULL,
  jurisdiction TEXT NOT NULL DEFAULT '',
  answer TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  approved_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(normalized_question, options_hash, jurisdiction, version)
);
CREATE TABLE IF NOT EXISTS submission_approvals (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  artifact_id INTEGER NOT NULL REFERENCES resume_artifacts(id),
  binding_hash TEXT NOT NULL UNIQUE,
  form_hash TEXT NOT NULL,
  answers_hash TEXT NOT NULL,
  answers_json TEXT NOT NULL DEFAULT '{}',
  typed_confirmation TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  invalidated_at TEXT
);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  approval_id INTEGER REFERENCES submission_approvals(id),
  provider TEXT NOT NULL,
  state TEXT NOT NULL,
  receipt TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  attempted_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS submission_resumes (
  submission_id INTEGER PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
  docx_path TEXT NOT NULL,
  docx_hash TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
  event_type TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
  due_date TEXT NOT NULL, kind TEXT NOT NULL, completed_at TEXT,
  UNIQUE(job_id, due_date, kind)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_queue_score ON jobs(eligibility_queue, fit_score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_job ON job_snapshots(job_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON resume_artifacts(job_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_events(job_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_one_confirmed
ON submissions(job_id) WHERE state = 'submitted';
"""

# Columns added after the original dependency-free prototype. SQLite supports
# this intentionally conservative form of additive migration.
JOB_COLUMN_MIGRATIONS = {
    "canonical_url": "TEXT NOT NULL DEFAULT ''",
    "description_hash": "TEXT NOT NULL DEFAULT ''",
    "workplace_type": "TEXT NOT NULL DEFAULT 'unspecified'",
    "employment_type": "TEXT NOT NULL DEFAULT 'full-time'",
    "country": "TEXT NOT NULL DEFAULT ''",
    "language_requirements": "TEXT NOT NULL DEFAULT ''",
    "sponsorship": "TEXT NOT NULL DEFAULT 'unknown'",
    "security_clearance": "TEXT NOT NULL DEFAULT ''",
    "posted_at": "TEXT NOT NULL DEFAULT ''",
    "closes_at": "TEXT NOT NULL DEFAULT ''",
    "salary_min": "REAL",
    "salary_max": "REAL",
    "salary_currency": "TEXT NOT NULL DEFAULT ''",
    "salary_interval": "TEXT NOT NULL DEFAULT 'year'",
    "salary_estimated": "INTEGER NOT NULL DEFAULT 0",
    "salary_estimate_source": "TEXT NOT NULL DEFAULT ''",
    "salary_estimate_confidence": "TEXT NOT NULL DEFAULT ''",
    "salary_includes_variable": "INTEGER NOT NULL DEFAULT 0",
    "eligibility_state": "TEXT NOT NULL DEFAULT 'pending'",
    "eligibility_queue": "TEXT NOT NULL DEFAULT 'pending'",
    "eligibility_reasons": "TEXT NOT NULL DEFAULT '[]'",
    "eligibility_warnings": "TEXT NOT NULL DEFAULT '[]'",
    "score_components": "TEXT NOT NULL DEFAULT '{}'",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def init(self):
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            existing = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for name, definition in JOB_COLUMN_MIGRATIONS.items():
                if name not in existing:
                    connection.execute("ALTER TABLE jobs ADD COLUMN {} {}".format(name, definition))
            approval_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(submission_approvals)").fetchall()
            }
            if "answers_json" not in approval_columns:
                connection.execute(
                    "ALTER TABLE submission_approvals ADD COLUMN answers_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.executescript(INDEXES)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (_now(),)
            )
            connection.execute("PRAGMA optimize")

    def start_discovery_run(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO discovery_runs(started_at,state) VALUES(?, 'running')", (_now(),)
            )
            return int(cursor.lastrowid)

    def record_provider_run(self, run_id: int, provider: str, state: str, count: int, error: str = ""):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO discovery_provider_runs(discovery_run_id,provider,state,item_count,error) VALUES(?,?,?,?,?)",
                (run_id, provider, state, count, error[:1000]),
            )

    def record_board_runs(self, run_id: int, provider: str, results):
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO discovery_board_runs
                (discovery_run_id,provider,company,board_token,state,raw_count,index_count,emitted_count,error)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                [(run_id, provider, row["company"], row["board_token"], row["state"],
                  row["raw_count"], row["index_count"], row["emitted_count"], row["error"][:100])
                 for row in results],
            )

    def discovery_board_summary(self, run_id: int):
        with self.connect() as connection:
            return connection.execute(
                """SELECT provider, COUNT(*) AS boards, SUM(state='failed') AS failed,
                SUM(state='partial') AS partial,
                SUM(raw_count) AS raw_count, SUM(index_count) AS index_count,
                SUM(emitted_count) AS emitted_count FROM discovery_board_runs
                WHERE discovery_run_id=? GROUP BY provider""", (run_id,)
            ).fetchall()

    def failed_discovery_boards(self, run_id: int, limit: int = 100):
        with self.connect() as connection:
            return connection.execute(
                """SELECT company,board_token,state,error FROM discovery_board_runs
                WHERE discovery_run_id=? AND state IN ('failed','partial') ORDER BY company LIMIT ?""",
                (run_id, limit),
            ).fetchall()

    def finish_discovery_run(self, run_id: int, discovered: int, qualified: int, review: int, error=""):
        with self.connect() as connection:
            connection.execute(
                """UPDATE discovery_runs SET completed_at=?,state=?,discovered_count=?,qualified_count=?,
                review_count=?,error_summary=? WHERE id=?""",
                (_now(), "partial" if error else "completed", discovered, qualified, review, error[:2000], run_id),
            )

    def discovery_runs(self, limit=20):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM discovery_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def discovery_run(self, run_id: int):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Discovery run {} not found".format(run_id))
        return row

    def new_jobs_in_discovery_run(self, run_id: int) -> int:
        run = self.discovery_run(run_id)
        if not run["completed_at"]:
            return 0
        with self.connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE created_at BETWEEN ? AND ?",
                (run["started_at"], run["completed_at"]),
            ).fetchone()[0])

    def upsert_job(self, job: JobInput) -> Tuple[int, bool, bool]:
        now = _now()
        key = identity_key(job)
        verified_canonical_url = canonical_url(job.canonical_url)
        description_hash = _hash(job.description)
        comp = job.compensation.annualized() if job.compensation else None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,description_hash,canonical_url FROM jobs WHERE identity_key=?", (key,)
            ).fetchone()
            if row is None and verified_canonical_url:
                # A direct ATS feed and a web-search result often use different
                # source IDs for the same posting. Prefer the verified employer
                # URL so enabling first-party feeds does not create duplicates.
                row = connection.execute(
                    "SELECT id,description_hash,canonical_url FROM jobs WHERE canonical_url=? ORDER BY id LIMIT 1",
                    (verified_canonical_url,),
                ).fetchone()
            is_new = row is None
            if is_new:
                cursor = connection.execute(
                    """INSERT INTO jobs(
                    identity_key,title,company,location,url,canonical_url,description,description_hash,source,external_id,
                    workplace_type,employment_type,country,language_requirements,sponsorship,security_clearance,
                    posted_at,closes_at,salary_min,salary_max,salary_currency,salary_interval,salary_estimated,
                    salary_estimate_source,salary_estimate_confidence,salary_includes_variable,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key, job.title, job.company, job.location, job.url, verified_canonical_url, job.description, description_hash,
                        job.source, job.external_id, job.workplace_type, job.employment_type, job.country,
                        job.language_requirements, job.sponsorship, job.security_clearance, job.posted_at, job.closes_at,
                        comp.minimum if comp else None, comp.maximum if comp else None, comp.currency if comp else "",
                        comp.interval if comp else "year", int(bool(comp and comp.is_estimated)),
                        comp.estimate_source if comp else "", comp.estimate_confidence if comp else "",
                        int(bool(comp and comp.includes_bonus_or_equity)), now, now,
                    ),
                )
                job_id = int(cursor.lastrowid)
                changed = True
            else:
                job_id = int(row["id"])
                if row["canonical_url"] and not verified_canonical_url:
                    # Do not replace a previously verified full posting with a
                    # later search-index snippet for the same external result.
                    return job_id, False, False
                changed = row["description_hash"] != description_hash
                connection.execute(
                    """UPDATE jobs SET title=?,company=?,location=?,url=?,canonical_url=?,description=?,
                    description_hash=?,source=?,external_id=?,workplace_type=?,employment_type=?,country=?,
                    language_requirements=?,sponsorship=?,security_clearance=?,posted_at=?,closes_at=?,salary_min=?,
                    salary_max=?,salary_currency=?,salary_interval=?,salary_estimated=?,salary_estimate_source=?,
                    salary_estimate_confidence=?,salary_includes_variable=?,updated_at=? WHERE id=?""",
                    (
                        job.title, job.company, job.location, job.url, verified_canonical_url, job.description, description_hash,
                        job.source, job.external_id, job.workplace_type, job.employment_type, job.country,
                        job.language_requirements, job.sponsorship, job.security_clearance, job.posted_at, job.closes_at,
                        comp.minimum if comp else None, comp.maximum if comp else None, comp.currency if comp else "",
                        comp.interval if comp else "year", int(bool(comp and comp.is_estimated)),
                        comp.estimate_source if comp else "", comp.estimate_confidence if comp else "",
                        int(bool(comp and comp.includes_bonus_or_equity)), now, job_id,
                    ),
                )
            if changed:
                connection.execute(
                    """INSERT OR IGNORE INTO job_snapshots(job_id,description_hash,description,raw_payload,captured_at)
                    VALUES(?,?,?,?,?)""",
                    (job_id, description_hash, job.description, job.raw_payload, now),
                )
            return job_id, is_new, changed

    def add_job(self, job: JobInput) -> bool:
        return self.upsert_job(job)[1]

    def save_evaluation(self, job_id: int, decision: EligibilityDecision, match: MatchScore):
        self.save_evaluations(((job_id, decision, match),))

    def save_evaluations(
        self,
        evaluations: Iterable[Tuple[int, EligibilityDecision, MatchScore]],
    ):
        """Persist many screening decisions in one SQLite transaction."""
        records = []
        now = _now()
        for job_id, decision, match in evaluations:
            components = {
                "responsibilities": match.responsibilities,
                "skills": match.skills,
                "seniority": match.seniority,
                "domain": match.domain,
                "compensation": match.compensation,
                "matched_terms": match.matched_terms,
                "missing_terms": match.missing_terms,
                "skill_match_percent": match.skill_match_percent,
                "jd_skills": match.jd_skills,
                "matched_skills": match.matched_skills,
                "missing_skills": match.missing_skills,
                "matched_required": match.matched_required,
                "missing_required": match.missing_required,
                "matched_preferred": match.matched_preferred,
                "missing_preferred": match.missing_preferred,
                "skill_confidence": match.skill_confidence,
            }
            records.append((
                decision.state, decision.queue, json.dumps(decision.reasons),
                json.dumps(decision.warnings), match.total, match.explanation,
                json.dumps(components), now, job_id,
            ))
        if not records:
            return
        with self.connect() as connection:
            connection.executemany(
                """UPDATE jobs SET eligibility_state=?,eligibility_queue=?,eligibility_reasons=?,
                eligibility_warnings=?,fit_score=?,score_explanation=?,score_components=?,updated_at=? WHERE id=?""",
                records,
            )

    def jobs(self, status: Optional[str] = None, queue: Optional[str] = None, limit=200):
        clauses, values = [], []
        if status:
            clauses.append("status=?")
            values.append(status)
        if queue:
            clauses.append("eligibility_queue=?")
            values.append(queue)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM jobs{} ORDER BY fit_score DESC,created_at DESC,id LIMIT ?".format(where), values
            ).fetchall()

    def surfaced_jobs(self, limit=30):
        with self.connect() as connection:
            return connection.execute(
                """SELECT * FROM jobs WHERE eligibility_queue IN ('qualified','estimated_pay_review')
                ORDER BY fit_score DESC,created_at DESC,id LIMIT ?""",
                (limit,),
            ).fetchall()

    def linkedin_links(self, limit=100):
        with self.connect() as connection:
            return connection.execute(
                """SELECT * FROM jobs WHERE source='linkedin'
                AND eligibility_queue IN ('qualified','estimated_pay_review')
                ORDER BY created_at DESC,fit_score DESC,id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def job_by_external_id(self, source: str, external_id: str):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM jobs WHERE source=? AND external_id=? ORDER BY id LIMIT 1",
                (source, external_id),
            ).fetchone()

    def job(self, job_id: int):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Job {} not found".format(job_id))
        return row

    def job_input(self, job_id: int) -> JobInput:
        row = self.job(job_id)
        compensation = None
        if row["salary_min"] is not None or row["salary_max"] is not None:
            compensation = Compensation(
                row["salary_min"], row["salary_max"], row["salary_currency"], row["salary_interval"],
                bool(row["salary_estimated"]), row["salary_estimate_source"], row["salary_estimate_confidence"],
                bool(row["salary_includes_variable"]),
            )
        return JobInput(
            row["title"], row["company"], row["location"], row["url"], row["description"], row["source"],
            row["external_id"], row["canonical_url"], row["workplace_type"], row["employment_type"], row["country"],
            row["language_requirements"], row["sponsorship"], row["security_clearance"], row["posted_at"],
            row["closes_at"], compensation,
        )

    def invalidate_web_verification(self, job_id: int) -> None:
        """Remove stale inferred evidence when a web result no longer validates."""
        row = self.job(job_id)
        with self.connect() as connection:
            if row["source"] == "brave":
                connection.execute(
                    """UPDATE jobs SET canonical_url='',salary_min=NULL,salary_max=NULL,salary_currency='',
                    salary_interval='year',salary_estimated=0,salary_estimate_source='',
                    salary_estimate_confidence='',salary_includes_variable=0,updated_at=? WHERE id=?""",
                    (_now(), job_id),
                )
            else:
                connection.execute("UPDATE jobs SET canonical_url='',updated_at=? WHERE id=?", (_now(), job_id))

    def save_score(self, job_id: int, result: ScoreResult):
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET fit_score=?,score_explanation=?,updated_at=? WHERE id=?",
                (result.score, result.explanation, _now(), job_id),
            )

    def transition(self, job_id: int, status: str, note: str = "", follow_up_days: Optional[int] = None):
        if status not in STATUSES:
            raise ValueError("Unknown status: {}".format(status))
        self.job(job_id)
        now = _now()
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (status, now, job_id))
            connection.execute(
                "INSERT INTO events(job_id,event_type,note,created_at) VALUES(?,?,?,?)",
                (job_id, "status:" + status, note, now),
            )
            connection.execute(
                "INSERT INTO audit_events(job_id,event_type,detail,created_at) VALUES(?,?,?,?)",
                (job_id, "status_changed", json.dumps({"status": status, "note": note}), now),
            )
            if follow_up_days is not None:
                due = (date.today() + timedelta(days=follow_up_days)).isoformat()
                connection.execute(
                    "INSERT OR IGNORE INTO reminders(job_id,due_date,kind) VALUES(?,?,?)",
                    (job_id, due, "application_follow_up"),
                )

    def reminders(self, through: Optional[str] = None):
        through = through or date.today().isoformat()
        with self.connect() as connection:
            return connection.execute(
                """SELECT reminders.*,jobs.title,jobs.company FROM reminders JOIN jobs ON jobs.id=reminders.job_id
                WHERE completed_at IS NULL AND due_date<=? ORDER BY due_date""", (through,)
            ).fetchall()

    def next_artifact_version(self, job_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(version),0)+1 value FROM resume_artifacts WHERE job_id=?", (job_id,)).fetchone()
            return int(row["value"])

    def create_artifact(self, job_id: int, version: int, state: str, metadata: Dict) -> int:
        with self.connect() as connection:
            connection.execute(
                "UPDATE submission_approvals SET invalidated_at=? WHERE job_id=? AND invalidated_at IS NULL",
                (_now(), job_id),
            )
            cursor = connection.execute(
                """INSERT INTO resume_artifacts(job_id,version,state,base_resume_hash,jd_hash,prompt_version,
                model,docx_path,pdf_path,docx_hash,change_report,ats_terms,gaps,validation_error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, version, state, metadata["base_resume_hash"], metadata["jd_hash"],
                    metadata["prompt_version"], metadata["model"], metadata.get("docx_path", ""),
                    metadata.get("pdf_path", ""), metadata.get("docx_hash", ""),
                    json.dumps(metadata.get("change_report", {})), json.dumps(metadata.get("ats_terms", [])),
                    json.dumps(metadata.get("gaps", [])), metadata.get("validation_error", ""), _now(),
                ),
            )
            artifact_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(job_id,event_type,detail,created_at) VALUES(?,?,?,?)",
                (job_id, "resume_generated", json.dumps({"artifact_id": artifact_id, "version": version, "state": state}), _now()),
            )
            return artifact_id

    def artifacts(self, job_id: int):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM resume_artifacts WHERE job_id=? ORDER BY version DESC", (job_id,)
            ).fetchall()

    def artifact(self, artifact_id: int):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM resume_artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise ValueError("Resume artifact not found")
        return row

    def save_answer(self, canonical_question: str, normalized_question: str, options_hash: str, jurisdiction: str, answer: str) -> int:
        with self.connect() as connection:
            connection.execute(
                "UPDATE saved_answers SET active=0 WHERE normalized_question=? AND options_hash=? AND jurisdiction=?",
                (normalized_question, options_hash, jurisdiction),
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 value FROM saved_answers WHERE normalized_question=? AND jurisdiction=?",
                (normalized_question, jurisdiction),
            ).fetchone()
            cursor = connection.execute(
                """INSERT INTO saved_answers(canonical_question,normalized_question,options_hash,jurisdiction,
                answer,version,approved_at,active) VALUES(?,?,?,?,?,?,?,1)""",
                (canonical_question, normalized_question, options_hash, jurisdiction, answer, int(row["value"]), _now()),
            )
            return int(cursor.lastrowid)

    def find_answer(self, normalized_question: str, options_hash: str, jurisdiction: str):
        with self.connect() as connection:
            return connection.execute(
                """SELECT * FROM saved_answers WHERE normalized_question=? AND options_hash=? AND jurisdiction=?
                AND active=1 ORDER BY version DESC LIMIT 1""",
                (normalized_question, options_hash, jurisdiction),
            ).fetchone()

    def approve_submission(
        self, job_id: int, artifact_id: int, form_hash: str, answers_hash: str,
        typed: str, answers_json: str = "{}"
    ) -> str:
        if typed != "SUBMIT":
            raise ValueError("Type SUBMIT exactly to approve this application")
        artifact = self.artifact(artifact_id)
        if artifact["job_id"] != job_id or artifact["state"] != "ready":
            raise ValueError("The selected resume is not ready for approval")
        binding = _hash("|".join((str(job_id), str(artifact_id), artifact["docx_hash"], form_hash, answers_hash)))
        with self.connect() as connection:
            connection.execute("UPDATE submission_approvals SET invalidated_at=? WHERE job_id=? AND invalidated_at IS NULL", (_now(), job_id))
            connection.execute(
                """INSERT INTO submission_approvals(job_id,artifact_id,binding_hash,form_hash,answers_hash,
                answers_json,typed_confirmation,approved_at) VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, artifact_id, binding, form_hash, answers_hash, answers_json, typed, _now()),
            )
        return binding

    def approval(self, binding_hash: str):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM submission_approvals WHERE binding_hash=? AND invalidated_at IS NULL", (binding_hash,)
            ).fetchone()

    @property
    def submission_resume_root(self) -> Path:
        return self.path.resolve().parent / "submission-resumes"

    def snapshot_resume(self, job_id: int, resume: Path) -> Path:
        """Freeze bytes BEFORE uploading, retaining the candidate's filename.

        Content-addressed copies are never replaced. Different attempts can
        share identical bytes, but a later edit always gets a different path.
        """
        self.job(job_id)
        source = Path(resume).resolve()
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        root = self.submission_resume_root
        if root in source.parents and source.parent.name != digest:
            raise ValueError("Archived resume was modified; refusing to use it")
        target = root / str(job_id) / digest / source.name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Publish atomically without overwriting an existing version.
        fd, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".resume-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o400)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            if target.read_bytes() != content:
                raise ValueError("Archived resume hash mismatch; refusing to overwrite")
        finally:
            os.unlink(temporary)
        return target

    def submission_resume(self, submission_id: int):
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM submission_resumes WHERE submission_id=?", (submission_id,)
            ).fetchone()

    def submission_resume_file(self, submission_id: int) -> Path:
        row = self.submission_resume(submission_id)
        if row is None:
            raise ValueError("No exact resume was recorded for this attempt")
        path = Path(row["docx_path"]).resolve()
        if self.submission_resume_root not in path.parents or not path.is_file():
            raise ValueError("Archived resume is unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["docx_hash"]:
            raise ValueError("Archived resume failed its integrity check")
        return path

    def record_submission(self, job_id: int, approval_id: Optional[int], provider: str, state: str, receipt="", detail="", resume_path=None):
        snapshot = self.snapshot_resume(job_id, resume_path) if resume_path is not None else None
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO submissions(job_id,approval_id,provider,state,receipt,detail,attempted_at,confirmed_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, approval_id, provider, state, receipt, detail, _now(), _now() if state == "submitted" else None),
            )
            submission_id = int(cursor.lastrowid)
            if snapshot is not None:
                connection.execute(
                    "INSERT INTO submission_resumes VALUES(?,?,?,?,?)",
                    (submission_id, str(snapshot), snapshot.parent.name, snapshot.name, _now()),
                )
            return submission_id

    def reconcile_rejected_intent(self, job_id: int, submission_id: int, evidence: str):
        """Resolve only our write-ahead intent after explicit employer validation rejection."""
        if not evidence.startswith('Employer rejected incomplete form; not submitted:'):
            raise ValueError('Explicit employer rejection evidence required')
        with self.connect() as db:
            result = db.execute("""UPDATE submissions SET state='manual_action_required', detail=?
                WHERE id=? AND job_id=? AND state='confirmation_unknown'
                AND detail='Submission started; awaiting receipt'""",
                ('Reconciled submission intent: '+evidence, submission_id, job_id))
            return result.rowcount == 1

    def submissions(self, job_id: Optional[int] = None):
        with self.connect() as connection:
            if job_id is None:
                return connection.execute("SELECT * FROM submissions ORDER BY attempted_at DESC").fetchall()
            return connection.execute("SELECT * FROM submissions WHERE job_id=? ORDER BY attempted_at DESC", (job_id,)).fetchall()
