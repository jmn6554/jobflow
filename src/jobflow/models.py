"""Domain models shared by discovery, scoring, tailoring, and submission."""

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


def _number(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("$", "").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Compensation:
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    currency: str = ""
    interval: str = "year"
    is_estimated: bool = False
    estimate_source: str = ""
    estimate_confidence: str = ""
    includes_bonus_or_equity: bool = False

    def annualized(self) -> "Compensation":
        factor = {"hour": 2080, "day": 260, "week": 52, "month": 12}.get(
            self.interval.lower(), 1
        )
        minimum = _number(self.minimum)
        maximum = _number(self.maximum)
        return Compensation(
            minimum=minimum * factor if minimum is not None else None,
            maximum=maximum * factor if maximum is not None else None,
            currency=self.currency.upper(),
            interval="year",
            is_estimated=self.is_estimated,
            estimate_source=self.estimate_source,
            estimate_confidence=self.estimate_confidence,
            includes_bonus_or_equity=self.includes_bonus_or_equity,
        )


@dataclass(frozen=True)
class JobInput:
    title: str
    company: str
    location: str
    url: str
    description: str
    source: str = "csv"
    external_id: Optional[str] = None
    canonical_url: str = ""
    workplace_type: str = "unspecified"
    employment_type: str = "full-time"
    country: str = ""
    language_requirements: str = ""
    sponsorship: str = "unknown"
    security_clearance: str = ""
    posted_at: str = ""
    closes_at: str = ""
    compensation: Optional[Compensation] = None
    raw_payload: str = ""


@dataclass(frozen=True)
class EligibilityDecision:
    state: str
    queue: str
    reasons: tuple = ()
    warnings: tuple = ()
    salary_floor: Optional[float] = None
    salary_currency: str = ""


@dataclass(frozen=True)
class MatchScore:
    total: int
    responsibilities: int
    skills: int
    seniority: int
    domain: int
    compensation: int
    explanation: str
    matched_terms: tuple = ()
    missing_terms: tuple = ()
    skill_match_percent: int = 0
    jd_skills: tuple = ()
    matched_skills: tuple = ()
    missing_skills: tuple = ()
    matched_required: tuple = ()
    missing_required: tuple = ()
    matched_preferred: tuple = ()
    missing_preferred: tuple = ()
    skill_confidence: str = "insufficient"


@dataclass(frozen=True)
class ScoreResult:
    """Compatibility model retained for the original CLI/tests."""

    score: int
    matched_required: tuple
    missing_required: tuple
    matched_preferred: tuple
    explanation: str


@dataclass(frozen=True)
class ResumeEvidence:
    evidence_id: str
    text: str
    section: str
    paragraph_index: int
    omittable: bool = False


@dataclass(frozen=True)
class ResumeEdit:
    evidence_id: str
    replacement: str
    reason: str = ""
    jd_terms: tuple = ()


@dataclass
class TailoringResult:
    edits: List[ResumeEdit] = field(default_factory=list)
    omit_evidence_ids: List[str] = field(default_factory=list)
    section_orders: Dict[str, List[str]] = field(default_factory=dict)
    ats_terms: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    model: str = ""
    prompt_version: str = "resume-tailor-v1"
    skipped_edits: List[Dict] = field(default_factory=list)
    audit_report: List[Dict] = field(default_factory=list)
    proposed_numerical_claims: List[Dict] = field(default_factory=list)
    tailoring_policy: Dict = field(default_factory=dict)

    def as_dict(self) -> Dict:
        return {
            "edits": [asdict(item) for item in self.edits],
            "omit_evidence_ids": self.omit_evidence_ids,
            "section_orders": self.section_orders,
            "ats_terms": self.ats_terms,
            "gaps": self.gaps,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "skipped_edits": self.skipped_edits,
            "audit_report": self.audit_report,
            "proposed_numerical_claims": self.proposed_numerical_claims,
            "tailoring_policy": self.tailoring_policy,
        }


@dataclass(frozen=True)
class SubmissionDecision:
    state: str
    reason: str
    receipt: str = ""
