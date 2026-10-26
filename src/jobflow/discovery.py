"""Discovery orchestration with source isolation and deterministic ranking."""

import os
import re
from dataclasses import replace
from typing import Iterable, List, Optional

from .eligibility import evaluate, score
from .models import Compensation, JobInput
from .providers import DiscoveryProvider, resolve_canonical_job
from .repository import Repository
from .policy import settings, skill_threshold_met


def configured_result_limit(default: int = 2000) -> int:
    try:
        value = int(os.environ.get("JOBFLOW_DISCOVERY_LIMIT", str(default)))
    except ValueError:
        value = default
    return max(30, min(value, 5000))


def excluded_job_urls() -> set:
    return {
        value.strip().rstrip("/")
        for value in os.environ.get("JOBFLOW_EXCLUDED_JOB_URLS", "").split(",")
        if value.strip()
    }


def _safe_error(exc: Exception) -> str:
    """Remove credentials commonly embedded in HTTP exception URLs."""
    return re.sub(
        r"(?i)(app_id|app_key|api_key|token|key)=([^&\s'\"]+)",
        r"\1=[redacted]",
        str(exc),
    )


class DiscoveryService:
    def __init__(self, repository: Repository, providers: Iterable[DiscoveryProvider], result_limit: Optional[int] = None,
                 reevaluate_stored: Optional[bool] = None):
        self.repository = repository
        self.providers = list(providers)
        self.result_limit = configured_result_limit() if result_limit is None else result_limit
        self.reevaluate_stored = reevaluate_stored

    def run(self) -> int:
        run_id = self.repository.start_discovery_run()
        discovered = qualified = review = 0
        errors: List[str] = []
        candidates = []
        excluded_urls = excluded_job_urls()
        # Policy changes apply to previously stored jobs too, avoiding stale
        # queue assignments after a salary or location threshold is updated.
        re_evaluate_stored = self.reevaluate_stored
        if re_evaluate_stored is None:
            re_evaluate_stored = os.environ.get(
                "JOBFLOW_DISCOVERY_REEVALUATE_STORED", "true"
            ).lower() in {"1", "true", "yes", "on"}
        if re_evaluate_stored:
            stored_evaluations = []
            for row in self.repository.jobs(limit=100000):
                stored = self.repository.job_input(row["id"])
                stored_evaluations.append((row["id"], evaluate(stored, ignore_salary=settings()['ignore_salary']), score(stored)))
            self.repository.save_evaluations(stored_evaluations)
        for provider in self.providers:
            count = 0
            provider_error = ""
            try:
                for job in provider.fetch():
                    count += 1
                    discovered += 1
                    if any(
                        value and value.rstrip("/") in excluded_urls
                        for value in (job.url, job.canonical_url)
                    ):
                        continue
                    decision = evaluate(job, ignore_salary=settings()['ignore_salary'])
                    match = score(job)
                    candidates.append((match.total, job, decision, match))
            except Exception as exc:
                safe_error = _safe_error(exc)
                provider_error = safe_error
            board_results = getattr(provider, "board_results", [])
            if board_results:
                self.repository.record_board_runs(run_id, provider.name, board_results)
                failed = sum(row["state"] == "failed" for row in board_results)
                partial = sum(row['state'] == 'partial' for row in board_results)
                if failed or partial:
                    board_message = "{} of {} boards failed; {} incomplete".format(failed, len(board_results), partial)
                    provider_error = "{}; {}".format(provider_error, board_message).strip("; ")
            state = "failed" if provider_error and not board_results else "partial" if provider_error else "completed"
            self.repository.record_provider_run(run_id, provider.name, state, count, provider_error)
            if provider_error:
                errors.append("{}: {}".format(provider.name, provider_error))

        candidates.sort(key=lambda item: item[0], reverse=True)
        surfaced_ids = set()
        for _, job, decision, match in candidates:
            # Search APIs return snippets with query-derived location hints. A
            # potentially eligible result must be resolved to a live individual
            # posting before appearing as verified. Skills do not block this
            # when the configured threshold is disabled.
            if (
                job.source in {"brave", "adzuna"}
                and not job.canonical_url
                and decision.queue != 'rejected'
                and skill_threshold_met(match.skill_match_percent)
            ):
                try:
                    provider_client = next(
                        (provider.client for provider in self.providers if provider.name == job.source),
                        None,
                    )
                    resolved = resolve_canonical_job(
                        replace(job, canonical_url=job.url), client=provider_client
                    )
                except Exception:
                    resolved = replace(job, canonical_url="")
                if resolved.canonical_url:
                    job = resolved
                    decision, match = evaluate(job, ignore_salary=settings()['ignore_salary']), score(job)
            # At large registry sizes, persisting every obviously unrelated
            # role creates tens of thousands of low-value SQLite transactions.
            # Keep relevant-role rejections auditable, but discard jobs whose
            # title is outside the requested domain before storage.
            if (
                decision.queue == "rejected"
                and "outside target or resume-adjacent roles" in decision.reasons
            ):
                continue
            job_id, is_new, changed = self.repository.upsert_job(job)
            if not is_new and not changed:
                stored = self.repository.job_input(job_id)
                decision, match = evaluate(stored, ignore_salary=settings()['ignore_salary']), score(stored)
            if decision.queue == "rejected":
                # Rejected records remain auditable but do not count toward the surfaced pool.
                self.repository.save_evaluation(job_id, decision, match)
                continue
            if job_id in surfaced_ids:
                self.repository.save_evaluation(job_id, decision, match)
                continue
            if len(surfaced_ids) >= self.result_limit:
                continue
            self.repository.save_evaluation(job_id, decision, match)
            surfaced_ids.add(job_id)
            if decision.queue == "qualified":
                qualified += 1
            elif decision.queue == "estimated_pay_review":
                review += 1
        self.repository.finish_discovery_run(run_id, discovered, qualified, review, "; ".join(errors))
        return run_id
