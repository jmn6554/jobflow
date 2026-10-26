"""Batch verification of search-index jobs before resume tailoring."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Tuple
import re

import httpx

from .eligibility import evaluate, score
from .providers import aggregator_url, prohibited_automation_url, public_http_url, resolve_canonical_job
from .repository import Repository
from .policy import skill_threshold_met


@dataclass(frozen=True)
class VerificationReport:
    checked: int
    resolved: int
    qualified: int
    rejected: int
    still_review: int
    errors: int
    preparable_job_ids: Tuple[int, ...]


def is_preparable(row) -> bool:
    """Require posting evidence and eligibility, not an implicit skill cutoff."""
    import json

    try:
        components = json.loads(row["score_components"] or "{}")
    except (TypeError, ValueError):
        components = {}
    canonical = row["canonical_url"]
    title = (row["title"] or "").lower()
    outside_resume_domain = any(term in title for term in (
        "people data", "people scientist", "talent management",
    )) or ("software engineer" in title and "data engineer" not in title)
    location_text = "{} {}".format(row["location"] or "", row["description"] or "")
    explicit_nonlocal_hybrid = bool(re.search(
        r"(?:location\s*:\s*hybrid|hybrid.{0,80}\b(?:days?|office|on[- ]?site)\b|"
        r"requires? (?:a )?hybrid work schedule)",
        location_text, re.I | re.S,
    )) and not re.search(r"\b(?:ottawa|gatineau|kanata|nepean|national capital region)\b", row["location"] or "", re.I)
    return (
        row["eligibility_queue"] == "qualified"
        and public_http_url(canonical)
        and not prohibited_automation_url(canonical)
        and not aggregator_url(canonical)
        and len(row["description"].strip()) >= 500
        and skill_threshold_met(components.get("skill_match_percent", 0))
        and not explicit_nonlocal_hybrid
        and not outside_resume_domain
    )


class VerificationService:
    def __init__(self, repository: Repository, workers: int = 8):
        self.repository = repository
        self.workers = max(1, min(int(workers), 16))

    def run(self, limit: int = 2000) -> VerificationReport:
        rows = [
            row for row in self.repository.surfaced_jobs(max(1, limit))
            if row["source"] in {"brave", "adzuna"}
            and (
                row["eligibility_queue"] == "qualified"
                or
                not row["canonical_url"]
                or aggregator_url(row["canonical_url"])
                or row["salary_estimated"]
                or row["salary_max"] is None
                or row["workplace_type"] == "unspecified"
                or len(row["description"].strip()) < 500
            )
        ]
        outcomes: List[Tuple[int, Optional[object], Optional[Exception]]] = []

        def resolve(row):
            candidate = replace(self.repository.job_input(row["id"]), canonical_url=row["canonical_url"] or row["url"])
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                return resolve_canonical_job(candidate, client=client)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(resolve, row): row["id"] for row in rows}
            for future in as_completed(futures):
                job_id = futures[future]
                try:
                    outcomes.append((job_id, future.result(), None))
                except Exception as exc:  # Keep one blocked page from stopping the batch.
                    outcomes.append((job_id, None, exc))

        resolved = qualified = rejected = still_review = errors = 0
        for job_id, job, error in outcomes:
            if error is not None or job is None:
                errors += 1
                continue
            try:
                if job.canonical_url:
                    resolved += 1
                    stored_id, _, _ = self.repository.upsert_job(job)
                    job_id = stored_id
                elif self.repository.job(job_id)["canonical_url"]:
                    self.repository.invalidate_web_verification(job_id)
                current = self.repository.job_input(job_id)
                decision, match = evaluate(current), score(current)
                self.repository.save_evaluation(job_id, decision, match)
                if decision.queue == "qualified":
                    qualified += 1
                elif decision.queue == "rejected":
                    rejected += 1
                else:
                    still_review += 1
            except (TypeError, ValueError):
                errors += 1

        preparable = []
        for row in self.repository.surfaced_jobs(max(1, limit)):
            if is_preparable(row) and not self.repository.artifacts(row["id"]):
                preparable.append(row["id"])
        return VerificationReport(
            checked=len(rows),
            resolved=resolved,
            qualified=qualified,
            rejected=rejected,
            still_review=still_review,
            errors=errors,
            preparable_job_ids=tuple(preparable),
        )


def prepare_verified_jobs(
    repository: Repository,
    job_ids: Tuple[int, ...],
    base_resume: Path,
    artifact_root: Path,
    provider=None,
    limit: int = 20,
) -> Tuple[Tuple[int, ...], Tuple[Tuple[int, str], ...]]:
    """Tailor each newly verified job once, continuing past individual failures."""
    from .resume import configured_tailoring_provider, ResumeService

    selected = tuple(job_ids[:max(0, limit)])
    if not selected:
        return (), ()
    provider = provider or configured_tailoring_provider()
    service = ResumeService(repository, Path(base_resume), Path(artifact_root), provider)
    prepared, failures = [], []
    for job_id in selected:
        if repository.artifacts(job_id):
            continue
        try:
            prepared.append(service.prepare(job_id))
        except (OSError, TypeError, ValueError) as exc:
            failures.append((job_id, str(exc)))
    return tuple(prepared), tuple(failures)
