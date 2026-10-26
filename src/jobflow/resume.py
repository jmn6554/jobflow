"""Evidence-constrained, formatting-preserving DOCX tailoring."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from xml.etree import ElementTree as ET

from .models import ResumeEdit, ResumeEvidence, TailoringResult
from .eligibility import evaluate, score
from .repository import Repository


# Document-only operations do not need a network stack (also usable in the
# bundled document renderer). Keep provider imports lazy.
def aggregator_url(url):
    from .providers import aggregator_url as check
    return check(url)


def prohibited_automation_url(url):
    from .providers import prohibited_automation_url as check
    return check(url)


def public_http_url(url):
    from .providers import public_http_url as check
    return check(url)


def resolve_canonical_job(job):
    from .providers import resolve_canonical_job as resolve
    return resolve(job)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PROMPT_VERSION = "resume-tailor-v1"
TECHNOLOGY_LEXICON = {
    "airflow", "alteryx", "aws", "azure", "bigquery", "databricks", "dbt", "delta lake",
    "docker", "excel", "firebase", "gcp", "java", "kafka", "looker", "power bi", "pyspark",
    "python", "react", "redshift", "snowflake", "spark sql", "sql", "tableau", "vba",
}
SECTION_NAMES = {
    "SUMMARY": "professional_summary", "PROFESSIONAL SUMMARY": "professional_summary",
    "SKILLS": "technical_skills", "TECHNICAL SKILLS": "technical_skills",
    "EXPERIENCE": "professional_experience", "PROFESSIONAL EXPERIENCE": "professional_experience",
    "PROJECTS": "selected_technical_projects", "SELECTED TECHNICAL PROJECTS": "selected_technical_projects",
    "EDUCATION": "education",
}


def configured_base_resume():
    return os.environ.get("JOBFLOW_BASE_RESUME") or os.environ.get(
        "JOBFLOW_APPLICATION_RESUME", "private-data/resume.docx")


def configured_tailoring_provider():
    if os.environ.get("JOBFLOW_RESUME_PROVIDER", "disabled").lower() == "openai":
        model = os.environ.get("JOBFLOW_OPENAI_MODEL", "").strip()
        if not model:
            raise ValueError("Set JOBFLOW_OPENAI_MODEL explicitly for optional cloud tailoring")
        return OpenAITailoringProvider(model)
    raise ValueError("Resume tailoring is disabled; applications use the unchanged configured resume")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _document_xml(path: Path) -> ET.Element:
    with zipfile.ZipFile(str(path)) as archive:
        return ET.fromstring(archive.read("word/document.xml"))


def _paragraph_text(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.iter(W + "t")).strip()


def extract_evidence(path: Path) -> List[ResumeEvidence]:
    root = _document_xml(path)
    evidence = []
    section = "header"
    index = 0
    for paragraph in root.iter(W + "p"):
        text = _paragraph_text(paragraph)
        if not text:
            continue
        if text.upper() in SECTION_NAMES:
            section = SECTION_NAMES[text.upper()]
        elif section.startswith("professional_experience") and re.search(
            r"\|.*(?:19|20)\d{2}|\|.*Present", text, re.I
        ):
            # Role-scoped groups prevent reordered bullets from moving beneath
            # a different employer and thereby changing the claim they make.
            section = "professional_experience:p{:03d}".format(index)
        style = paragraph.find(".//" + W + "pStyle")
        style_name = style.get(W + "val", "") if style is not None else ""
        omittable = paragraph.find(".//" + W + "numPr") is not None or "list" in style_name.lower()
        omittable = omittable or bool(re.match(r"^[-•]\s+", text))
        if section.startswith("professional_experience") and not omittable and re.search(
            r"\b(?:19|20)\d{2}\b.*(?:Present|(?:19|20)\d{2})", text, re.I
        ):
            section = "professional_experience:p{:03d}".format(index)
        evidence.append(ResumeEvidence("p{:03d}".format(index), text, section, index, omittable))
        index += 1
    return evidence


def redact_contact(evidence: Iterable[ResumeEvidence]) -> List[Dict]:
    result = []
    for item in evidence:
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL REDACTED]", item.text)
        text = re.sub(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)", "[PHONE REDACTED]", text)
        result.append({
            "evidence_id": item.evidence_id,
            "section": item.section,
            "text": text,
            "omittable": item.omittable,
            "max_replacement_characters": len(item.text),
            "target_replacement_characters": max(40, int(len(item.text) * 0.8)),
        })
    return result


class TailoringProvider(ABC):
    @abstractmethod
    def tailor(self, job: Dict, evidence: List[ResumeEvidence]) -> TailoringResult:
        raise NotImplementedError


class FakeTailoringProvider(TailoringProvider):
    """Deterministic provider for tests and local fixture demonstrations."""

    def tailor(self, job: Dict, evidence: List[ResumeEvidence]) -> TailoringResult:
        summary = next((item for item in evidence if item.section == "professional_summary" and len(item.text) > 80), None)
        edits = [ResumeEdit(summary.evidence_id, summary.text, "Retain verified summary", ())] if summary else []
        base = "{} {}".format(job.get("title", ""), job.get("description", "")).lower()
        terms = [term for term in TECHNOLOGY_LEXICON if term in base and any(term in item.text.lower() for item in evidence)]
        return TailoringResult(edits=edits, ats_terms=sorted(terms), gaps=[], model="fake-tailor")


class OpenAITailoringProvider(TailoringProvider):
    def __init__(
        self,
        model: str = "gpt-5.6-luna",
        api_key: str = "",
        reasoning_effort: str = "",
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.reasoning_effort = reasoning_effort or os.environ.get("JOBFLOW_OPENAI_REASONING", "medium")

    def tailor(self, job: Dict, evidence: List[ResumeEvidence]) -> TailoringResult:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not configured")
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["edits", "omit_evidence_ids", "section_orders", "ats_terms", "gaps"],
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["evidence_id", "replacement", "reason", "jd_terms"],
                        "properties": {
                            "evidence_id": {"type": "string"},
                            "replacement": {"type": "string"},
                            "reason": {"type": "string"},
                            "jd_terms": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "omit_evidence_ids": {"type": "array", "items": {"type": "string"}},
                "section_orders": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["section", "evidence_ids"],
                        "properties": {
                            "section": {"type": "string"},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "ats_terms": {"type": "array", "items": {"type": "string"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
            },
        }
        system = """You tailor a resume to one job description. Return only schema-valid data.
Every replacement must stay fully supported by the supplied resume evidence. Never invent or strengthen an
employer, role, project, tool, duty, date, metric, credential, education item, or accomplishment. You may rewrite
the summary, rewrite existing bullets, use accurate JD terminology, reorder evidence within its existing role-scoped
section, and omit only evidence marked omittable. Prefer omitting the least JD-relevant bullets needed for a clean
one-page resume. Keep replacements concise. Never edit contact data,
employer names, role titles, dates, or education. Cite the exact evidence_id for every replacement. A gap is a JD
requirement not supported by the resume; report it instead of inserting it.

Structural rules:
- Never put the same evidence_id in both edits and omit_evidence_ids.
- Never omit every omittable bullet from any section; at least one bullet must remain in each section.
- section_orders may contain only evidence IDs whose omittable value is true, and every ID must belong to that exact section.
- Every replacement must be no longer than that evidence item's max_replacement_characters value.
- Aim for target_replacement_characters, especially for summaries and bullets; concise evidence is more valuable than rewriting every item.
- An empty omit_evidence_ids or section_orders value is preferred when uncertain."""
        payload = {
            "job": {"title": job["title"], "company": job["company"], "description": job["description"]},
            "resume_evidence": redact_contact(evidence),
        }
        response = client.responses.create(
            model=self.model,
            store=False,
            reasoning={"effort": self.reasoning_effort},
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            text={"format": {"type": "json_schema", "name": "resume_edits", "strict": True, "schema": schema}},
        )
        data = json.loads(response.output_text)
        edits = [
            ResumeEdit(item["evidence_id"], item["replacement"], item["reason"], tuple(item["jd_terms"]))
            for item in data["edits"]
        ]
        orders = {item["section"]: item["evidence_ids"] for item in data["section_orders"]}
        actual_model = getattr(response, "model", None) or self.model
        return TailoringResult(
            edits=edits,
            omit_evidence_ids=data["omit_evidence_ids"],
            section_orders=orders,
            ats_terms=data["ats_terms"],
            gaps=data["gaps"],
            model=actual_model,
            prompt_version=PROMPT_VERSION,
        )


def validate_tailoring(result: TailoringResult, evidence: List[ResumeEvidence]):
    by_id = {item.evidence_id: item for item in evidence}
    all_text = "\n".join(item.text for item in evidence)
    present_technologies = {term for term in TECHNOLOGY_LEXICON if term in all_text.lower()}
    errors = []
    if len(result.omit_evidence_ids) != len(set(result.omit_evidence_ids)):
        errors.append("duplicate omission ids")
    edited_ids = {edit.evidence_id for edit in result.edits}
    for evidence_id in result.omit_evidence_ids:
        item = by_id.get(evidence_id)
        if item is None:
            errors.append("unknown omission id {}".format(evidence_id))
        elif not item.omittable:
            errors.append("non-bullet omission {}".format(evidence_id))
        elif evidence_id in edited_ids:
            errors.append("evidence cannot be both edited and omitted {}".format(evidence_id))
    omission_set = set(result.omit_evidence_ids)
    omittable_groups = {}
    for item in evidence:
        if item.omittable:
            omittable_groups.setdefault(item.section, set()).add(item.evidence_id)
    for section, evidence_ids in omittable_groups.items():
        if evidence_ids and evidence_ids.issubset(omission_set):
            errors.append("at least one bullet must remain in {}".format(section))
    for edit in result.edits:
        if edit.evidence_id not in by_id:
            errors.append("unknown evidence id {}".format(edit.evidence_id))
            continue
        original = by_id[edit.evidence_id]
        locked_heading = original.text.upper() in SECTION_NAMES
        role_heading = original.section.startswith("professional_experience:") and bool(
            re.search(r"\|.*(?:19|20)\d{2}|\|.*Present", original.text, re.I)
        )
        if original.section in {"header", "education"} or locked_heading or role_heading or original.text.endswith(":"):
            errors.append("locked section edit {}".format(edit.evidence_id))
        allowed_numbers = set(re.findall(r"\b\d[\d,+.%$-]*", original.text))
        for number in re.findall(r"\b\d[\d,+.%$-]*", edit.replacement):
            if number not in allowed_numbers:
                errors.append("unsupported number {} in {}".format(number, edit.evidence_id))
        allowed_technologies = present_technologies
        if original.section.startswith("professional_experience:"):
            allowed_technologies = {
                term for term in TECHNOLOGY_LEXICON if term in original.text.lower()
            }
        for term in TECHNOLOGY_LEXICON:
            if term in edit.replacement.lower() and term not in allowed_technologies:
                errors.append("unsupported technology {} in {}".format(term, edit.evidence_id))
        if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", edit.replacement):
            errors.append("contact information may not be edited")
        if len(edit.replacement) > max(len(original.text) * 1.35, len(original.text) + 40):
            errors.append("replacement is too long for {}".format(edit.evidence_id))
    section_ids: Dict[str, set] = {}
    for item in evidence:
        section_ids.setdefault(item.section, set()).add(item.evidence_id)
    for section, ordered_ids in result.section_orders.items():
        if section not in section_ids or not set(ordered_ids).issubset(section_ids[section]):
            errors.append("invalid ordering for {}".format(section))
        elif any(not by_id[evidence_id].omittable for evidence_id in ordered_ids):
            errors.append("only bullets may be reordered in {}".format(section))
    if errors:
        raise ValueError("Tailored resume failed evidence validation: " + "; ".join(errors))


def sanitize_structure(result: TailoringResult, evidence: List[ResumeEvidence]):
    """Drop only structurally invalid model instructions before strict claim validation.

    This never alters replacement text or makes unsupported edits acceptable.
    """
    by_id = {item.evidence_id: item for item in evidence}
    edited_ids = {edit.evidence_id for edit in result.edits}
    omitted = []
    for evidence_id in result.omit_evidence_ids:
        item = by_id.get(evidence_id)
        if item and item.omittable and evidence_id not in edited_ids and evidence_id not in omitted:
            omitted.append(evidence_id)
    for section in {item.section for item in evidence if item.omittable}:
        group = [item.evidence_id for item in evidence if item.section == section and item.omittable]
        if group and all(evidence_id in omitted for evidence_id in group):
            omitted.remove(group[0])
    result.omit_evidence_ids = omitted

    orders = {}
    for section, ordered_ids in result.section_orders.items():
        valid = []
        for evidence_id in ordered_ids:
            item = by_id.get(evidence_id)
            if item and item.omittable and item.section == section and evidence_id not in valid:
                valid.append(evidence_id)
        if len(valid) > 1:
            orders[section] = valid
    result.section_orders = orders


def apply_edits(base_path: Path, output_path: Path, result: TailoringResult, evidence: List[ResumeEvidence]):
    if base_path.resolve() == output_path.resolve():
        raise ValueError("The original resume may not be overwritten")
    validate_tailoring(result, evidence)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    root = _document_xml(base_path)
    paragraph_map = {}
    index = 0
    for paragraph in root.iter(W + "p"):
        if _paragraph_text(paragraph):
            paragraph_map["p{:03d}".format(index)] = paragraph
            index += 1
    for edit in result.edits:
        paragraph = paragraph_map[edit.evidence_id]
        nodes = list(paragraph.iter(W + "t"))
        nodes[0].text = edit.replacement
        for node in nodes[1:]:
            node.text = ""
    _apply_omissions(root, paragraph_map, result.omit_evidence_ids)
    _apply_orders(root, paragraph_map, result.section_orders, evidence)
    with zipfile.ZipFile(str(base_path), "r") as source, zipfile.ZipFile(
        str(output_path), "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            data = ET.tostring(root, encoding="utf-8", xml_declaration=True) if info.filename == "word/document.xml" else source.read(info.filename)
            target.writestr(info, data)


def _apply_omissions(root, paragraph_map, omission_ids):
    if not omission_ids:
        return
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for evidence_id in omission_ids:
        paragraph = paragraph_map[evidence_id]
        parent = parent_map.get(paragraph)
        if parent is not None:
            parent.remove(paragraph)


def _apply_orders(root, paragraph_map, section_orders, evidence):
    if not section_orders:
        return
    parent_map = {child: parent for parent in root.iter() for child in parent}
    section_by_id = {item.evidence_id: item.section for item in evidence}
    for section, ids in section_orders.items():
        ids = [item for item in ids if item in paragraph_map and section_by_id.get(item) == section]
        grouped = {}
        for item in ids:
            parent = parent_map.get(paragraph_map[item])
            if parent is not None and paragraph_map[item] in list(parent):
                grouped.setdefault(parent, []).append(item)
        for parent, ordered_ids in grouped.items():
            nodes = [paragraph_map[item] for item in ordered_ids]
            positions = sorted(list(parent).index(node) for node in nodes)
            if len(positions) != len(nodes):
                continue
            for node in nodes:
                parent.remove(node)
            for position, node in zip(positions, nodes):
                parent.insert(position, node)


def render_pdf(docx_path: Path, output_dir: Path, expected_pages=None) -> Tuple[Path, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice"
    soffice = os.environ.get("JOBFLOW_SOFFICE") or str(bundled)
    if not Path(soffice).exists():
        raise ValueError("LibreOffice is required to create the PDF preview")
    pdf_path = output_dir / (docx_path.stem + ".pdf")
    if pdf_path.exists():
        pdf_path.unlink()
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="jobflow-lo-", dir=temp_root) as profile:
        subprocess.run(
            [soffice, "--headless", "-env:UserInstallation=file://" + profile, "--convert-to", "pdf", "--outdir", str(output_dir), str(docx_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            env=dict(os.environ, TMPDIR=temp_root or tempfile.gettempdir()),
        )
    if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
        raise ValueError("PDF preview was not created")
    page_count = _pdf_page_count(pdf_path)
    if expected_pages is not None and page_count != expected_pages:
        raise ValueError("Tailored resume must be {} page(s); rendered {}".format(expected_pages, page_count))
    return pdf_path, page_count


def _pdf_page_count(path: Path) -> int:
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/pdfinfo"
    pdfinfo = str(bundled) if bundled.exists() else shutil.which("pdfinfo")
    if not pdfinfo:
        data = path.read_bytes()
        values = [int(value) for value in re.findall(rb"/Count\s+(\d+)", data)]
        return max(values) if values else 0
    result = subprocess.run([pdfinfo, str(path)], check=True, stdout=subprocess.PIPE, text=True)
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.M)
    return int(match.group(1)) if match else 0


class ResumeService:
    def __init__(self, repository: Repository, base_resume: Path, artifact_root: Path, provider: TailoringProvider):
        self.repository = repository
        self.base_resume = Path(base_resume)
        self.artifact_root = Path(artifact_root)
        self.provider = provider

    def prepare(self, job_id: int) -> int:
        job_row = self.repository.job(job_id)
        if not self.base_resume.exists():
            raise ValueError("Base resume is missing")
        if job_row["eligibility_queue"] not in {"qualified", "estimated_pay_review"}:
            raise ValueError("Only qualified or pay-review jobs can produce a tailored resume")
        canonical = job_row["canonical_url"]
        if not canonical and job_row["source"] in {"brave", "adzuna"}:
            candidate = replace(self.repository.job_input(job_id), canonical_url=job_row["url"])
            resolved = resolve_canonical_job(candidate)
            if resolved.canonical_url:
                self.repository.upsert_job(resolved)
                self.repository.save_evaluation(job_id, evaluate(resolved), score(resolved))
                job_row = self.repository.job(job_id)
                canonical = job_row["canonical_url"]
        if job_row["eligibility_queue"] not in {"qualified", "estimated_pay_review"}:
            raise ValueError("The verified posting no longer meets the eligibility requirements")
        if not canonical:
            raise ValueError("Resolve a canonical employer or ATS posting before tailoring")
        if not public_http_url(canonical) or prohibited_automation_url(canonical) or aggregator_url(canonical):
            raise ValueError("This source is link-only; resolve the canonical employer posting first")
        if len(job_row["description"].strip()) < 120:
            raise ValueError("A complete job description is required before tailoring")
        evidence = extract_evidence(self.base_resume)
        version = self.repository.next_artifact_version(job_id)
        result = self.provider.tailor(dict(job_row), evidence)
        sanitize_structure(result, evidence)
        validate_tailoring(result, evidence)
        directory = self.artifact_root / "job-{}".format(job_id) / "v{}".format(version)
        docx_path = directory / self.base_resume.name
        apply_edits(self.base_resume, docx_path, result, evidence)
        pdf_path, page_count = render_pdf(docx_path, directory)
        if page_count > 1:
            _compact_to_one_page(result, evidence, job_row["description"], self.base_resume, docx_path, directory)
            pdf_path, page_count = render_pdf(docx_path, directory)
        if page_count != 1:
            raise ValueError("Tailored resume must remain one page; rendered {} page(s)".format(page_count))
        changes = []
        by_id = {item.evidence_id: item for item in evidence}
        for edit in result.edits:
            changes.append({
                "evidence_id": edit.evidence_id,
                "before": by_id[edit.evidence_id].text,
                "after": edit.replacement,
                "reason": edit.reason,
                "jd_terms": list(edit.jd_terms),
            })
        for evidence_id in result.omit_evidence_ids:
            changes.append({
                "evidence_id": evidence_id,
                "before": by_id[evidence_id].text,
                "after": "[OMITTED FOR ONE-PAGE FIT]",
                "reason": "Least relevant existing bullet omitted to preserve the one-page limit",
                "jd_terms": [],
            })
        metadata = {
            "base_resume_hash": file_hash(self.base_resume),
            "jd_hash": text_hash(job_row["description"]),
            "prompt_version": result.prompt_version,
            "model": result.model,
            "docx_path": str(docx_path),
            "pdf_path": str(pdf_path),
            "docx_hash": file_hash(docx_path),
            "change_report": {"changes": changes, "section_orders": result.section_orders,
                              "skipped_edits": result.skipped_edits, "audit_report": result.audit_report,
                              "proposed_numerical_claims": result.proposed_numerical_claims,
                              "tailoring_policy": result.tailoring_policy,
                              "gaps_assessed": True},
            "ats_terms": result.ats_terms,
            "gaps": result.gaps,
        }
        artifact_id = self.repository.create_artifact(job_id, version, "ready", metadata)
        self.repository.transition(job_id, "preparing", "One-page tailored resume generated")
        return artifact_id


def _compact_to_one_page(result, evidence, job_description, base_path, docx_path, directory):
    words = set(re.findall(r"[a-z][a-z0-9+#.-]{2,}", job_description.lower()))
    omitted = set(result.omit_evidence_ids)
    edited = {edit.evidence_id for edit in result.edits}
    group_counts = {}
    for item in evidence:
        if item.omittable:
            group_counts[item.section] = group_counts.get(item.section, 0) + 1
    candidates = sorted(
        (
            item for item in evidence
            if item.omittable and item.evidence_id not in omitted and item.evidence_id not in edited
        ),
        key=lambda item: (
            len(words.intersection(re.findall(r"[a-z][a-z0-9+#.-]{2,}", item.text.lower()))),
            -item.paragraph_index,
        ),
    )
    for item in candidates:
        already_omitted_in_group = sum(
            1 for candidate in evidence
            if candidate.section == item.section and candidate.evidence_id in omitted
        )
        if group_counts.get(item.section, 0) - already_omitted_in_group <= 1:
            continue
        result.omit_evidence_ids.append(item.evidence_id)
        omitted.add(item.evidence_id)
        apply_edits(base_path, docx_path, result, evidence)
        _, pages = render_pdf(docx_path, directory)
        if pages == 1:
            return
    raise ValueError("The resume could not be reduced to one page without removing protected content")
