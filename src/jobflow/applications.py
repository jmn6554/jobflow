"""Human-approved application preparation and guarded browser submission."""

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .models import SubmissionDecision
from .providers import prohibited_automation_url, public_http_url
from .repository import Repository
from .eligibility import application_guard


SUPPORTED_ATS_HOSTS = {
    "greenhouse": ("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("lever.co", "jobs.lever.co"),
    "ashby": ("ashbyhq.com", "jobs.ashbyhq.com"),
    "workable": ("workable.com", "apply.workable.com"),
    "workday": ("myworkdayjobs.com", "myworkdaysite.com"),
    "smartrecruiters": ("smartrecruiters.com", "jobs.smartrecruiters.com"),
    "icims": ("icims.com",),
    "taleo": ("taleo.net",),
    "breezy": ("breezy.hr",),
    "jobvite": ("jobvite.com",),
    "recruitee": ("recruitee.com",),
    "bamboohr": ("bamboohr.com",),
    "teamtailor": ("teamtailor.com",),
}

MANUAL_ONLY_QUESTION_TERMS = (
    "social security", "social insurance", "ssn", "sin number", "bank account", "routing number",
    "credit card", "password", "date of birth", "birth date", "race", "ethnicity", "gender",
    "sexual orientation", "disability", "veteran status", "religion",
)


def application_provider_allowed(job, policy) -> bool:
    allowed = policy.get('application_providers')
    if not allowed:
        return True
    row = dict(job)
    provider = provider_for_url(row.get('canonical_url') or row.get('url') or '')
    # Official employer wrappers may embed their original ATS form. The live
    # resolved page is checked separately before any account/form interaction.
    if provider == 'employer':
        provider = row.get('source', '')
    return provider in allowed


def stable_hash(value) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_question(question: str) -> str:
    value = question.lower().strip()
    value = re.sub(r"\b(?:this|the)\s+(?:role|position|job)\b", "the role", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def options_hash(options: Iterable[str]) -> str:
    return stable_hash(sorted(normalize_question(option) for option in options))


def provider_for_url(url: str) -> str:
    if not public_http_url(url):
        return "prohibited"
    if prohibited_automation_url(url):
        return "prohibited"
    host = urlparse(url).hostname or ""
    for provider, hosts in SUPPORTED_ATS_HOSTS.items():
        if any(host == allowed or host.endswith("." + allowed) for allowed in hosts):
            return provider
    return "employer"


class AnswerLibrary:
    def __init__(self, repository: Repository):
        self.repository = repository

    def approve(self, question: str, options: List[str], answer: str, jurisdiction="") -> int:
        normalized = normalize_question(question)
        return self.repository.save_answer(question, normalized, options_hash(options), jurisdiction, answer)

    def lookup(self, question: str, options: List[str], jurisdiction="") -> Optional[str]:
        row = self.repository.find_answer(normalize_question(question), options_hash(options), jurisdiction)
        return row["answer"] if row else None


class ApplicationService:
    def __init__(self, repository: Repository, dry_run: bool = True, feature_flags: Optional[Dict[str, bool]] = None):
        self.repository = repository
        self.dry_run = dry_run
        self.feature_flags = feature_flags or {"greenhouse": False, "lever": False, "ashby": False}

    def approve(self, job_id: int, artifact_id: int, fields: Dict, typed_confirmation: str) -> str:
        if not fields:
            raise ValueError("At least one reviewed application field is required")
        manual_only = [
            question for question in fields
            if any(term in normalize_question(question) for term in MANUAL_ONLY_QUESTION_TERMS)
        ]
        if manual_only:
            raise ValueError(
                "Sensitive or demographic questions must be completed manually: " + ", ".join(manual_only)
            )
        job = self.repository.job(job_id)
        if not public_http_url(job["canonical_url"]) or prohibited_automation_url(job["canonical_url"]):
            raise ValueError("A safe canonical employer or ATS URL is required")
        artifact = self.repository.artifact(artifact_id)
        path = Path(artifact["docx_path"])
        if artifact["job_id"] != job_id or not path.is_file() or file_hash(path) != artifact["docx_hash"]:
            raise ValueError("The selected resume file is missing, changed, or belongs to another job")
        if stable_hash(job["description"]) != artifact["jd_hash"]:
            raise ValueError("The job description changed after this resume was generated")
        form_hash = stable_hash({"url": job["canonical_url"], "fields": sorted(fields)})
        answers_hash = stable_hash(fields)
        binding = self.repository.approve_submission(
            job_id, artifact_id, form_hash, answers_hash, typed_confirmation,
            json.dumps(fields, sort_keys=True),
        )
        library = AnswerLibrary(self.repository)
        jurisdiction = job["country"] or ""
        for question, answer in fields.items():
            if not any(term in normalize_question(question) for term in ("resume", "cover letter", "attachment", "upload")):
                library.approve(question, [], str(answer), jurisdiction)
        return binding

    def submit(self, binding_hash: str, fields: Dict) -> SubmissionDecision:
        approval = self.repository.approval(binding_hash)
        if approval is None:
            return SubmissionDecision("blocked", "Approval is missing, invalidated, or does not match the current package")
        job = self.repository.job(approval["job_id"])
        guard = application_guard(job)
        if guard:
            return SubmissionDecision('blocked', guard)
        if self.repository.submissions(job["id"]):
            if any(row["state"] == "submitted" for row in self.repository.submissions(job["id"])):
                return SubmissionDecision("blocked", "This job already has a confirmed submission")
        expected_answers = stable_hash(fields)
        if expected_answers != approval["answers_hash"]:
            return SubmissionDecision("blocked", "Application answers changed after approval")
        expected_form = stable_hash({"url": job["canonical_url"], "fields": sorted(fields)})
        if expected_form != approval["form_hash"]:
            return SubmissionDecision("blocked", "Application fields changed after approval")
        artifact = self.repository.artifact(approval["artifact_id"])
        artifact_path = Path(artifact["docx_path"])
        if (
            stable_hash(job["description"]) != artifact["jd_hash"]
            or not artifact_path.is_file()
            or file_hash(artifact_path) != artifact["docx_hash"]
        ):
            return SubmissionDecision("blocked", "The job description or resume file changed after approval")
        provider = provider_for_url(job["canonical_url"] or job["url"])
        if provider == "prohibited":
            self.repository.record_submission(job["id"], approval["id"], provider, "blocked", detail="Prohibited board automation")
            return SubmissionDecision("blocked", "This job board is never automated")
        if provider == "unsupported":
            self.repository.record_submission(job["id"], approval["id"], provider, "manual_action_required", detail="Unsupported application system")
            return SubmissionDecision("manual_action_required", "Open the employer page and submit the approved package manually")
        if self.dry_run or not self.feature_flags.get(provider, False):
            self.repository.record_submission(job["id"], approval["id"], provider, "manual_action_required", detail="Provider is in dry-run mode")
            return SubmissionDecision("manual_action_required", "The application is prepared; live submission is disabled for this provider")
        return self._browser_submit(job, approval, provider, fields)

    def _browser_submit(self, job, approval, provider: str, fields: Dict) -> SubmissionDecision:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return SubmissionDecision("failed", "Install the browser extra and Playwright browser runtime")
        artifact = self.repository.artifact(approval["artifact_id"])
        resume = self.repository.snapshot_resume(job["id"], Path(artifact["docx_path"]))
        if file_hash(resume) != artifact["docx_hash"]:
            return SubmissionDecision("blocked", "Resume changed before upload")
        receipt = ""
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                page = browser.new_page()
                page.goto(job["canonical_url"] or job["url"], wait_until="domcontentloaded")
                if provider_for_url(page.url) != provider:
                    browser.close()
                    return SubmissionDecision("manual_action_required", "The application redirected to a different or unsupported system")
                page_text = page.locator("body").inner_text().lower()
                if any(marker in page_text for marker in ("captcha", "verify you are human", "security challenge")):
                    browser.close()
                    self.repository.record_submission(job["id"], approval["id"], provider, "blocked", detail="CAPTCHA or security challenge", resume_path=resume)
                    return SubmissionDecision("blocked", "CAPTCHA or security challenge requires manual action")
                for label, value in fields.items():
                    control = page.get_by_label(label, exact=True)
                    if control.count() != 1:
                        browser.close()
                        return SubmissionDecision("manual_action_required", "Application field changed or is ambiguous: " + label)
                    control.fill(str(value))
                file_input = page.locator('input[type="file"]').first
                if file_input.count():
                    file_input.set_input_files(str(resume))
                submit_control = page.locator('button[type="submit"], input[type="submit"]')
                if submit_control.count() != 1:
                    browser.close()
                    return SubmissionDecision("manual_action_required", "The final submit control is missing or ambiguous")
                submit_control.click()
                page.wait_for_load_state("domcontentloaded")
                confirmation = page.locator("body").inner_text()
                if re.search(r"application (?:has been )?received|thank you for applying|application submitted", confirmation, re.I):
                    receipt = page.url
                browser.close()
        except Exception as exc:
            self.repository.record_submission(job["id"], approval["id"], provider, "failed", detail=str(exc), resume_path=resume)
            return SubmissionDecision("failed", "Submission could not be confirmed: " + str(exc))
        if not receipt:
            self.repository.record_submission(job["id"], approval["id"], provider, "manual_action_required", detail="No confirmation evidence", resume_path=resume)
            return SubmissionDecision("manual_action_required", "The site did not provide verifiable submission confirmation")
        self.repository.record_submission(job["id"], approval["id"], provider, "submitted", receipt=receipt, resume_path=resume)
        self.repository.transition(job["id"], "submitted", "Confirmed ATS submission: " + receipt)
        return SubmissionDecision("submitted", "Application submission was confirmed", receipt)
