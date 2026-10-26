"""Import LinkedIn job-alert email links without accessing LinkedIn itself."""

import re
import json
from dataclasses import dataclass, replace
from email.utils import parseaddr
from typing import Iterable, Tuple

from .eligibility import evaluate, score
from .gmail import GMAIL_API, GmailVerificationClient, _header, _message_text, _message_raw
from .models import EligibilityDecision, JobInput
from .providers import infer_compensation, strip_html
from .repository import Repository


ALERT_SENDER = "jobalerts-noreply@linkedin.com"
JOB_LINK = re.compile(r"^View job:\s*https://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d{8,})", re.I)
CANADIAN_LOCATION = re.compile(
    r"\b(?:Ottawa|Gatineau|Kanata|Nepean|Canada|Remote|Toronto|Montreal|Montréal|"
    r"Vancouver|Calgary|Edmonton|Halifax|Winnipeg|Victoria)\b|,\s*(?:ON|QC|BC|AB|NS|MB|SK|NB|NL|PE)\b",
    re.I,
)
LOCAL_LOCATION = re.compile(r"\b(?:Ottawa|Gatineau|Kanata|Nepean)\b", re.I)
REMOTE_LOCATION = re.compile(r"\bremote\b", re.I)
US_LOCATION = re.compile(r"\bUnited States\b|,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b", re.I)
NOISE = re.compile(
    r"\b(?:connections?|alumni|alum|actively hiring|apply with resume|new jobs from|jobs in|"
    r"see all jobs|manage alerts|edit alert)\b",
    re.I,
)
UNWANTED_SPECIALTIES = re.compile(r"\b(?:data scientist|hris|scrum master|servicenow|bilingual)\b", re.I)


@dataclass(frozen=True)
class ImportReport:
    emails_checked: int = 0
    links_found: int = 0
    imported: int = 0
    existing: int = 0
    filtered: int = 0
    new_links: tuple = ()


def parse_alert_jobs(text: str, raw_email: str = '') -> Tuple[JobInput, ...]:
    """Extract only job-card links, discarding email tracking tokens and search links."""
    lines = [strip_html(line).strip() for line in (text or "").splitlines()]
    found = {}
    for index, line in enumerate(lines):
        match = JOB_LINK.match(line)
        if not match:
            continue
        job_id = match.group(1)
        if job_id in found:
            continue
        previous = []
        for value in reversed(lines[max(0, index - 16):index]):
            if value.startswith("View job:") or value.startswith("---"):
                break
            if value and not value.startswith("http") and not NOISE.search(value):
                previous.append(value)
        previous.reverse()
        location_index = next(
            (pos for pos in range(len(previous) - 1, -1, -1)
             if (CANADIAN_LOCATION.search(previous[pos]) or US_LOCATION.search(previous[pos])) and not re.search(r"\$|/\s*year", previous[pos], re.I)),
            None,
        )
        if location_index is None or location_index < 2:
            continue
        title, company, location = previous[location_index - 2:location_index + 1]
        if not title or not company:
            continue
        # Multipart alerts omit workplace type in plain text but include it in
        # the HTML card. Match this exact title/company/location, not another job.
        card = re.search(re.escape(title) + r"\s+" + re.escape(company) + r"\s*·\s*"
                         + re.escape(location) + r"\s*\((Remote|Hybrid|On-site)\)", text, re.I)
        workplace = card.group(1).lower() if card else ('remote' if REMOTE_LOCATION.search(location) else 'unspecified')
        country = 'US' if US_LOCATION.search(location) else 'CA'
        compensation = infer_compensation(" ".join(previous[location_index + 1:]), "USD" if country == 'US' else "CAD")
        if compensation:
            compensation = replace(
                compensation, is_estimated=True, estimate_source="LinkedIn alert email",
                estimate_confidence="low",
            )
        found[job_id] = JobInput(
            title=title, company=company, location=location,
            url="https://www.linkedin.com/jobs/view/{}/".format(job_id),
            description="LinkedIn job-alert email; full description not verified.",
            source="linkedin", external_id=job_id,
            workplace_type=workplace,
            employment_type="contract" if re.search(r"\b(?:contract|temporary|part[- ]time)\b", title, re.I) else "full-time",
            country=country, compensation=compensation,
            raw_payload=json.dumps({'employer_link_candidates': list(dict.fromkeys(
                _card_employer_links(lines, index) + _email_link_candidates(raw_email)))[:20]}),
        )
    return tuple(found.values())


def _email_link_candidates(raw_email):
    # Email-level candidates are hints only. The resolver verifies each page's
    # exact company/title/location before associating it with an alert card.
    from .employer_links import unwrap, direct
    urls = re.findall(r'https?://[^\s<>"\']+', raw_email)
    return list(dict.fromkeys(unwrap(url) for url in urls if direct(unwrap(url))))


def _card_employer_links(lines, index):
    """Only retain URLs within this card; never associate a neighbouring job's link."""
    from .employer_links import unwrap, direct
    start = index
    while start > 0 and not lines[start - 1].startswith(('View job:', '---')):
        start -= 1
    urls = re.findall(r'https?://[^\s<>"\']+', '\n'.join(lines[start:index + 1]))
    return list(dict.fromkeys(unwrap(url) for url in urls if direct(unwrap(url))))


class LinkedInAlertImporter:
    def __init__(self, repository: Repository, gmail: GmailVerificationClient = None):
        self.repository = repository
        self.gmail = gmail or GmailVerificationClient.from_environment()

    def run(self, days: int = 14) -> ImportReport:
        if not self.gmail.connected():
            raise ValueError("Gmail is not connected; reconnect it in JobFlow Settings")
        headers = {"Authorization": "Bearer " + self.gmail._access_token()}
        query = "from:{} newer_than:{}d".format(ALERT_SENDER, max(1, min(int(days), 90)))
        emails_checked = links_found = imported = existing = filtered = 0
        new_links = []
        page_token = ""
        seen_messages = set()
        for _ in range(5):
            params = {"q": query, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            listing = self.gmail._get(GMAIL_API + "/messages", headers=headers, params=params)
            listing.raise_for_status()
            payload = listing.json()
            for item in payload.get("messages") or []:
                message_id = str(item.get("id") or "")
                if not message_id or message_id in seen_messages:
                    continue
                seen_messages.add(message_id)
                response = self.gmail._get(
                    GMAIL_API + "/messages/" + message_id,
                    headers=headers, params={"format": "full"},
                )
                response.raise_for_status()
                message = response.json()
                envelope = message.get("payload") or {}
                if parseaddr(_header(envelope, "From"))[1].lower() != ALERT_SENDER:
                    continue
                emails_checked += 1
                subject = _header(envelope, "Subject")
                for job in parse_alert_jobs(_message_text(envelope), _message_raw(envelope)):
                    links_found += 1
                    if job.title.lower() in subject.lower() and not job.compensation:
                        salary = infer_compensation(subject, "CAD")
                        if salary:
                            job = replace(job, compensation=replace(
                                salary, is_estimated=True, estimate_source="LinkedIn alert subject",
                                estimate_confidence="low",
                            ))
                    previous = self.repository.job_by_external_id("linkedin", job.external_id)
                    decision = (
                        EligibilityDecision("rejected", "rejected", ("specialty outside the requested roles",))
                        if UNWANTED_SPECIALTIES.search(job.title) else evaluate(job)
                    )
                    if decision.queue == "rejected":
                        if previous:
                            self.repository.save_evaluation(previous["id"], decision, score(job))
                        filtered += 1
                        continue
                    if previous:
                        with self.repository.connect() as db:
                            db.execute('UPDATE job_snapshots SET raw_payload=? WHERE job_id=?',
                                       (job.raw_payload, previous['id']))
                        existing += 1
                        continue
                    job_id, is_new, _ = self.repository.upsert_job(job)
                    self.repository.save_evaluation(job_id, decision, score(job))
                    imported += int(is_new)
                    existing += int(not is_new)
                    if is_new:
                        new_links.append((job.title, job.company, job.url))
            page_token = payload.get("nextPageToken") or ""
            if not page_token:
                break
        return ImportReport(emails_checked, links_found, imported, existing, filtered, tuple(new_links))
