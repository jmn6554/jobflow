"""Bulk application orchestration using one truthful, reusable resume."""

import json
import copy
import html
import csv
import os
import re
import socket
import subprocess
import time
import zipfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse, parse_qs
from urllib.request import urlopen, Request
from xml.etree import ElementTree

from .accounts import AccountAutomation, AccountStore
from .consent import application_privacy_answer
from .applications import MANUAL_ONLY_QUESTION_TERMS, normalize_question, provider_for_url
from .gmail import GmailVerificationClient
from .models import SubmissionDecision
from .providers import aggregator_url, prohibited_automation_url, public_http_url
from .repository import Repository
from .verification import is_preparable
from .form_questions import REQUIRED_QUESTIONS, MISSING_QUESTIONS, QUESTION_HELPERS
from .eligibility import cybersecurity_role, application_guard
from .chrome_scope import ScopedChromeConnection
from .profile_records import record_answer, bind_existing_records

APPLICATION_SUBMIT_BUTTON = re.compile(
    r'^(?:submit(?: your)?(?: application)?|send application|envoyer (?:la|ma) candidature|soumettre (?:la|ma) candidature)$',
    re.I,
)


@dataclass(frozen=True)
class BulkApplicationReport:
    considered: int
    submitted: int
    skipped_existing: int
    manual_action_required: int
    confirmation_unknown: int
    blocked: int
    failed: int


class ApplicationProfile:
    def __init__(self, values: Dict):
        self.values = values
        self._form_answers = {}

    @staticmethod
    def _work_country_question(question):
        return bool(re.search(r'country or countries you anticipate working in', normalize_question(question)))

    @staticmethod
    def _selected_location_reference(question):
        return bool(re.search(r'locations?(?: s)? you selected.*previous response', normalize_question(question)))

    def bind_form_countries(self, fields, job):
        """Bind dependent authorization to a visible/planned choice, not residence.

        Ephemeral per-form answers must never leak into another application.
        Multiple/unknown selected countries need review, not a guessed yes/no.
        """
        self._form_answers = {}
        chosen = None
        aliases = {'canada': 'CA', 'us': 'US', 'usa': 'US', 'united states': 'US'}
        for field in fields:
            question = field['question']
            key = normalize_question(question)
            if self._work_country_question(question):
                answer = (field.get('selectedAnswer') if field.get('selected') else
                          {'CA': 'Canada', 'US': 'US'}.get(self.jurisdiction(job)))
                chosen = aliases.get(normalize_question(answer or ''))
                self._form_answers[key] = answer if chosen else None
            elif self._selected_location_reference(question):
                facts = self.values.get('work_authorization', {}).get(chosen, {})
                fact = ('requires_sponsorship_now_or_future' if re.search(r'sponsor|work permit', key)
                        else 'authorized' if re.search(r'authoriz|authoris', key) else None)
                value = facts.get(fact)
                self._form_answers[key] = None if value is None else 'Yes' if value else 'No'
            elif key in {'work authorization', 'work authorisation'}:
                country = self.jurisdiction(job)
                facts = self.values.get('work_authorization', {}).get(country, {})
                if facts.get('citizen') is True and facts.get('authorized') is True:
                    for option in field.get('options', []):
                        if normalize_question(option) == 'i am authorized to work in the country due to my nationality':
                            self._form_answers[key] = option
                            break
            elif key == 'which option best describes your situation':
                options = {normalize_question(option): option for option in field.get('options', [])}
                never = 'i will not require sponsorship now or in the future'
                future = 'i do not require sponsorship now but i may require sponsorship in the future'
                now = 'i require sponsorship now'
                if set(options) == {never, future, now}:
                    # A vague prompt becomes unambiguous only with this entire
                    # visible set of work-sponsorship choices and known facts.
                    country = chosen if any(self._work_country_question(f['question']) for f in fields) else self.jurisdiction(job)
                    facts = self.values.get('work_authorization', {}).get(country, {})
                    authorized = facts.get('authorized')
                    sponsorship = facts.get('requires_sponsorship_now_or_future')
                    answer = never if authorized is True and sponsorship is False else now if authorized is False and sponsorship is True else None
                    self._form_answers[key] = options.get(answer)

    @staticmethod
    def _job_key(job):
        return tuple(str(job[k] or '') if k in job.keys() else ''
                     for k in ('id', 'canonical_url', 'company', 'location'))

    @classmethod
    def load(cls, path: Path):
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("Application profile must contain a JSON object")
        return cls(values)

    def jurisdiction(self, job) -> str:
        """Choose the truthful jurisdiction the candidate will apply from."""
        explicit = str(job["country"] or "").upper().strip()
        if explicit in {"CA", "CAN", "CANADA"}:
            return "CA"
        if explicit in {"US", "USA", "UNITED STATES"}:
            return "US"
        if explicit:
            return explicit  # Never replace foreign country evidence with US boilerplate.
        raw_location = str(job['location'] or '').strip()
        us_states = ('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT '
                     'NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC').split()
        # Require city/state syntax, never an incidental abbreviation like IN.
        if re.search(r',\s*(?:' + '|'.join(us_states) + r')(?:\s+\d{5}(?:-\d{4})?)?$', raw_location):
            return 'US'
        if re.search(r',\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)$', raw_location):
            return 'CA'
        if re.search(r'(?:,|/)\s*(?:Ontario|Quebec|Québec|British Columbia|Alberta|Manitoba|Saskatchewan|Nova Scotia|New Brunswick|Newfoundland and Labrador|Prince Edward Island|Yukon|Northwest Territories|Nunavut)\s*$', raw_location, re.I):
            return 'CA'
        for name, code in {'Hungary':'HU', 'India':'IN', 'Germany':'DE', 'Portugal':'PT',
                           'Spain':'ES', 'United Kingdom':'GB', 'Algeria':'DZ', 'Lithuania':'LT',
                           'Hong Kong':'HK', 'South Africa':'ZA', 'Australia':'AU'}.items():
            if re.search(r'\b' + re.escape(name) + r'\b', raw_location, re.I):
                return code
        location = normalize_question(str(job["location"] or ""))
        try:
            description_value = job["description"]
        except (KeyError, IndexError):
            description_value = ""
        description = normalize_question(str(description_value or ""))
        if "canada" in location:
            # Mixed Canada/US remote postings should use the candidate's
            # Canadian authorization and location.
            return "CA"
        if any(term in location for term in ("united states", "remote us", "remote u s", "usa")):
            return "US"
        if re.fullmatch(r'u\s?s(?: remote)?', location):
            return "US"
        if any(term in description for term in (
            "authorized to work in the united states", "authorized to work in the us",
            "legally authorized to work in the united states", "u s work authorization",
        )):
            return "US"
        try:
            salary_currency = str(job["salary_currency"] or "").upper()
        except (KeyError, IndexError):
            salary_currency = ""
        if location == "remote" and salary_currency == "USD" and "canada" not in description:
            return "US"
        evidence = getattr(self, '_form_country_evidence', None)
        if evidence and evidence[0] == self._job_key(job):
            return evidence[1]
        if self.values.get('require_job_country_evidence'):
            return ''
        identity_country = normalize_question(str(self.values.get("identity", {}).get("country") or ""))
        return "CA" if identity_country == "canada" else "US" if identity_country in {"us", "usa", "united states"} else ""

    def answer(self, question, country, employer="", job_location=""):
        """Return only explicitly saved facts; unknown questions remain unanswered.

        Employer-specific answers win over global exact-question answers. No
        narrative, consent, demographic or eligibility facts are inferred.
        """
        normalized = normalize_question(question)
        if normalized in self._form_answers:
            return self._form_answers[normalized]
        for company, answers in self.values.get('employer_answers', {}).items():
            if normalize_question(company) == normalize_question(employer):
                for label, value in answers.items():
                    if normalize_question(label) == normalized and value is not None:
                        return ('Yes' if value else 'No') if isinstance(value, bool) else str(value)
        for label, value in self.values.get('additional_answers', {}).items():
            if normalize_question(label) == normalized and value is not None:
                return ('Yes' if value else 'No') if isinstance(value, bool) else str(value)
        if re.match(r'^(employment|education) record \d+ ', normalized):
            return record_answer(self.values, normalized)
        identity = self.values.get('identity', {})
        labels = {
            'name': 'full_name', 'full name': 'full_name',
            'first name': 'first_name', 'last name': 'last_name',
            'email': 'email', 'email address': 'email',
            'phone': 'phone', 'phone number': 'phone',
            'city': 'city', 'current city': 'city', 'location': 'location',
            'current location': 'location', 'country': 'country',
            'state': 'province', 'province': 'province', 'state province': 'province',
            'postal code': 'postal_code', 'zip code': 'postal_code',
            'address': 'street_address', 'street address': 'street_address',
            'linkedin': 'linkedin', 'linkedin profile': 'linkedin',
            'linkedin url': 'linkedin', 'linkedin profile url': 'linkedin',
            'website': 'website', 'portfolio': 'portfolio',
        }
        if normalized in labels:
            value = identity.get(labels[normalized])
            return str(value) if value else None
        facts = self.values.get('work_authorization', {}).get(country, {})
        # A question naming another country must not reuse this job's country.
        named = 'CA' if 'canada' in normalized else 'US' if re.search(r'\b(?:united states|usa|u s)\b', normalized) else None
        if named and named != country:
            return None
        if re.fullmatch(r'(?:are you )?(?:legally )?(?:authorized|authorised|eligible) to work(?: in .+)?', normalized):
            value = facts.get('authorized')
            return None if not isinstance(value, bool) else 'Yes' if value else 'No'
        if re.fullmatch(r'(?:will|do) you (?:now or in the future )?(?:require|need) (?:visa |employment |work )?sponsorship(?: now or in the future)?', normalized):
            value = facts.get('requires_sponsorship_now_or_future')
            return None if not isinstance(value, bool) else 'Yes' if value else 'No'
        if normalized in {'how did you hear about us', 'how did you hear about this opportunity'}:
            return self.values.get('application_source')
        if normalized in {'salary expectations', 'expected annual salary', 'desired salary'}:
            value = self.values.get('salary_expectations', {}).get(country)
            return str(value) if value is not None else None
        return None


class LocalAnswerProfile(ApplicationProfile):
    """Verified answers for this form/job only; never writes to the saved profile."""
    def __init__(self, values):
        super().__init__(values)
        self.local_answers = {}

    def preset_answer(self, question, country, employer='', job_location=''):
        return super().answer(question, country, employer, job_location)

    def remember_local_answer(self, question, country, employer, job_location, answer):
        self.local_answers[(normalize_question(question), country, employer, job_location)] = answer

    def answer(self, question, country, employer='', job_location=''):
        key = (normalize_question(question), country, employer, job_location)
        if key in self.local_answers:
            return self.local_answers[key]
        saved = self.preset_answer(question, country, employer, job_location)
        return saved


class OpenAIApplicationAnswerProvider:
    """Answer novel application questions using only resume/profile evidence."""

    def __init__(self, model: str = "", api_key: str = "", reasoning_effort: str = ""):
        self.model = model or os.environ.get("JOBFLOW_OPENAI_MODEL", "")
        if not self.model:
            raise ValueError("Set JOBFLOW_OPENAI_MODEL explicitly before enabling cloud answers")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.reasoning_effort = reasoning_effort or os.environ.get("JOBFLOW_OPENAI_REASONING", "medium")
        self._resume_cache: Dict[str, str] = {}
        self._answer_cache: Dict[Tuple[int, str, Tuple[str, ...]], Optional[str]] = {}

    def _resume_text(self, resume: Path) -> str:
        key = str(Path(resume).resolve())
        if key in self._resume_cache:
            return self._resume_cache[key]
        with zipfile.ZipFile(key) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        text = " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
        text = re.sub(r"[\w.+-]+@[\w.-]+", "[email redacted]", text)
        text = re.sub(r"(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}", "[phone redacted]", text)
        self._resume_cache[key] = text[:18000]
        return self._resume_cache[key]

    def answer(self, question: str, options: List[str], job, resume: Path, profile: ApplicationProfile) -> Optional[str]:
        if not self.api_key:
            return None
        key = (int(job["id"]), normalize_question(question), tuple(options))
        if key in self._answer_cache:
            return self._answer_cache[key]
        from openai import OpenAI

        identity = profile.values.get("identity", {})
        safe_profile = {
            "location": identity.get("location"),
            "country": identity.get("country"),
            "current_company": identity.get("current_company"),
            "work_authorization": profile.values.get("work_authorization", {}),
            "education": profile.values.get("education", {}),
            "experience": profile.values.get("experience", {}),
            "employment_history": profile.values.get("employment_history", {}),
            "additional_answers": profile.values.get("additional_answers", {}),
        }
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["supported", "answer"],
            "properties": {
                "supported": {"type": "boolean"},
                "answer": {"type": "string"},
            },
        }
        system = """Answer one job-application question using only the supplied resume and explicit profile facts.
Never invent, exaggerate, or infer an unstated credential, employer, duty, tool, metric, visa, or number of years.
For an open-ended question, write a direct professional answer of at most 500 characters grounded in the evidence.
When options are supplied, copy exactly one supplied option and only when the evidence supports it.
Set supported=false and answer to an empty string when the evidence is insufficient. Do not answer protected-trait
questions. The applicant is applying from Canada when the posting permits Canada."""
        payload = {
            "question": question,
            "options": options,
            "job": {
                "title": job["title"], "company": job["company"],
                "location": job["location"], "description": str(job["description"] or "")[:16000],
            },
            "profile_facts": safe_profile,
            "resume": self._resume_text(resume),
        }
        response = OpenAI(api_key=self.api_key).responses.create(
            model=self.model, store=False, reasoning={"effort": self.reasoning_effort},
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            text={"format": {"type": "json_schema", "name": "application_answer", "strict": True, "schema": schema}},
        )
        data = json.loads(response.output_text)
        answer = str(data.get("answer") or "").strip() if data.get("supported") else ""
        self._answer_cache[key] = answer or None
        return self._answer_cache[key]


class PlaywrightFormAdapter:
    """Fill direct public application forms, stopping on any unknown required field."""

    def __init__(
        self,
        headless: bool = True,
        evidence_root: Path = Path("artifacts/submission-receipts"),
        gmail_client=None,
        browser_profile: Optional[Path] = None,
        account_store=None,
        answer_provider=None,
        browser_type: str = "chromium",
    ):
        if browser_type not in {"chromium", "firefox"}:
            raise ValueError("Unsupported application browser")
        self.browser_type = browser_type
        self.headless = headless
        self.evidence_root = Path(evidence_root)
        if gmail_client is None:
            configured_gmail = GmailVerificationClient.from_environment()
            gmail_client = (
                configured_gmail
                if configured_gmail.configured() and configured_gmail.connected()
                else None
            )
        self.gmail_client = gmail_client
        self.browser_profile = Path(
            browser_profile or os.environ.get("JOBFLOW_BROWSER_PROFILE", "private-data/browser-profile")
        ).resolve()
        self.account_automation = AccountAutomation(
            account_store or AccountStore.from_environment(), gmail_client
        )
        self.answer_provider = answer_provider
        ai_enabled = os.environ.get("JOBFLOW_APPLICATION_AI_ENABLED", "false").lower() in {
            "1", "true", "yes", "on",
        }
        if (self.answer_provider is None and ai_enabled and os.environ.get("OPENAI_API_KEY")):
            self.answer_provider = OpenAIApplicationAnswerProvider()

    def _preset_for(
        self, profile: ApplicationProfile, question: str, job, resume: Path,
        options: Optional[List[str]] = None,
    ) -> Optional[str]:
        preset = getattr(profile, 'preset_answer', profile.answer)
        answer = preset(
            question, profile.jurisdiction(job), job["company"] or "", job["location"] or ""
        )
        if answer is not None:
            answer = self._compatible_answer(question, answer, options)
        return answer

    def _prefetch_answers(self, fields, profile, job, resume):
        provider = self.answer_provider
        if provider is None or not hasattr(provider, 'answer_many'):
            return
        from .answer_policy import manual_only
        pending = []
        for field in fields:
            question, options = field['question'], field['options']
            normalized = normalize_question(question)
            if (field.get('selected') or field['kind'] == 'file' or manual_only(question)
                    or normalized.startswith(('if ', 'unless ', 'si ', 'employment record ', 'education record '))
                    or 'application password' in normalized):
                continue
            if self._preset_for(profile, question, job, resume, options) is None:
                pending.append((question, options))
        for offset in range(0, len(pending), 4):
            group = pending[offset:offset + 4]
            if not group:
                break
            print('QUESTION_BATCH: job #{}; {} unanswered questions'.format(job['id'], len(group)), flush=True)
            try:
                results = provider.answer_many(group, job, getattr(profile, 'evidence_resume', resume), profile)
            except Exception:
                return  # Fill-time path retains abstention/error handling.
            if any(results.get((q, tuple(o))) is None for q, o in group):
                break  # Do not spend on later groups when one required answer is missing.

    def _answer_for(
        self, profile: ApplicationProfile, question: str, job, resume: Path,
        options: Optional[List[str]] = None,
    ) -> Optional[str]:
        answer = self._preset_for(profile, question, job, resume, options)
        if answer is not None or self.answer_provider is None:
            return answer
        if any(re.search(r'\b' + re.escape(term) + r'\b', normalize_question(question))
               for term in MANUAL_ONLY_QUESTION_TERMS):
            return None
        try:
            answer = self.answer_provider.answer(question, options or [], job,
                getattr(profile, 'evidence_resume', resume), profile)
            if answer is not None:
                answer = self._compatible_answer(question, answer, options)
            if answer is not None and hasattr(profile, 'remember_local_answer'):
                profile.remember_local_answer(question, profile.jurisdiction(job),
                                              job['company'] or '', job['location'] or '', answer)
            return answer
        except Exception:
            # A model outage must stop the field safely, not abort the batch or
            # cause a guessed answer to be submitted.
            return None

    @staticmethod
    def _compatible_answer(question, answer, options):
        """Check form shape, not factual semantics (the local reviewer does that)."""
        normalized = normalize_question(question)
        # Typeahead results aren't a complete choice set until text is entered.
        dynamic = (normalized in {'school', 'school or university', 'location city', 'phone country code'}
                   or bool(re.fullmatch(r'education record \d+ school', normalized)))
        if not options or dynamic:
            return answer
        # A checkbox can expose the question itself instead of a Yes option.
        if (len(options) == 1 and normalize_question(options[0]) == normalized
                and normalize_question(str(answer)) in {'yes', 'i agree', 'acknowledge', 'confirmed'}):
            return answer
        return AssistedApplicationService._matching_option_text(options, str(answer), question)

    @staticmethod
    def _ashby_unanswered_required_questions(surface, profile: ApplicationProfile, job, answer_for=None) -> List[str]:
        """Inspect Ashby's initial form before uploading or entering candidate data."""
        unanswered = []
        for label in surface.locator("label.ashby-application-form-question-title").all():
            if not label.is_visible() or "_required_" not in (label.get_attribute("class") or ""):
                continue
            question = label.inner_text().strip()
            field_type = label.evaluate(
                "el => el.parentElement?.querySelector('input,textarea,select')?.type || ''"
            )
            if field_type == "file":
                continue
            group = label.locator('..')
            options = group.locator('select option,button[data-option]').all_text_contents()
            radios = label.locator("xpath=..//input[@type='radio']")
            if radios.count():
                options = [AssistedApplicationService._label(radio) for radio in radios.all()]
            answer = (answer_for(question, options) if answer_for is not None else profile.answer(
                question, profile.jurisdiction(job), job["company"] or "", job["location"] or ""
            ))
            if answer is None:
                unanswered.append(question or "unnamed required question")
                continue
            radios = label.locator("xpath=..//input[@type='radio']")
            if radios.count():
                options = [AssistedApplicationService._label(radio) for radio in radios.all()]
                if AssistedApplicationService._matching_option_text(options, answer, question) is None:
                    unanswered.append(question or "unmatched required choice")
        return list(dict.fromkeys(unanswered))

    @staticmethod
    def _captcha_present(page) -> bool:
        bodies = []
        for frame in page.frames:
            try:
                if frame.locator('body').count():
                    bodies.append(frame.locator('body').inner_text().lower())
            except Exception:
                # Analytics frames can disappear while we inspect them. A
                # detached frame cannot contain a live challenge; errors on
                # live frames still fail closed instead of assuming no CAPTCHA.
                if not frame.is_detached():
                    raise
        body = ' '.join(bodies)
        # Greenhouse and other ATS pages routinely embed an *invisible*
        # reCAPTCHA badge on every application.  Its mere presence is not a
        # user challenge and must not send every job to the exception queue.
        text_marker = any(marker in body for marker in (
            "verify you are human", "complete the captcha", "solve the captcha",
            "captcha challenge",
        ))
        widgets = page.locator(
            'iframe[src*="captcha" i]:not([src*="size=invisible" i]), '
            'iframe[src*="recaptcha" i]:not([src*="size=invisible" i]), '
            'iframe[src*="hcaptcha" i]:not([src*="size=invisible" i]), '
            '.h-captcha'
        )
        visible_widget = any(widget.is_visible() for widget in widgets.all())
        return text_marker or visible_widget

    @staticmethod
    def _form_surface(page):
        for frame in page.frames:
            try:
                if frame.locator('input[type="password"]:visible').count():
                    continue
                # SuccessFactors retains an uploaded resume and uses text
                # controls for email; neither a file input nor type=email is
                # guaranteed on a returning applicant's form. Identify the
                # requisition-specific career form before looking for Apply,
                # which is the FINAL submission control on this surface.
                parsed = urlparse(frame.url)
                if (parsed.hostname or '').endswith('.successfactors.com') and parse_qs(parsed.query).get('career_job_req_id'):
                    if frame.locator('form#careerform').count() and frame.locator('[id$="_submitBtn"][role="button"]').count():
                        return frame
                # Multi-step application wizards can hide their file input
                # until the resume step and expose Next as an accessible link.
                if re.search(r'/applyRequisition$', urlparse(frame.url).path, re.I):
                    query = parse_qs(urlparse(frame.url).query)
                    navigation = frame.get_by_role('link', name=re.compile(r'^next$', re.I))
                    final_action = frame.get_by_role('button', name=re.compile(r'^submit$', re.I))
                    if query.get('rid') and frame.locator('input[name="candESignature"]:visible').count() and any(
                        c.is_visible() for c in final_action.all()
                    ):
                        return frame
                    if query.get('rid') and frame.locator('input[type="file"]').count() and frame.locator(
                        'input:visible:not([type=hidden]),select:visible,textarea:visible'
                    ).count() and any(c.is_visible() for c in navigation.all()):
                        return frame
                # Some employer landing pages collect identity before handing
                # off to their ATS. Require an explicit application action in
                # the same form so newsletters/talent communities don't qualify.
                for form in frame.locator('form:visible').all():
                    email = form.locator('input[type="email"]:visible, input[autocomplete="email"]:visible')
                    name = form.locator('input[autocomplete="given-name"]:visible, input[name*="firstName" i]:visible, input[name*="first_name" i]:visible')
                    action = form.get_by_role('button', name=re.compile(r'^continue (?:your )?application$', re.I))
                    if email.count() and name.count() and any(button.is_visible() for button in action.all()):
                        return frame
                if frame.locator('[data-automation-id="pageFooterNextButton"]:visible, [data-automation-id="pageFooterSubmitButton"]:visible').count():
                    return frame
                if re.search(r'/apply(?:/|$)', urlparse(frame.url).path, re.I):
                    headings = [normalize_question(text) for text in frame.locator('h1,h2,h3,h4,h5,h6,[role=heading]').all_text_contents()]
                    # Later voluntary-disclosure steps no longer contain the
                    # identity or resume controls used to recognize step one.
                    demographic_heading = any(re.search(
                        r'equal employment opportunity|voluntary self identification|self identification information',
                        text) for text in headings)
                    final_action = frame.get_by_role('button', name=re.compile(
                        r'^(?:submit(?: your)? application|send application)$', re.I))
                    if demographic_heading and frame.locator('select:visible,input[type=radio]:visible').count() and any(
                        button.is_visible() for button in final_action.all()
                    ):
                        return frame
                    step_heading = any(re.fullmatch(r'(?:work experience|my experience|education|application questions|review application)(?:\s*progress will be auto[- ]saved)?', text) for text in headings)
                    # Some ATS questionnaires expose internal section names
                    # instead of a meaningful heading, but retain job/step IDs.
                    query = parse_qs(urlparse(frame.url).query)
                    identified_step = bool(query.get('jobSeqNo') and query.get('step'))
                    navigation = frame.get_by_role('button', name=re.compile(r'^(?:next(?: step)?|continue|save and continue|review application|submit(?: application)?)$', re.I))
                    field_count = frame.locator('input:visible:not([type=hidden]),select:visible,textarea:visible').count()
                    if (identified_step or (step_heading and field_count >= 3)) and any(c.is_visible() for c in navigation.all()):
                        return frame
                if frame.locator('input[type="file"]:visible').count():
                    return frame
                # Ashby hides native upload inputs behind visible upload widgets.
                if frame.locator('input[type="file"]').count() and frame.locator(
                    'input[type="email"]:visible, input[autocomplete="email"]:visible, '
                    'input[name="email" i]:visible, input[placeholder="Email" i]:visible'
                ).count():
                    return frame
                # Greenhouse removes the file input once a resume is attached.
                if frame.locator('input#email:visible').count() and frame.locator('input#first_name:visible').count():
                    return frame
                if frame.locator('input[type="email"]:visible').count() and frame.locator(
                    'input[autocomplete="given-name"]:visible, input[name*="firstName" i]:visible, input[name*="first_name" i]:visible'
                ).count() and re.search(r'apply|application|candidate', frame.url, re.I):
                    return frame
            except Exception:
                continue
        return None

    @staticmethod
    def _dismiss_consent(page) -> None:
        """Close common privacy overlays using the least-permissive visible choice."""
        # Klaviyo email-capture overlays are marketing, not ATS consent.
        # Dismiss only an explicit negative/close action; never sign up.
        try:
            overlays = page.locator('[data-kl-scroll-locking-modal="true"]:visible')
            for overlay in overlays.all():
                close = overlay.get_by_role('button', name=re.compile(r'^(?:No Thanks|Close dialog)$', re.I))
                if close.count():
                    close.first.click(timeout=3000)
        except Exception:
            pass
        choices = (
            r"^necessary only$", r"^reject all(?: (?:optional )?cookies)?$",
            r"^reject optional cookies$", r"^decline all(?: (?:optional )?cookies)?$",
            r"^(?:decline|reject) non[-\s]essential(?: cookies)?$",
            r"^decline$", r"^deny$", r"^disagree$",
            r"^continue without accepting$", r"^accept cookies$",
            r"^click to decline all cookies$",
        )
        # Embedded ATS forms can mount their own OneTrust dialog inside an
        # iframe. Searching only the top-level page leaves that overlay in
        # place, where it intercepts every custom dropdown click.
        surfaces = getattr(page, "frames", None) or [page]
        for surface in surfaces:
            # Advertising/challenge frames are not application consent surfaces.
            # Inspect only the employer origin and recognized ATS frames; an
            # unrelated cross-origin frame can otherwise wedge a DOM query.
            surface_url = getattr(surface, 'url', '')
            if surface_url and urlparse(surface_url).hostname != urlparse(page.url).hostname:
                if provider_for_url(surface_url) in {'employer', 'prohibited'}:
                    continue
            for pattern in choices:
                try:
                    buttons = surface.get_by_role("button", name=re.compile(pattern, re.I)).all()
                    button = next((item for item in buttons if item.is_visible() and item.is_enabled()), None)
                    if button is not None:
                        button.click(timeout=4000)
                        page.wait_for_timeout(500)
                        return
                except Exception:
                    continue

    @staticmethod
    def _confirmation(page, responses: List[Dict]) -> Tuple[bool, str]:
        body = page.locator("body").inner_text()
        url = page.url
        sent_receipt = any(re.fullmatch(r'application sent[!.]?', text.strip(), re.I)
                           for text in page.locator('h1,h2,h3,[role=heading]').all_text_contents()) and bool(
            re.search(r'we will review your application', body, re.I))
        ashby_success = False
        try:
            containers = page.locator(".ashby-application-form-success-container:visible")
            ashby_success = bool(containers.count() and re.search(r"\bsuccess\b", containers.first.inner_text(), re.I))
        except Exception:
            pass
        text_success = sent_receipt or re.search(
            r"\byour application has been sent[.!]\s*thank you[.!]|"
            r"application (?:(?:has been|was) (?:successfully )?)?(?:received|submitted)|thank you for applying|"
            r"thanks for applying|"
            r"thank you for submitting (?:your )?application|we(?:(?:'|’)ve| have)? received your application|"
            r"successfully submitted",
            body,
            re.I,
        )
        url_success = re.search(r"(?:thank[-_]?you|application[-_]?submitted|submission[-_]?success)", url, re.I)
        response_success = next((
            item for item in responses
            if 200 <= item.get("status", 0) < 300
            and re.search(r"application.?id|candidate.?id|success|submitted|received", item.get("body", ""), re.I)
            and not re.search(r'"success"\s*:\s*false|\berror\b', item.get("body", ""), re.I)
        ), None)
        signal = (
            "Ashby success panel" if ashby_success else
            "page confirmation" if text_success else
            "confirmation URL" if url_success else
            "ATS success response" if response_success else ""
        )
        return bool(ashby_success or text_success or url_success or response_success), signal

    def _capture_evidence(self, page, job_id: int) -> str:
        folder = self.evidence_root / "job-{}".format(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = folder / "{}.png".format(stamp)
        page.screenshot(path=str(path), full_page=True)
        return str(path.resolve())

    def _submit_workday(self, page, job, resume: Path, profile: ApplicationProfile) -> SubmissionDecision:
        """Complete Workday's authenticated multi-step application wizard."""
        helper = AssistedApplicationService.__new__(AssistedApplicationService)
        responses = []

        def capture_response(response):
            try:
                if response.request.method == "POST" and re.search(
                    r"apply|application|candidate|submission", response.url, re.I
                ):
                    body = response.text()[:5000] if response.status < 400 else ""
                    responses.append({"url": response.url, "status": response.status, "body": body})
            except Exception:
                return

        page.on("response", capture_response)
        for _ in range(10):
            # Workday may expose a job-detail Apply button and then a separate
            # application-method chooser before the wizard exists.
            job_apply = page.locator(
                'a[data-automation-id="adventureButton"]:visible, '
                'a[role="button"]:visible:text-is("Apply")'
            )
            if job_apply.count():
                href = job_apply.last.get_attribute("href")
                if href:
                    page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1800)
                    continue
            manual = page.locator(
                'a[data-automation-id="applyManually"]:visible, '
                'a:visible:text-is("Apply Manually")'
            )
            if manual.count():
                href = manual.last.get_attribute("href")
                if href:
                    page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1800)
                    continue
            try:
                page.locator(
                    '[data-automation-id="pageFooterNextButton"], '
                    '[data-automation-id="pageFooterSubmitButton"]'
                ).first.wait_for(state="visible", timeout=10000)
                # Workday paints the footer before hydrating the current step's
                # inputs and dropdowns.
                page.wait_for_timeout(2500)
                for _loading_wait in range(10):
                    if not re.search(r"\bLoading\b", page.locator("body").inner_text(), re.I):
                        break
                    page.wait_for_timeout(700)
            except Exception:
                page.wait_for_timeout(1200)
            if self._captcha_present(page):
                return SubmissionDecision("blocked", "CAPTCHA_REQUIRED: solve the visible challenge")
            confirmed, signal = self._confirmation(page, responses)
            if confirmed:
                evidence = self._capture_evidence(page, job["id"])
                return SubmissionDecision(
                    "submitted", "Application confirmed by {}. Evidence: {}".format(signal, evidence), page.url
                )

            helper._prefill(page, page, job, resume, profile)
            self.account_automation._consents(page, profile)

            body = page.locator("body").inner_text()
            final = [item for item in page.locator('button:visible').all() if re.fullmatch(
                r"submit(?: application)?", (item.inner_text() or "").strip(), re.I
            ) and item.is_enabled()]
            if final and re.search(r"\breview\b|current step [6-9]", body, re.I):
                started = int(time.time())
                final[-1].click(force=True)
                page.wait_for_timeout(3000)
                if self.gmail_client is not None:
                    try:
                        self.gmail_client.fill_code_challenge(
                            page, started, job["company"] or "", timeout_seconds=90
                        )
                    except Exception:
                        pass
                confirmed, signal = self._confirmation(page, responses)
                evidence = self._capture_evidence(page, job["id"])
                if confirmed:
                    return SubmissionDecision(
                        "submitted", "Application confirmed by {}. Evidence: {}".format(signal, evidence), page.url
                    )
                return SubmissionDecision(
                    "confirmation_unknown",
                    "Submit was clicked but no success signal was found. Evidence: {}".format(evidence),
                )

            advance = None
            for pattern in (r"^save and continue$", r"^next$", r"^continue$"):
                matches = [item for item in page.locator('button:visible').all() if re.fullmatch(
                    pattern, (item.inner_text() or "").strip(), re.I
                ) and item.is_enabled()]
                if matches:
                    advance = matches[-1]
                    break
            if advance is None:
                return SubmissionDecision("failed", "AUTOMATIC_RETRY: Workday step control was not recognized")
            advance.click(force=True)
            page.wait_for_timeout(1800)
            notices = [text.strip() for text in page.locator(
                '[role="alert"]:visible, [data-automation-id*="error" i]:visible'
            ).all_inner_texts() if text.strip()]
            errors = [text for text in notices if re.search(
                r"\berror\b|\brequired\b|must have a value|\binvalid\b|please (?:enter|select|provide)",
                text, re.I,
            ) and not re.search(r"successfully|uploaded successfully", text, re.I)]
            if errors:
                return SubmissionDecision(
                    "failed", "AUTOMATIC_RETRY: Workday requires additional profile data: "
                    + "; ".join(dict.fromkeys(errors))[:2000]
                )
        return SubmissionDecision("failed", "AUTOMATIC_RETRY: Workday wizard exceeded its step limit")

    def submit(self, job, resume: Path, profile: ApplicationProfile) -> SubmissionDecision:
        guard = application_guard(job)
        if guard:
            return SubmissionDecision('blocked', guard)
        session = None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return SubmissionDecision("failed", "Playwright is not installed; install the browser extra")

        try:
            with sync_playwright() as playwright:
                self.browser_profile.mkdir(parents=True, exist_ok=True)
                session = getattr(playwright, self.browser_type).launch_persistent_context(
                    str(self.browser_profile), headless=self.headless
                )
                page = session.pages[0] if session.pages else session.new_page()
                target_url = job["canonical_url"] or job["url"]
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                self._dismiss_consent(page)
                if prohibited_automation_url(page.url) or aggregator_url(page.url):
                    session.close()
                    return SubmissionDecision("manual_action_required", "Posting redirected to a restricted or aggregator site")
                surface = self._form_surface(page)
                if surface is None:
                    links = page.get_by_role("link", name=re.compile(r"apply", re.I)).all()
                    link = next((item for item in links if item.is_visible() and item.is_enabled()), None)
                    if link is not None and link.get_attribute("href"):
                        page.goto(urljoin(page.url, link.get_attribute("href")), wait_until="domcontentloaded", timeout=30000)
                    else:
                        buttons = page.get_by_role("button", name=re.compile(r"apply", re.I)).all()
                        control = next((item for item in buttons if item.is_visible() and item.is_enabled()), None)
                        if control is not None:
                            control.click()
                    page.wait_for_timeout(2500)
                    surface = self._form_surface(page)

                if self._captcha_present(page):
                    session.close()
                    return SubmissionDecision("blocked", "CAPTCHA_REQUIRED: solve the visible challenge")
                if surface is None:
                    if self.account_automation.looks_like_login(page):
                        login_return_url = page.url
                        resolution = self.account_automation.resolve(
                            page, target_url, profile.values.get("identity", {}),
                            job["company"] or "", login_return_url,
                            profile=profile,
                        )
                        if resolution.state == "captcha":
                            session.close()
                            return SubmissionDecision("blocked", resolution.detail)
                        if resolution.state == "mfa":
                            session.close()
                            return SubmissionDecision("manual_action_required", resolution.detail)
                        if resolution.state != "resolved":
                            session.close()
                            return SubmissionDecision("failed", resolution.detail)
                        self._dismiss_consent(page)
                        surface = self._form_surface(page)
                        if surface is None:
                            links = page.get_by_role("link", name=re.compile(r"apply", re.I)).all()
                            link = next((item for item in links if item.is_visible() and item.is_enabled()), None)
                            if link is not None and link.get_attribute("href"):
                                page.goto(urljoin(page.url, link.get_attribute("href")), wait_until="domcontentloaded", timeout=30000)
                                page.wait_for_timeout(2000)
                                surface = self._form_surface(page)
                            else:
                                buttons = page.get_by_role("button", name=re.compile(r"^apply$", re.I)).all()
                                button = next((item for item in buttons if item.is_visible() and item.is_enabled()), None)
                                if button is not None:
                                    href = button.get_attribute("href")
                                    if href:
                                        page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=30000)
                                    else:
                                        button.click(timeout=5000)
                                    page.wait_for_timeout(2500)
                                    surface = self._form_surface(page)
                        if surface is None and "myworkdayjobs.com" in page.url:
                            return self._submit_workday(page, job, resume, profile)
                    if surface is None:
                        session.close()
                        return SubmissionDecision("failed", "AUTOMATIC_RETRY: no accessible application form was found")

                if "ashbyhq.com" in page.url:
                    # Custom questions can render after the initial form shell.
                    page.wait_for_timeout(2000)
                    unanswered = self._ashby_unanswered_required_questions(surface, profile, job,
                        answer_for=lambda question, options: self._answer_for(profile, question, job, resume, options))
                    if unanswered:
                        session.close()
                        return SubmissionDecision(
                            "manual_action_required",
                            "PRECHECK_UNANSWERED: " + "; ".join(unanswered),
                        )

                helper = AssistedApplicationService.__new__(AssistedApplicationService)
                missing = []
                # Ashby parses uploaded resumes asynchronously and can replace
                # values during that process. Upload first, wait for parsing,
                # then fill fields so the final values reach React form state.
                file_controls = surface.locator('input[type="file"]')
                for file_index in range(file_controls.count()):
                    file_control = file_controls.nth(file_index)
                    file_identity = " ".join(filter(None, (
                        file_control.get_attribute("id"), file_control.get_attribute("name"),
                        file_control.get_attribute("aria-label"), helper._label(file_control),
                    )))
                    if "cover" not in normalize_question(file_identity):
                        file_control.set_input_files(str(resume))
                        # ATS pages commonly re-render all upload controls as
                        # soon as the resume is attached. One resume upload is
                        # sufficient; continuing with stale indexed locators
                        # can time out or accidentally populate a cover letter.
                        break
                if "ashbyhq.com" in page.url:
                    try:
                        page.get_by_text(re.compile(r"autofill completed", re.I)).wait_for(timeout=12000)
                    except Exception:
                        page.wait_for_timeout(5000)
                controls = surface.locator("input, textarea, select")
                processed_choice_names = set()
                answered_choice_values = set()
                for index in reversed(range(controls.count())):
                    control = controls.nth(index)
                    tag = control.evaluate("el => el.tagName.toLowerCase()")
                    kind = (control.get_attribute("type") or "").lower()
                    if kind in {"hidden", "submit", "button"}:
                        continue
                    # Workable renders radio and checkbox inputs as hidden
                    # state holders inside visible ARIA option wrappers. They
                    # still need to be reconciled and clicked through the
                    # visible wrapper.
                    if control.get_attribute("aria-hidden") == "true" and kind not in {"radio", "checkbox"}:
                        continue
                    if kind == "file":
                        continue
                    # Greenhouse's structured Employment section has generic
                    # labels ("Title", "Start date year") that must never be
                    # answered using identity or education defaults.
                    control_id = control.get_attribute("id") or ""
                    employment_field = re.fullmatch(
                        r"(company-name|title|start-date-month|start-date-year|"
                        r"end-date-month|end-date-year|current-role)-(\d+)(?:_\d+)?",
                        control_id,
                    )
                    if employment_field:
                        field_name, record_index = employment_field.group(1), int(employment_field.group(2))
                        records = profile.values.get("employment_history", {}).get("roles", [])
                        record = records[record_index] if record_index < len(records) else None
                        if record is None:
                            if field_name != "current-role":
                                missing.append("Employment history row {}".format(record_index + 1))
                            continue
                        if field_name == "current-role":
                            # Set this after all text/select fields; Greenhouse
                            # may reset the checkbox during subsequent renders.
                            continue
                        if record.get("current") and field_name.startswith("end-date-"):
                            continue
                        record_key = {
                            "company-name": "company", "title": "title",
                            "start-date-month": "start_month", "start-date-year": "start_year",
                            "end-date-month": "end_month", "end-date-year": "end_year",
                        }[field_name]
                        answer = str(record.get(record_key) or "").strip()
                        if not answer:
                            missing.append("Employment history {}".format(field_name))
                        elif control.get_attribute("role") == "combobox":
                            if not helper._select_combobox_option(page, surface, control, answer, field_name):
                                missing.append("Employment history {}".format(field_name))
                        else:
                            control.fill(answer)
                            control.press("Tab")
                        continue
                    if not control.is_enabled():
                        continue
                    parent_class = control.evaluate("el => el.parentElement?.className || ''")
                    if kind == "checkbox" and "ashby-application-form-input-yesno" in parent_class:
                        group_label = control.evaluate(
                            "el => el.parentElement?.parentElement?.querySelector('label')?.innerText || ''"
                        )
                        answer = self._answer_for(profile, group_label, job, resume, ["Yes", "No"])
                        if answer is None:
                            missing.append(group_label or "required yes/no question")
                        else:
                            option = "yes" if normalize_question(answer) == "yes" else "no"
                            button = control.locator("xpath=..").locator(
                                'button[data-option="{}"]'.format(option)
                            )
                            if button.count() == 1:
                                # Resume parsing can paint a choice without
                                # committing it to Ashby's submission state.
                                # Force a real state transition before choosing
                                # the final answer.
                                opposite = "no" if option == "yes" else "yes"
                                opposite_button = control.locator("xpath=..").locator(
                                    'button[data-option="{}"]'.format(opposite)
                                )
                                if opposite_button.count() == 1:
                                    opposite_button.click()
                                    page.wait_for_timeout(100)
                                button.click()
                            else:
                                missing.append(group_label or "required yes/no question")
                        continue
                    if not control.is_visible():
                        visible_choice = control.locator(
                            "xpath=ancestor::*[@role='radio' or @role='checkbox'][1]"
                        ) if kind in {"radio", "checkbox"} else None
                        if visible_choice is None or not visible_choice.count() or not visible_choice.first.is_visible():
                            continue
                    required = control.get_attribute("required") is not None or control.get_attribute("aria-required") == "true"
                    label = helper._label(control)
                    normalized = normalize_question(label)
                    if any(term in normalized for term in MANUAL_ONLY_QUESTION_TERMS):
                        privacy_default = profile.answer(
                            label, profile.jurisdiction(job), job["company"] or "", job["location"] or ""
                        )
                        if privacy_default is None and required:
                            missing.append(label or "required sensitive question")
                        if privacy_default is None:
                            continue
                    if kind == "checkbox":
                        choice_name = control.get_attribute("name") or ""
                        if choice_name and choice_name in processed_choice_names:
                            continue
                        if choice_name:
                            processed_choice_names.add(choice_name)
                        fieldset = control.locator("xpath=ancestor::fieldset[1]")
                        question = control.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' application-question ')][1]")
                        if fieldset.count():
                            group = fieldset.locator('input[type="checkbox"]')
                        elif question.count():
                            # Lever represents a required yes/no response as
                            # sibling checkboxes inside application-question,
                            # without a fieldset. Treat them as one exclusive
                            # choice instead of two unrelated requirements.
                            group = question.locator('input[type="checkbox"]')
                        else:
                            group = control
                        if group.count() > 1:
                            group_label = control.evaluate(
                                "el => el.closest('fieldset')?.querySelector('legend')?.innerText || "
                                "el.closest('[role=group]')?.getAttribute('aria-label') || "
                                "el.closest('.application-question')?.querySelector('.application-label .text')?.innerText || "
                                "el.closest('[data-field]')?.querySelector('label, [class*=label i], [class*=question i]')?.innerText || ''"
                            )
                            if any(item.is_checked() for item in group.all()):
                                continue
                            option_labels = [helper._label(item) for item in group.all()]
                            answer = self._answer_for(profile, group_label or label, job, resume, option_labels)
                            matched_text = helper._matching_option_text(
                                option_labels, answer or "", group_label or label
                            )
                            match = next((
                                item for item in group.all()
                                if normalize_question(helper._label(item)) == normalize_question(matched_text or "")
                            ), None)
                            if match is not None:
                                # Record every option from the completed group.
                                # Some Lever forms synchronously unmount the
                                # question after a click; a live nth locator can
                                # then briefly resolve to the former sibling
                                # (usually "No") as though it were a standalone
                                # required checkbox.
                                answered_choice_values.update(
                                    normalize_question(option_label)
                                    for option_label in option_labels
                                    if option_label
                                )
                                wrapper = match.locator("xpath=ancestor::*[@role='checkbox'][1]")
                                if wrapper.count() and wrapper.first.is_visible():
                                    wrapper.first.click()
                                else:
                                    visible_label = match.locator("xpath=ancestor::label[1]")
                                    if visible_label.count() and visible_label.first.is_visible():
                                        visible_label.first.click()
                                    else:
                                        match.check(force=True)
                            elif required and (group_label or label) not in missing:
                                missing.append(group_label or label or "required checkbox group")
                            continue
                        group_label = control.evaluate(
                            "el => el.closest('.application-question')?.querySelector('.application-label .text')?.innerText || "
                            "el.closest('fieldset')?.querySelector('legend')?.innerText || ''"
                        )
                        if group_label:
                            answer = self._answer_for(profile, group_label, job, resume, [label])
                            matched = helper._matching_option_text([label], answer or "", group_label)
                            if matched is not None:
                                answered_choice_values.add(normalize_question(matched))
                                visible_label = control.locator("xpath=ancestor::label[1]")
                                if visible_label.count() and visible_label.first.is_visible():
                                    visible_label.first.click()
                                else:
                                    control.check(force=True)
                                continue
                        if not group_label and normalized in answered_choice_values:
                            # Lever can unmount a completed yes/no checkbox
                            # group immediately after selection. The live nth
                            # locator may then momentarily resolve to the old
                            # sibling option; it is not a new question.
                            continue
                        if required and any(term in normalized for term in ("consent", "privacy", "terms", "acknowledge", "agree")):
                            control.check()
                        elif required:
                            missing.append(label or "required choice")
                        continue
                    if kind == "radio":
                        choice_name = control.get_attribute("name") or ""
                        if choice_name and choice_name in processed_choice_names:
                            continue
                        if choice_name:
                            processed_choice_names.add(choice_name)
                        group_label = control.evaluate(
                            "el => { const group = el.closest('fieldset, [role=radiogroup]'); "
                            "const labelled = group?.getAttribute('aria-labelledby'); "
                            "return group?.querySelector('legend')?.innerText || "
                            "(labelled ? document.getElementById(labelled)?.innerText : '') || "
                            "el.closest('.application-question')?.querySelector('.application-label .text')?.innerText || "
                            "el.closest('fieldset')?.querySelector('label.ashby-application-form-question-title')?.innerText || "
                            "el.closest('[role=radiogroup]')?.getAttribute('aria-label') || "
                            "el.closest('[role=radiogroup]')?.querySelector('[class*=label i], [class*=question i]')?.innerText || "
                            "el.parentElement?.parentElement?.querySelector('label')?.innerText || ''; }"
                        )
                        name = choice_name
                        group = surface.locator('input[type="radio"][name="{}"]'.format(name.replace('"', '\\"'))) if name else control
                        # Ashby's resume parser can preselect the wrong radio
                        # choice. Always reconcile Ashby radios with the saved
                        # factual profile instead of trusting the painted value.
                        if "ashbyhq.com" not in page.url and any(
                            item.is_checked() for item in group.all()
                        ):
                            continue
                        option_labels = [helper._label(item) for item in group.all()]
                        answer = self._answer_for(profile, group_label or label, job, resume, option_labels)
                        matched_text = helper._matching_option_text(
                            option_labels, answer or "", group_label or label
                        )
                        match = next((
                            item for item in group.all()
                            if normalize_question(helper._label(item)) == normalize_question(matched_text or "")
                        ), None)
                        if match is not None:
                            answered_choice_values.update(
                                normalize_question(option_label)
                                for option_label in option_labels
                                if option_label
                            )
                            if "ashbyhq.com" in page.url:
                                # As with Ashby yes/no controls, make React see
                                # an actual transition even when resume autofill
                                # has already painted the desired choice.
                                match_id = match.get_attribute("id")
                                alternatives = [
                                    item for item in group.all()
                                    if item.get_attribute("id") != match_id
                                ]
                                if match.is_checked() and alternatives:
                                    alternatives[0].click(force=True)
                                    page.wait_for_timeout(100)
                                match.click(force=True)
                            else:
                                wrapper = match.locator("xpath=ancestor::*[@role='radio'][1]")
                                if wrapper.count() and wrapper.first.is_visible():
                                    wrapper.first.click()
                                else:
                                    match.check()
                        elif required and (group_label or label) not in missing:
                            missing.append(group_label or label or "required choice")
                        continue
                    options = control.locator("option").all_text_contents() if tag == "select" else []
                    answer = self._answer_for(profile, label, job, resume, options)
                    if answer is None:
                        if required:
                            missing.append(label or "unnamed required field")
                        continue
                    if control.get_attribute("role") == "combobox":
                        if not helper._select_combobox_option(page, surface, control, answer, label) and required:
                            missing.append(label)
                    elif tag == "select":
                        match = helper._matching_option_text(options, answer, label)
                        if match is None:
                            if required:
                                missing.append(label)
                        else:
                            control.select_option(label=match)
                    else:
                        # Re-enter even displayed autofill values: Ashby can
                        # show resume-parsed text before registering it in the
                        # submission payload.
                        if "ashbyhq.com" in page.url:
                            control.click()
                            control.press("Meta+A")
                            control.press("Backspace")
                            control.press_sequentially(answer, delay=60)
                            page.wait_for_timeout(200)
                        else:
                            control.fill("")
                            control.fill(answer)
                        control.press("Tab")

                for record_index, record in enumerate(
                    profile.values.get("employment_history", {}).get("roles", [])
                ):
                    if record.get("current"):
                        current_checkbox = surface.locator(
                            'input[id^="current-role-{}"]'.format(record_index)
                        )
                        if current_checkbox.count() == 1 and not current_checkbox.is_checked():
                            current_checkbox.check()
                if missing:
                    session.close()
                    return SubmissionDecision(
                        "manual_action_required",
                        "Unknown required fields: " + "; ".join(dict.fromkeys(missing)),
                    )
                submit = surface.get_by_role("button", name=re.compile(r"^submit application$", re.I))
                if not submit.count():
                    submit = surface.locator('button[type="submit"]:visible, input[type="submit"]:visible')
                if submit.count() != 1:
                    session.close()
                    return SubmissionDecision("manual_action_required", "Final submit control is missing or ambiguous")
                # Workable can mount its cookie dialog after the application
                # form has loaded, so the initial dismissal above may run too
                # early. Recheck immediately before the final click.
                self._dismiss_consent(page)
                responses = []

                def capture_response(response):
                    try:
                        if response.request.method != "POST":
                            return
                        request_body = response.request.post_data or ""
                        is_ashby_submit = "ashbyhq.com" in page.url and re.search(
                            r"submit(?:Application|MultipleForms)|applicationForm", request_body, re.I
                        )
                        is_greenhouse_submit = (
                            "greenhouse" in page.url and "greenhouse" in response.url
                        )
                        if not is_ashby_submit and not is_greenhouse_submit and not re.search(
                            r"apply|application|candidate|submission", response.url, re.I
                        ):
                            return
                        body = response.text()[:5000]
                        responses.append({"url": response.url, "status": response.status, "body": body})
                    except Exception:
                        return

                page.on("response", capture_response)
                try:
                    page.evaluate("""() => {
                        window.__jobflowSubmissionSignal = '';
                        window.addEventListener('message', event => {
                            if (event.data === 'application_submitted' || event.data === 'application_errored') {
                                window.__jobflowSubmissionSignal = event.data;
                            }
                        });
                    }""")
                except Exception:
                    pass
                if "ashbyhq.com" in page.url:
                    # Ashby uploads and parses the resume asynchronously before
                    # its submit handler will proceed.
                    page.wait_for_timeout(3000)
                verification_started = int(time.time())
                submit.click()
                page.wait_for_timeout(2500)
                verification_filled = False
                verification_error = ""
                if self.gmail_client is not None:
                    try:
                        verification_filled = self.gmail_client.fill_code_challenge(
                            page, verification_started, job["company"] or "", timeout_seconds=90
                        )
                    except Exception as exc:
                        # Gmail failures must never make a completed form look submitted.
                        verification_error = "Gmail verification reader failed"
                        response = getattr(exc, "response", None)
                        if response is not None:
                            try:
                                if response.json().get("error") == "invalid_grant":
                                    verification_error = "Gmail authorization expired or was revoked; reconnect Gmail"
                            except (ValueError, TypeError):
                                pass
                confirmed_after_click, _ = self._confirmation(page, responses)
                if not verification_filled and not confirmed_after_click and not responses:
                    # Some Greenhouse forms render an enabled-looking React
                    # submit button whose click does not dispatch the owning
                    # form. Native requestSubmit still runs constraint
                    # validation and the page's submit handler.
                    try:
                        dispatched = submit.evaluate("""el => {
                            const form = el.closest('form');
                            if (!form || !form.checkValidity()) return false;
                            form.requestSubmit(el);
                            return true;
                        }""")
                        if dispatched:
                            page.wait_for_timeout(2500)
                            if self.gmail_client is not None:
                                try:
                                    verification_filled = self.gmail_client.fill_code_challenge(
                                        page, verification_started, job["company"] or "", timeout_seconds=90
                                    )
                                except Exception:
                                    verification_error = verification_error or "Gmail verification reader failed"
                    except Exception:
                        pass
                # Greenhouse can validate an emailed code in place and then
                # expose the same submit button for the actual application.
                # Filling the code is not itself a submission.
                if verification_filled:
                    confirmed_after_code, _ = self._confirmation(page, responses)
                    if not confirmed_after_code:
                        # The Greenhouse security-code prompt can live inside
                        # the embedded application frame. Search the form
                        # surface, not only the employer's outer page.
                        final_buttons = [item for item in surface.get_by_role(
                            "button", name=re.compile(r"^submit application$", re.I)
                        ).all() if item.is_visible() and item.is_enabled()]
                        if len(final_buttons) == 1:
                            final_buttons[0].click()
                            page.wait_for_timeout(3000)
                # Ashby's invisible reCAPTCHA and device fingerprint are both
                # awaited by its client before the GraphQL submission. Do not
                # close the page while that work is still in flight.
                if "ashbyhq.com" in page.url:
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        confirmed_now, _ = self._confirmation(surface, responses)
                        try:
                            message_signal = page.evaluate("window.__jobflowSubmissionSignal || ''")
                        except Exception:
                            message_signal = ""
                        if confirmed_now or message_signal in {"application_submitted", "application_errored"}:
                            break
                        page.wait_for_timeout(750)
                if self._captcha_present(page):
                    evidence = self._capture_evidence(page, job["id"])
                    session.close()
                    return SubmissionDecision(
                        "blocked",
                        "CAPTCHA_REQUIRED: solve the visible challenge. Evidence: {}".format(evidence),
                    )
                validation = surface.locator(
                    '[role="alert"]:visible, .error:visible, .errors:visible, .field-error:visible, '
                    '[aria-invalid="true"]:visible'
                ).all_inner_texts()
                validation = [item.strip() for item in validation if item.strip()]
                native_invalid = surface.locator(
                    "input:not([type=file]):invalid, textarea:invalid, select:invalid"
                )
                invalid_files = surface.locator('input[type="file"]:invalid')
                for file_index in range(invalid_files.count()):
                    file_control = invalid_files.nth(file_index)
                    if not file_control.evaluate("el => Boolean(el.files && el.files.length)"):
                        invalid_label = helper._label(file_control) or "Resume"
                        validation.append(
                            "Browser rejected required field: {}".format(invalid_label)
                        )
                for invalid_index in range(native_invalid.count()):
                    invalid = native_invalid.nth(invalid_index)
                    invalid_label = helper._label(invalid) or invalid.get_attribute("name") or "unnamed field"
                    message = "Browser rejected required field: {}".format(invalid_label)
                    if message not in validation:
                        validation.append(message)
                confirmed, signal = self._confirmation(surface, responses)
                try:
                    final_body = "\n".join(
                        frame.locator("body").inner_text()
                        for frame in page.frames
                        if frame.locator("body").count()
                    )
                except Exception:
                    final_body = ""
                email_code_pending = bool(re.search(
                    r"verification code(?: was)? sent|enter the (?:\d[- ]digit )?code|check your email",
                    final_body, re.I,
                ))
                receipt = surface.url
                evidence = self._capture_evidence(page, job["id"])
                session.close()
                if email_code_pending and not confirmed:
                    return SubmissionDecision(
                        "manual_action_required",
                        "EMAIL_VERIFICATION_REQUIRED: {}. The application was not submitted. Evidence: {}".format(
                            verification_error or "email code challenge remains open", evidence
                        ),
                    )
                if confirmed:
                    return SubmissionDecision(
                        "submitted", "Application confirmed by {}. Evidence: {}".format(signal, evidence), receipt
                    )
                if any("captcha-failed" in (response.get("body") or "").lower() for response in responses):
                    return SubmissionDecision(
                        "blocked",
                        "CAPTCHA_REQUIRED: ATS rejected the verification response; "
                        "the application was not submitted. Evidence: {}".format(evidence),
                    )
                if validation and not confirmed:
                    return SubmissionDecision(
                        "manual_action_required",
                        "ATS rejected or requires more input: {}. Evidence: {}".format(
                            "; ".join(dict.fromkeys(validation))[:500], evidence
                        ),
                    )
                return SubmissionDecision(
                    "confirmation_unknown",
                    "Submit was clicked but no receipt or success signal was found. "
                    "ATS responses: {}. Evidence: {}".format(
                        "; ".join(
                            "{} {} {}".format(
                                item.get("status", ""), item.get("url", ""),
                                re.sub(r"\s+", " ", item.get("body", ""))[:240],
                            )
                            for item in responses[-4:]
                        ) or "none captured",
                        evidence,
                    ),
                )
        except Exception as exc:
            return SubmissionDecision("failed", "Browser application failed: {}".format(exc))
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass


class AssistedApplicationService:
    """Open one qualified application in a visible persistent browser and monitor its receipt."""

    @staticmethod
    def _existing_application_page(pages, url):
        target = urlparse(url)
        def path(value):
            parsed = urlparse(value)
            result = parsed.path.rstrip('/')
            if (parsed.hostname or '').endswith('.myworkdayjobs.com'):
                result = re.sub(r'^/[a-z]{2}-[A-Z]{2}(?=/)', '', result)
            return result
        target_path = path(url)
        # Resume the existing draft before choosing the posting landing page.
        drafts = []
        for candidate in reversed(pages):
            parsed = urlparse(candidate.url)
            # Amazon's board and authenticated application use separate hosts.
            # Reuse only the exact job ID, never another open Amazon draft.
            if target.hostname in {'www.amazon.jobs', 'amazon.jobs'} and parsed.hostname == 'account.amazon.jobs':
                target_id = re.search(r'/jobs/(\d+)(?:/|$)', target.path)
                draft_id = re.search(r'/applicant/jobs/(\d+)/apply(?:/|$)', parsed.path)
                if target_id and draft_id and target_id[1] == draft_id[1]:
                    drafts.append(candidate)
                    continue
            if parsed.hostname != target.hostname:
                continue
            candidate_path = path(candidate.url)
            # Taleo Business Edition stores tenant and posting identity in
            # query parameters, not the pathname. Reuse only the exact draft.
            if target_path.endswith('/viewRequisition') and candidate_path.endswith('/applyRequisition'):
                source_ids, draft_ids = parse_qs(target.query), parse_qs(parsed.query)
                if all(len(source_ids.get(key, [])) == 1 and source_ids[key] == draft_ids.get(key)
                       for key in ('org', 'cws', 'rid')):
                    drafts.append(candidate)
                    continue
            if candidate_path in {target_path + '/application', target_path + '/apply'} or candidate_path.startswith(target_path + '/apply/'):
                drafts.append(candidate)
                continue
            # Phenom-style career sites move /job/<stable-id>/<title> to
            # /apply?jobSeqNo=<stable-id>. Match the exact id, never just host.
            sequence = parse_qs(parsed.query).get('jobSeqNo', [])
            if (candidate_path.endswith('/apply') and len(sequence) == 1
                    and '/job/' in target_path and sequence[0] == target_path.split('/job/', 1)[1].split('/')[0]):
                drafts.append(candidate)
        if drafts:
            def progress(candidate):
                # Workday keeps the same URL across every wizard step. A newer
                # duplicate may be logged out while an older tab has a usable
                # questionnaire. Inspect visible UI, never navigate or mutate.
                try:
                    return candidate.evaluate(r"""() => {
                        const visible = el => !!el.getClientRects().length;
                        if ([...document.querySelectorAll('input[type=password]')].some(visible)) return -1;
                        const headings = [...document.querySelectorAll('h1,h2,h3,[aria-current=step]')]
                            .filter(visible).map(el => el.innerText).join(' ');
                        if (/review (?:your )?application/i.test(headings)) return 5;
                        if ([...document.querySelectorAll('[id^="primaryQuestionnaire--"], [id^="secondaryQuestionnaire--"]')].some(visible)) return 4;
                        if (/voluntary disclosures|self.identif|application questions/i.test(headings)) return 4;
                        if (/my experience|work experience/i.test(headings)) return 3;
                        if (/my information|personal information/i.test(headings)) return 2;
                        return [...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(visible).length >= 3 ? 1 : 0;
                    }""")
                except Exception:
                    return -2
            # Stable max preserves newest-first order when progress is equal.
            return max(drafts, key=progress)
        return next((candidate for candidate in pages if candidate.url == url), None)

    def __init__(
        self,
        repository: Repository,
        browser_profile: Path = Path("private-data/browser-profile"),
        evidence_root: Path = Path("artifacts/submission-receipts"),
    ):
        self.repository = repository
        self.browser_profile = Path(browser_profile).resolve()
        gmail = GmailVerificationClient.from_environment()
        self.gmail_client = gmail if gmail.configured() and gmail.connected() else None
        self.adapter = PlaywrightFormAdapter(
            headless=False, evidence_root=evidence_root, gmail_client=self.gmail_client
        )

    @staticmethod
    def _label(control) -> str:
        shared = control.evaluate('el => {' + QUESTION_HELPERS + 'return labelOf(el);}')
        if shared:
            return shared
        return control.evaluate(r"""el => {
            const explicit = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            const wrapped = el.closest('label');
            const legend = el.closest('fieldset')?.querySelector(':scope > legend');
            const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
                .map(id => document.getElementById(id)?.innerText || '').join(' ');
            const container = el.closest('[data-field], [class*="field" i], [class*="question" i], [class*="input" i]');
            const nearby = container?.querySelector('label, legend, [class*="label" i], [class*="question" i]');
            const applicationQuestion = el.closest('.application-question')?.querySelector(
                '.application-label .text, .application-label'
            );
            const previous = el.previousElementSibling;
            const ancestorText = [el.parentElement, el.parentElement?.parentElement, el.parentElement?.parentElement?.parentElement]
                .map(node => (node?.innerText || '').trim())
                .find(text => text && text.length < 1200);
            return (explicit?.innerText || el.getAttribute('aria-label') || labelled || wrapped?.innerText ||
                applicationQuestion?.innerText || nearby?.innerText || legend?.innerText || previous?.innerText || ancestorText || el.getAttribute('placeholder') ||
                el.name || el.id || '').trim();
        }""")

    @staticmethod
    def _matching_option(options, answer: str, label: str):
        """Match truthful profile answers to equivalent ATS option wording."""
        normalized_answer = normalize_question(answer)
        normalized_label = normalize_question(label)
        visible = []
        for option in options:
            try:
                if option.is_visible():
                    visible.append((option, normalize_question(option.inner_text())))
            except Exception:
                continue

        exact = next((option for option, text in visible if text == normalized_answer), None)
        if exact is not None:
            return exact
        # Translate only standalone binary options, never a longer qualified
        # statement such as "Oui, avec parrainage" or a double negative.
        binary_alias = {'yes': 'oui', 'no': 'non', 'oui': 'yes', 'non': 'no'}.get(normalized_answer)
        if binary_alias:
            matches = [option for option, text in visible if text == binary_alias]
            if len(matches) == 1:
                return matches[0]
        if 'language' in normalized_label or 'langue' in normalized_label:
            language_aliases = {'english': {'anglais'}, 'french': {'fran ais', 'francais'},
                                'anglais': {'english'}, 'fran ais': {'french'}, 'francais': {'french'}}
            matches = [option for option, text in visible if text in language_aliases.get(normalized_answer, set())]
            if len(matches) == 1:
                return matches[0]
        if normalized_label == 'phone country code' and normalized_answer in {'canada 1', '1 canada'}:
            # +1 is shared by multiple countries; require Canada's identity too.
            return next((option for option, text in visible
                         if re.fullmatch(r'1 ca(?: canada)?|canada 1', text)), None)
        if normalized_answer == 'no' and re.search(
                r'previously (?:been )?employed|previously worked|ever been employed', normalized_label):
            prior_employment_no = next((option for option, text in visible
                         if re.fullmatch(r'i have (?:not|never) (?:previously )?(?:been employed (?:at|by)|worked (?:at|for)) .+', text)), None)
            if prior_employment_no is not None:
                return prior_employment_no
            # Otherwise continue to the ordinary "No, ..." choice matching.
        if 'timezone' in normalized_label or 'time zone' in normalized_label:
            if normalized_answer in {'eastern time', 'america toronto'}:
                matches = [option for option, text in visible if text in {'est', 'et', 'eastern', 'eastern time'}]
                return matches[0] if len(matches) == 1 else None
        if 'years' in normalized_label and 'experience' in normalized_label and re.fullmatch(r'\d+', normalized_answer):
            matches = []
            years = int(normalized_answer)
            for option, text in visible:
                raw = option.inner_text().strip().lower()
                raw = re.sub(r'\s+years?\s*$', '', raw).strip()
                lower = re.fullmatch(r'(\d+)\s*(?:\+|or more|and above|plus)', raw)
                band = re.fullmatch(r'(\d+)\s*(?:-|–|—|to)\s*(\d+)', raw)
                below = re.fullmatch(r'(?:less than|under)\s+(\d+)', raw)
                above = re.fullmatch(r'(?:more than|over)\s+(\d+)', raw)
                if ((lower and years >= int(lower[1])) or
                    (band and int(band[1]) <= years <= int(band[2])) or
                    (below and years < int(below[1])) or
                    (above and years > int(above[1])) or
                    (raw.isdigit() and years == int(raw)) or
                    (raw == 'no experience' and years == 0)):
                    matches.append(option)
            if matches:
                return matches[0] if len(matches) == 1 else None
        duration_days = None
        if 'notice period' in normalized_label and re.fullmatch(r'\d+', normalized_answer):
            duration_days = int(normalized_answer)
        elif re.search(r'earliest (?:(?:possible|available) )?start(?:ing)? date', normalized_label) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', answer):
            duration_days = (datetime.fromisoformat(answer).date() - datetime.now(ZoneInfo('America/Toronto')).date()).days
        if duration_days is not None:
            days = duration_days
            matches = []
            for option, text in visible:
                band = re.fullmatch(r'(\d+)\s+(\d+)\s+(days|weeks)(?: notice)?', text)
                unit = 7 if band and band[3] == 'weeks' else 1
                exact_duration = re.fullmatch(r'(\d+)\s+(days|weeks)(?: notice)?', text)
                if band and int(band[1])*unit <= days <= int(band[2])*unit:
                    matches.append((int(band[2])*unit, -int(band[1])*unit, option))
                elif exact_duration and int(exact_duration[1])*(7 if exact_duration[2]=='weeks' else 1) == days:
                    return option
            # Adjacent inclusive ranges overlap at endpoints. The earliest
            # upper bound accurately contains the saved notice duration (14
            # days -> 1–2 weeks, not an ambiguous rejection at 2–4 weeks).
            return min(matches, key=lambda item: item[:2])[2] if matches else None
        if normalized_answer == 'requires sponsorship' and 'citizenship employment eligibility' in normalized_label:
            return next((option for option, text in visible
                         if text == 'non citizen seeking work authorization'), None)
        if 'degree' in normalized_label or 'education' in normalized_label:
            # Degree level selectors omit the discipline and vary punctuation:
            # Bachelor of Economics / Bachelor's degree / Bachelors.
            if re.match(r'^bachelor(?:s| s)?(?:\b|$)', normalized_answer):
                degree = next((option for option, text in visible
                    if re.fullmatch(r'bachelor(?:s| s)?(?: degree)?', text)), None)
                if degree is not None:
                    return degree
        if normalized_answer == 'linkedin' and any(term in normalized_label for term in ('how did you hear', 'how did you find', 'referral source', 'application source')):
            named_source = next((option for option, text in visible if re.search(r'\blinkedin\b', text)), None)
            if named_source is not None:
                return named_source
            # A source-category dropdown can reveal a second, specific-source
            # field. LinkedIn belongs to these categories; never choose a
            # different named site or an arbitrary category/leaf.
            for category in ('professional networking', 'social media', 'job board', 'job boards', 'browsing the internet'):
                match = next((option for option, text in visible if text == category), None)
                if match is not None:
                    return match
        # ISO country codes in address dropdowns are equivalent to country
        # names, not a different citizenship or work-authorization claim.
        # Keep this separate from substring matching ("not Canada" is not CA).
        if 'country' in normalized_label and not any(
            word in normalized_label for word in ('phone', 'telephone', 'dial')):
            country_groups = ({'canada', 'ca', 'can'},
                              {'united states', 'united states of america', 'us', 'usa', 'u s'})
            group = next((values for values in country_groups if normalized_answer in values), None)
            if group is not None:
                return next((option for option, text in visible if text in group), None)
        contained = next((
            option for option, text in visible
            if len(normalized_answer) > 3 and len(text) > 3 and (
                re.search(r'\b' + re.escape(normalized_answer) + r'\b', text) or
                re.search(r'\b' + re.escape(text) + r'\b', normalized_answer))
        ), None)
        if contained is not None:
            return contained

        if re.fullmatch(r"\d+(?:\.\d+)?", normalized_answer) and any(
            term in normalized_label for term in ("salary", "compensation", "pay", "rate")
        ):
            target = float(normalized_answer)
            for option, text in visible:
                comma_style = [float(value) * 1000 for value in re.findall(r"\b(\d{1,3})\s+000\b", text)]
                numbers = comma_style or [float(value) for value in re.findall(r"\d+(?:\.\d+)?", text)]
                # Treat abbreviated salary bands such as 150k as 150000.
                numbers = [value * 1000 if value < 1000 and "k" in text else value for value in numbers]
                if len(numbers) == 1 and numbers[0] == target:
                    return option
                if len(numbers) >= 2 and numbers[0] <= target <= numbers[1]:
                    return option
                if numbers and any(term in text for term in ("or more", "and above", "plus")) and target >= numbers[0]:
                    return option

        if normalized_answer in {"decline to self identify", "prefer not to answer"}:
            return next((
                option for option, text in visible
                if any(term in text for term in (
                    "dont wish to answer", "don t wish to answer", "prefer not to answer",
                    "prefer not to say", "decline to self identify", "prefer not to disclose",
                    "choose not to disclose", "do not wish to disclose", "decline",
                    "do not wish to provide this information",
                ))
            ), None)
        if normalized_answer == "no" and "veteran" in normalized_label:
            non_veteran = next((option for option, text in visible if 'not a veteran' in text), None)
            if non_veteran is not None:
                return non_veteran
            return next((
                option for option, text in visible
                if any(term in text for term in ("not a protected veteran", "not a veteran", "no"))
            ), None)
        if normalized_answer == "no" and "disability" in normalized_label:
            return next((option for option, text in visible if text == "no" or text.startswith("no ")), None)
        if normalized_answer == "no" and any(term in normalized_label for term in (
            "right to work", "work authorization", "authorized to work",
        )):
            return next((
                option for option, text in visible
                if text == "no" or text.startswith("no ") or "do not have the right to work" in text
                or "not currently authorized" in text
            ), None)
        if normalized_answer in {"male", "man"} and "gender" in normalized_label:
            return next((option for option, text in visible if text in {"male", "man"}), None)
        if normalized_answer in {"i agree", "acknowledge", "confirm"}:
            preferred = ("i agree", "i acknowledge", "acknowledge", "confirm", "i consent", "accept", "yes")
            return next((
                option for value in preferred for option, text in visible
                if text == value or text.startswith(value + " ")
            ), None)
        if normalized_answer == "yes":
            if application_privacy_answer(label, {'application_privacy_consent': True,
                    'all_application_privacy_consent': True}) == 'Yes':
                simple = [option for option, text in visible
                          if text in {'acknowledge confirm', 'acknowledge', 'confirm', 'i agree', 'i acknowledge'}]
                if len(simple) == 1:
                    return simple[0]
            if normalized_label in {'important', 'required', 'consent', 'acknowledgement', 'acknowledgment'} and len(visible) == 1:
                option, text = visible[0]
                if application_privacy_answer(text, {'application_privacy_consent': True,
                        'all_application_privacy_consent': True}) == 'Yes':
                    return option
            # Match an approved privacy acknowledgement, not its negation or
            # a bundled permission such as marketing/background checks.
            if application_privacy_answer(label, {'application_privacy_consent': True}) == 'Yes':
                acknowledgements = [option for option, text in visible
                    if re.search(r'\b(?:read|reviewed|acknowledge|accept|agree|consent)\b', text)
                    and not re.search(r'\b(?:not|no|decline|refuse)\b', text)
                    and application_privacy_answer(text, {'application_privacy_consent': True}) == 'Yes']
                if len(acknowledgements) == 1:
                    return acknowledgements[0]
            # A binary answer cannot distinguish current from former employment.
            if "government official" in normalized_label and any(
                "current government official" in text or "former government official" in text
                for _, text in visible
            ):
                return None
            return next((option for option, text in visible if text == "yes" or text.startswith("yes ")), None)
        if normalized_answer == "no":
            return next((option for option, text in visible if text == "no" or text.startswith("no ")
                         or text.startswith(('i do not agree', 'i do not consent', 'i decline'))), None)
        if normalized_answer == "online search":
            if "where have you learned" in normalized_label:
                terms = ("samsara blog or website", "company website", "website")
            else:
                terms = (
                    "company careers site", "careers site", "career site", "careersite",
                    "company career page", "company website", "company site", "career page", "website",
                    "job boards", "job board other", "direct employers", "indeed glassdoor",
                    "other",
                )
            return next((option for term in terms for option, text in visible if term in text), None)
        if normalized_answer == "requires sponsorship":
            sponsorship_option = next((
                option for option, text in visible
                if "sponsor" in text
                or "visa" in text and any(term in text for term in ("require", "need"))
                or "need to apply for work authorization" in text
                or "do not have the right to work" in text
                or "non citizen seeking work authorization" in text
            ), None)
            if sponsorship_option is not None:
                return sponsorship_option
            return next((option for option, text in visible if text == "other"), None)

        if "canada" in normalized_answer and any(term in normalized_label for term in ("location", "work from", "state")):
            if 'quebec' in normalized_answer:
                province = next((option for option, text in visible if 'canada' in text and 'quebec' in text), None)
                if province is not None:
                    return province
            outside_canada = next((
                option for option, text in visible if "outside us" in text and "canada" in text
            ), None)
            if outside_canada is not None:
                return outside_canada
            other = next((option for option, text in visible if text == "other"), None)
            if other is not None:
                return other

        if normalized_answer == 'quebec' and any(term in normalized_label for term in ('state', 'residence', 'location')):
            outside_us = next((option for option, text in visible if text in {
                'i do not live in the united states', 'outside of the us', 'outside the us', 'outside us'}), None)
            if outside_us is not None:
                return outside_us

        answer_city = normalized_answer.split()[0] if normalized_answer else ""
        if any(term in normalized_label for term in ("location", "city")) and answer_city:
            return next((option for option, text in visible if text.startswith(answer_city + " ")), None)
        return None

    @staticmethod
    def _matching_option_text(option_texts: List[str], answer: str, label: str) -> Optional[str]:
        """Use semantic ATS option matching for native selects and radios too."""
        class TextOption:
            def __init__(self, value: str):
                self.value = value

            def is_visible(self):
                return True

            def inner_text(self):
                return self.value

        wrapped = [TextOption(value) for value in option_texts]
        match = AssistedApplicationService._matching_option(wrapped, answer, label)
        return match.value if match is not None else None

    @staticmethod
    def _combobox_options(surface, control):
        active_list = control.get_attribute('aria-controls')
        if active_list:
            scope = surface.locator('[id=' + json.dumps(active_list) + ']')
            return scope.locator('[role=option],.react-select__option').filter(visible=True)
        react = control.locator('xpath=ancestor::*[contains(concat(" ",normalize-space(@class)," ")," react-select ")][1]')
        if react.count():
            return react.locator('[role=option],.react-select__option').filter(visible=True)
        return surface.get_by_role('option').filter(visible=True)

    def _select_combobox_option(self, page, surface, control, answer: str, label: str) -> bool:
        # Generic Location may accept a country even when its city search has
        # no match. Never downgrade an explicit City/Province question.
        country_fallback = (answer.rsplit(',', 1)[-1].strip()
                            if normalize_question(label) == 'location' and ',' in answer else '')
        for attempt in range(3 if country_fallback else 2):
            try:
                match_answer = country_fallback if attempt == 2 else answer
                control.click(timeout=3000)
                page.wait_for_timeout(650 + (attempt * 350))
                option = self._matching_option(self._combobox_options(surface, control).all(), match_answer, label)
                if option is None:
                    search_value = answer.split(",", 1)[0] if any(
                        term in normalize_question(label) for term in ("location", "city")
                    ) else answer
                    if attempt == 2:
                        search_value = country_fallback
                    if normalize_question(label) == 'phone country code':
                        search_value = re.sub(r'\(?\+?\d+\)?', '', answer).strip()
                    control.fill("", timeout=3000)
                    control.press_sequentially(search_value, delay=75, timeout=5000)
                    page.wait_for_timeout(1500)
                    option = self._matching_option(self._combobox_options(surface, control).all(), match_answer, label)
                if option is not None:
                    chosen_text = option.inner_text().strip()
                    option.click(timeout=3000)
                    if control.evaluate("el => el.matches('.ashby-application-form-input-autocomplete,.rcmpaginatedselectinput')"):
                        # This records our observed UI selection for local
                        # validation only; it does not mutate employer form state.
                        control.evaluate('''(el, chosen) => {
                            if (el.value.trim() !== chosen || el.getAttribute('aria-expanded') !== 'false') return;
                            window.__jobflowObservedSelections ||= new WeakMap();
                            window.__jobflowObservedSelections.set(el, chosen);
                            el.addEventListener('input', () => window.__jobflowObservedSelections.delete(el), {once:true});
                        }''', chosen_text)
                    return True
                control.press("Escape", timeout=3000)
            except Exception:
                try:
                    control.press("Escape", timeout=1000)
                except Exception:
                    pass
                page.wait_for_timeout(350)
        return False

    @staticmethod
    def _select_structured_location(control, answer):
        """Select a visible city suggestion and verify the paired saved value."""
        city = answer.split(',', 1)[0].strip()
        country = answer.rsplit(',', 1)[-1].strip() if ',' in answer else ''
        if not city or not country:
            return False
        control.fill(city)
        choices = control.locator('..').locator('ul li button[type="button"]')
        try:
            choices.first.wait_for(state='visible', timeout=5000)
        except Exception:
            return False
        matches = [option for option in choices.all() if option.is_visible()
                   and normalize_question(option.inner_text().split(',')[0]) == normalize_question(city)
                   and normalize_question(option.inner_text().split(',')[-1]) == normalize_question(country)]
        if len(matches) != 1:
            return False
        expected = matches[0].inner_text().strip()
        matches[0].click(timeout=3000)
        return control.input_value().strip() == expected and control.evaluate(
            'el => {' + QUESTION_HELPERS + 'return selected(el);}')

    def _select_workday_linkedin(self, page, surface, control):
        """Choose the exact saved source across differing visible menu trees."""
        paths, visited, other_paths = [()], set(), []
        self.last_source_search = []
        known_branches = {'job board', 'job boards', 'professional networking', 'social media',
                          'social networks', 'external career site sources'}
        def visible_options():
            menu = surface.locator('[data-automation-id="activeListContainer"]:visible')
            scope = menu if menu.count() == 1 else surface
            return [item for item in scope.get_by_role('option').all() if item.is_visible()]
        while paths and len(visited) < 24:
            path = paths.pop(0)
            if path in visited:
                continue
            visited.add(path)
            control.fill('')
            control.press('Escape')
            page.wait_for_timeout(200)
            control.click(timeout=3000)
            page.wait_for_timeout(800)
            reached = True
            for parent in path:
                branch = next((item for item in visible_options()
                               if normalize_question(item.inner_text()) == parent), None)
                if branch is None:
                    reached = False
                    break
                branch.click(timeout=3000)
                page.wait_for_timeout(500)
            if not reached:
                continue
            for _ in range(6):
                self.last_source_search.append({'path': path, 'options': [item.inner_text() for item in visible_options()]})
                for option in visible_options():
                    label = normalize_question(option.inner_text())
                    if label == 'linkedin':
                        option.click(timeout=3000)
                        control.press('Escape')
                        selected = control.locator('xpath=ancestor::*[@data-automation-id="multiSelectContainer"][1]').locator('[data-automation-id="selectedItem"]')
                        return any(normalize_question(item.inner_text()) == 'linkedin' for item in selected.all())
                    if label == 'other' and (not path or path[-1] in {
                        'job sites', 'job board', 'job boards', 'social media',
                        'social networks', 'professional networking', 'external career site sources',
                    }) and path not in other_paths:
                        other_paths.append(path)
                    # Discover visible parent nodes, not employer-specific names.
                    expandable = option.locator('[data-uxi-multiselectlistitem-hassidecharm="true"], svg[class*="chevron-right"]').count()
                    if len(path) < 3 and (expandable or label in known_branches):
                        child = path + (label,)
                        if child not in visited and child not in paths:
                            paths.append(child)
                menu = surface.locator('[data-automation-id="activeListContainer"]:visible')
                if menu.count() != 1:
                    break
                options = visible_options()
                sizes = [int(item.get_attribute('aria-setsize') or 0) for item in options]
                positions = [int(item.get_attribute('aria-posinset') or 0) for item in options]
                if sizes and max(sizes) > 0 and max(positions) >= max(sizes):
                    break
                menu.hover()
                page.mouse.wheel(0, 320)
                page.wait_for_timeout(200)
        # "Other job site" is truthful when LinkedIn is absent. Do not select
        # another named site, a referral, or "Other" in an unrelated branch.
        for path in other_paths:
            control.fill('')
            control.press('Escape')
            page.wait_for_timeout(200)
            control.click(timeout=3000)
            page.wait_for_timeout(800)
            reached = True
            for label in path + ('other',):
                self.last_source_search.append({'fallback': label, 'options': [item.inner_text() for item in visible_options()]})
                option = next((item for item in visible_options()
                               if normalize_question(item.inner_text()) == label), None)
                if option is None:
                    reached = False
                    break
                option.click(timeout=3000)
                page.wait_for_timeout(800)
            if reached:
                control.press('Escape')
                selected = control.locator('xpath=ancestor::*[@data-automation-id="multiSelectContainer"][1]').locator('[data-automation-id="selectedItem"]')
                return any(normalize_question(item.inner_text()) == 'other' for item in selected.all())
        control.press('Escape')
        return False

    @staticmethod
    def _save_inline_resume(surface, resume: Path) -> bool:
        """Commit Taleo Business Edition's staged resume widget through its UI.

        Its hidden template is not an upload: Add Resume mounts a local picker,
        and Save transfers that picker into the application attachment.
        """
        widgets = surface.locator('.oracletaleocwsv2-dynamic-content-resume')
        for widget in widgets.all():
            if not widget.is_visible():
                continue
            if widget.locator('.oracletaleocwsv2-head-title').filter(has_text=resume.name).count():
                return True
            editor = widget.locator('.well:visible')
            if not editor.count():
                add = widget.get_by_role('link', name='Add Resume', exact=True)
                if not add.count() or not add.is_enabled():
                    continue
                add.click(timeout=5000)
                editor = widget.locator('.well:visible')
            picker = editor.locator('input[type=file]')
            if editor.count() != 1 or picker.count() != 1:
                continue
            picker.set_input_files(str(resume))
            save = editor.get_by_role('link', name='Save', exact=True)
            save.click(timeout=10000)
            editor.wait_for(state='hidden', timeout=10000)
            return True
        return False

    def _prefill(self, page, surface, job, resume: Path, profile: ApplicationProfile) -> int:
        # Resume parsers can reorder/populate rows after preflight. Re-read
        # visible anchors on every fill pass before assigning any dates.
        binding_error = bind_existing_records(profile.values, surface.evaluate(REQUIRED_QUESTIONS))
        if binding_error:
            raise ValueError(binding_error)
        filled = int(self._save_inline_resume(surface, resume))
        filled_checkbox_groups = set()
        for label in surface.locator('label.ashby-application-form-question-title').all():
            group = label.locator('..')
            buttons = group.locator('button[data-option]')
            if not buttons.count():
                continue
            answer = profile.answer(label.inner_text(), profile.jurisdiction(job), job['company'] or '', job['location'] or '')
            if answer in {'Yes', 'No'}:
                target = buttons.filter(has_text=re.compile('^' + answer + '$'))
                if target.get_attribute('aria-pressed') != 'true':
                    target.click()
                filled += 1
        source_fields = surface.locator(
            '[data-automation-id^="formField-source"] input[data-uxi-multiselect-id]:visible'
        )
        for index in range(source_fields.count()):
            control = source_fields.nth(index)
            container_text = control.evaluate(
                "el => el.closest('[data-automation-id^=formField-source]')?.innerText || ''"
            )
            answer = profile.answer(
                "How Did You Hear About Us?", profile.jurisdiction(job),
                job["company"] or "", job["location"] or "",
            )
            selected_sources = control.locator('xpath=ancestor::*[@data-automation-id="multiSelectContainer"][1]').locator('[data-automation-id="selectedItem"]')
            if normalize_question(answer or '') == 'linkedin' and any(
                normalize_question(item.inner_text()) in {'linkedin', 'other'} for item in selected_sources.all()
            ):
                continue
            if re.search(r"\b[1-9]\d* items? selected\b", container_text, re.I):
                if normalize_question(answer or '') != 'linkedin' or 'linkedin' in normalize_question(container_text):
                    continue
                container = control.locator('xpath=ancestor::*[starts-with(@data-automation-id,"formField-source")][1]')
                for selected in container.locator('[data-automation-id="selectedItem"][aria-label*="press delete" i]').all():
                    selected.press('Delete')
            if answer:
                if normalize_question(answer) == 'linkedin':
                    filled += int(self._select_workday_linkedin(page, surface, control))
                    continue
                control.click(timeout=3000)
                page.wait_for_timeout(700)
                option = self._matching_option(surface.get_by_role("option").all(), answer, "How Did You Hear About Us?")
                source_parent = False
                if option is None and normalize_question(answer) == 'linkedin':
                    for parent_name in ('job board', 'job boards', 'social media', 'social networks'):
                        parents = [item for item in surface.get_by_role('option').all()
                                   if item.is_visible() and normalize_question(item.inner_text()) == parent_name]
                        if len(parents) == 1:
                            option, source_parent = parents[0], True
                            break
                if option is not None:
                    hierarchical = source_parent or option.locator('svg[class*="chevron-right" i]').count() > 0
                    option.click(timeout=3000)
                    if hierarchical:
                        page.wait_for_timeout(700)
                        children = [item for item in surface.get_by_role("option").all() if item.is_visible()]
                        leaf = self._matching_option(children, answer, "How Did You Hear About Us?")
                        if leaf is not None:
                            leaf.click(timeout=3000)
                    page.keyboard.press("Escape")
                    filled += 1
        controls = surface.locator("input, textarea, select, .select2-selection[role=combobox]")
        processed_date_groups = set()
        for index in reversed(range(controls.count())):
            control = controls.nth(index)
            date_section = ''
            try:
                tag = control.evaluate("el => el.tagName.toLowerCase()")
                kind = (control.get_attribute("type") or "").lower()
                if control.evaluate("el => el.matches('.select2-selection[role=combobox]')"):
                    if control.evaluate('el => {' + QUESTION_HELPERS + 'return selected(el);}'):
                        continue
                    label = self._label(control)
                    answer = profile.answer(label, profile.jurisdiction(job), job['company'] or '', job['location'] or '')
                    if answer is None:
                        continue
                    control.click(timeout=3000)
                    options = surface.get_by_role('option')
                    options.first.wait_for(state='visible', timeout=3000)
                    option = self._matching_option(options.all(), answer, label)
                    if option is not None:
                        option.click(timeout=3000)
                        filled += 1
                    else:
                        control.press('Escape')
                    continue
                date_section = control.get_attribute('data-automation-id') or ''
                if date_section in {'dateSectionMonth-input', 'dateSectionDay-input', 'dateSectionYear-input'}:
                    container = control.locator('xpath=ancestor::*[starts-with(@data-automation-id,"formField")][1]')
                    record_label = self._label(control)
                    if re.match(r'^(employment|education) record \d+ ', record_label):
                        value = profile.answer(record_label, profile.jurisdiction(job))
                        if value is None:
                            continue
                        if date_section == 'dateSectionMonth-input':
                            value = str(datetime.strptime(value, '%B').month).zfill(2)
                        display = container.locator('[data-automation-id="' + date_section.replace('-input', '-display') + '"]')
                        if display.count() and display.is_visible():
                            display.click(timeout=3000)
                            control.press('ControlOrMeta+A')
                            control.press_sequentially(value, delay=50)
                        else:
                            control.fill(value)
                        control.press('Tab')
                        if int(control.input_value()) != int(value):
                            raise ValueError('Employment/education date did not retain the saved value')
                        filled += 1
                        continue
                    group_key = container.get_attribute('data-automation-id')
                    if group_key in processed_date_groups:
                        continue
                    processed_date_groups.add(group_key)
                    question = control.evaluate("el => el.closest('[data-automation-id^=formField]')?.innerText || ''")
                    value = profile.answer(question, profile.jurisdiction(job), job['company'] or '', job['location'] or '')
                    if value and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                        date = datetime.strptime(value, '%Y-%m-%d')
                        for section, part in (('Month', date.month), ('Day', date.day), ('Year', date.year)):
                            field = container.locator('[data-automation-id="dateSection' + section + '-input"]')
                            display = container.locator('[data-automation-id="dateSection' + section + '-display"]')
                            if display.count() and display.is_visible():
                                # Workday's input is offscreen; its visible display
                                # activates the segmented keyboard editor. fill()
                                # can shift digits into the following segment.
                                display.click(timeout=3000)
                                field.press('ControlOrMeta+A')
                                field.press_sequentially(str(part).zfill(2), delay=50)
                            else:
                                field.fill(str(part))
                        container.locator('[data-automation-id="dateSectionYear-input"]').press('Tab')
                        actual = [container.locator('[data-automation-id="dateSection' + section + '-input"]').input_value()
                                  for section in ('Month', 'Day', 'Year')]
                        if [int(part) for part in actual] != [date.month, date.day, date.year]:
                            raise ValueError('Calendar start date did not retain the approved value')
                        filled += 1
                    continue
                if kind in {"hidden", "submit", "button"} or (kind != 'file' and control.get_attribute("aria-hidden") == "true"):
                    continue
                if kind == "file":
                    if control.locator('xpath=ancestor::*[contains(@class,"oracletaleocwsv2-dynamic-content-resume")]').count():
                        continue  # Staged widget is committed by _save_inline_resume.
                    if control.evaluate("el => Boolean(el.files && el.files.length)"):
                        continue
                    # Portaled attachment pickers can have anonymous file inputs.
                    # Use only the nearest single-upload dialog's explicit label,
                    # never whole-page text (which also mentions cover letters).
                    upload_dialog_label = control.evaluate(r'''el => {
                        const dialog = el.closest('[role="dialog"]');
                        if (!dialog || dialog.querySelectorAll('input[type="file"]').length !== 1) return '';
                        const text = dialog.textContent || '';
                        const match = text.match(/(?:type\s+or\s+paste|upload|attach)\s+(?:your\s+)?(cover\s+letter|resume|curriculum\s+vitae|cv)\b/i);
                        return match ? match[1] : '';
                    }''')
                    file_identity = " ".join(filter(None, (
                        control.get_attribute("id"), control.get_attribute("name"),
                        control.get_attribute("aria-label"), self._label(control), upload_dialog_label,
                    )))
                    identity_text = normalize_question(file_identity)
                    if 'cover' not in identity_text and (
                        re.search(r'resume|curriculum|\bcv\b', identity_text)
                        or (not re.search(r'additional|attachment|document', identity_text)
                            and (control.get_attribute('required') is not None
                                 or surface.locator('input[type=file]').count() == 1))
                    ):
                        control.set_input_files(str(resume))
                        filled += 1
                    continue
                if not control.is_visible():
                    visible_choice = control.locator(
                        "xpath=ancestor::*[@role='radio' or @role='checkbox'][1]"
                    ) if kind in {"radio", "checkbox"} else None
                    visible_label = control.locator("xpath=ancestor::label[1]") \
                        if kind in {"radio", "checkbox"} else None
                    linked_visible = kind in {'radio', 'checkbox'} and control.evaluate(
                        'el => {' + QUESTION_HELPERS + 'return visible(el);}')
                    if (
                        (visible_choice is None or not visible_choice.count() or not visible_choice.first.is_visible())
                        and (visible_label is None or not visible_label.count() or not visible_label.first.is_visible())
                        and not linked_visible
                    ):
                        continue
                if kind == "checkbox":
                    if control.locator('xpath=ancestor::*[contains(@class,"ashby-application-form-input-yesno")]').count():
                        continue
                    label = self._label(control)
                    answer = profile.answer(label, profile.jurisdiction(job), job['company'] or '', job['location'] or '')
                    choice_ids = control.evaluate('el => {' + QUESTION_HELPERS + 'return checkboxChoices(el).map(c => c.id);}')
                    if len(choice_ids) > 1 and all(choice_ids):
                        group_key = tuple(choice_ids)
                        if group_key in filled_checkbox_groups:
                            continue
                        peers = [surface.locator('[id=' + json.dumps(cid) + ']') for cid in choice_ids]
                        labels = [c.evaluate("el => el.labels?.[0]?.innerText || el.value") for c in peers]
                        match = self._matching_option_text(labels, answer or '', label)
                        if match is not None:
                            for peer, text in zip(peers, labels):
                                peer.set_checked(text == match)
                            filled += 1
                            filled_checkbox_groups.add(group_key)
                        continue
                    ashby_group = control.locator('xpath=ancestor::*[contains(@class,"ashby-application-form-input-checkbox-group") or (contains(@class,"application-question") and .//*[contains(@class,"application-label") and contains(@class,"multiple-select")])][1]')
                    if ashby_group.count():
                        peers = [c for c in ashby_group.locator('input[type=checkbox]').all()
                                 if c.get_attribute('name') == control.get_attribute('name') and c.is_visible()]
                        labels = [c.evaluate("el => el.labels?.[0]?.innerText || el.value") for c in peers]
                        matched = self._matching_option_text(labels, answer or '', label)
                        if matched is not None:
                            # A single saved choice selects only its matching
                            # option, not every checkbox under a shared prompt.
                            for peer, text in zip(peers, labels):
                                peer.set_checked(text == matched)
                            filled += 1
                        continue
                    name = control.get_attribute('name')
                    if name and answer in {'Yes', 'No'}:
                        peers = surface.locator('input[type="checkbox"]').all()
                        peers = [c for c in peers if c.get_attribute('name') == name]
                        if {c.get_attribute('value') for c in peers} == {'Yes','No'}:
                            control.set_checked(control.get_attribute('value') == answer)
                            filled += 1
                            continue
                    if normalize_question(str(answer or '')) in {'yes', 'i agree', 'acknowledge', 'confirmed'}:
                        control.check()
                        filled += 1
                    elif answer == 'No':
                        control.uncheck()
                    continue
                if kind == "radio":
                    group_label = control.evaluate(
                        "el => { const legend = el.closest('fieldset')?.querySelector('legend,.ashby-application-form-question-title'); "
                        "if (legend?.innerText) return legend.innerText; let node = "
                        "el.closest('.application-question'); "
                        "const leverLabel = node?.querySelector('.application-label .text, .application-label'); "
                        "if (leverLabel?.innerText) return leverLabel.innerText; node = "
                        "el.closest('[data-automation-id^=formField]'); "
                        "if (node) return node.innerText || ''; node = el.parentElement; "
                        "while (node && node !== document.body) { "
                        "const radios = node.querySelectorAll('input[type=radio]').length; "
                        "const text = (node.innerText || '').trim(); "
                        "if (radios >= 2 && text.length < 1600) return text; "
                        "node = node.parentElement; } return ''; }"
                    )
                    answer = profile.answer(
                        group_label, profile.jurisdiction(job), job["company"] or "", job["location"] or ""
                    )
                    shared_label = self._label(control)
                    labelled_group = control.evaluate("el => !!el.closest('[role=radiogroup][aria-labelledby], [role=radiogroup][aria-label]')")
                    structural_group = control.evaluate('el => {' + QUESTION_HELPERS + 'return !!structuralGroup(el);}')
                    if labelled_group or structural_group or shared_label in {'Gender', 'Race/ethnicity', 'Veteran status', 'Do you consent to receiving recruiting text messages?'}:
                        group_label = shared_label
                        answer = profile.answer(group_label, profile.jurisdiction(job), job['company'] or '', job['location'] or '')
                    name = control.get_attribute("name") or ""
                    group = surface.locator(
                        'input[type="radio"][name="{}"]'.format(name.replace('"', '\\"'))
                    ) if name else control
                    def choice_label(item):
                        return item.evaluate("el => el.labels?.[0]?.innerText?.trim() || el.closest('label')?.innerText?.trim() || el.getAttribute('aria-label') || el.value || ''")
                    option_labels = [choice_label(item) for item in group.all()]
                    matched_text = self._matching_option_text(option_labels, answer or "", group_label)
                    option_label = choice_label(control)
                    if matched_text and normalize_question(matched_text) == normalize_question(option_label):
                        wrapper = control.locator("xpath=ancestor::*[@role='radio'][1]")
                        visible_label = control.locator("xpath=ancestor::label[1]")
                        control_id = control.get_attribute('id')
                        linked_label = surface.locator('label[for=' + json.dumps(control_id) + ']') if control_id else None
                        if wrapper.count() and wrapper.first.is_visible():
                            wrapper.first.click()
                        elif visible_label.count() and visible_label.first.is_visible():
                            visible_label.first.click()
                        elif linked_label is not None and linked_label.count() == 1 and linked_label.is_visible():
                            if not control.is_checked():
                                linked_label.click(timeout=3000)
                        else:
                            control.check(timeout=3000)
                        filled += 1
                    continue
                if control.get_attribute("data-uxi-multiselect-id"):
                    label = self._label(control)
                    if "how did you hear" in normalize_question(label):
                        continue
                    answer = profile.answer(
                        label, profile.jurisdiction(job), job["company"] or "", job["location"] or ""
                    )
                    if answer is None:
                        continue
                    control.click(timeout=3000)
                    if re.fullmatch(r'education record \d+ (school|field of study)', normalize_question(label)):
                        control.fill(answer)
                    page.wait_for_timeout(700)
                    option = self._matching_option(surface.get_by_role("option").all(), answer, label)
                    if option is not None:
                        option.click(timeout=3000)
                        try:
                            control.press("Escape", timeout=1000)
                        except Exception:
                            pass
                        filled += 1
                    else:
                        control.press("Escape")
                    continue
                existing_value = str(control.input_value(timeout=1000) or "").strip()
                label = self._label(control)
                normalized_label = normalize_question(label)
                if control.evaluate('el => {' + QUESTION_HELPERS + 'return !!locationValue(el);}'):
                    if not control.evaluate('el => {' + QUESTION_HELPERS + 'return selected(el);}'):
                        answer = profile.answer(label, profile.jurisdiction(job), job['company'] or '', job['location'] or '')
                        if answer and self._select_structured_location(control, answer):
                            filled += 1
                    continue
                identity = profile.values.get('identity', {})
                if normalized_label == 'phone country code' and control.get_attribute('role') == 'combobox':
                    answer = profile.answer(label, profile.jurisdiction(job))
                    if answer and self._matching_option_text([existing_value], answer, label) is None:
                        if self._select_combobox_option(page, surface, control, answer, label):
                            filled += 1
                    continue
                if normalized_label in {'phone extension', 'extension'} and not identity.get('phone_extension'):
                    # Repair only our known old mapping (main number copied
                    # into extension), not an unrelated user-entered extension.
                    if existing_value and re.sub(r'\D', '', existing_value) == re.sub(r'\D', '', identity.get('phone', '')):
                        control.fill('')
                    continue
                # Masked phone fields can contain just a default dialing code.
                # That is not a completed applicant phone number.
                incomplete_phone = (tag == 'input' and (kind == 'tel' or
                    re.fullmatch(r'(?:mobile |cell |home |primary )?(?:phone|telephone)(?: number)?', normalized_label))
                    and not any(term in normalized_label for term in ('country', 'extension', 'code'))
                    and (len(re.sub(r'\D', '', existing_value)) < 7 or
                         not control.evaluate('el => el.validity.valid')))
                uncommitted_combobox = control.get_attribute('role') == 'combobox' and not control.evaluate(
                    'el => {' + QUESTION_HELPERS + 'return selected(el);}')
                placeholder_select = tag == 'select' and control.evaluate(
                    'el => {' + QUESTION_HELPERS + 'return !selected(el);}')
                if existing_value and not incomplete_phone and not uncommitted_combobox and not placeholder_select and not re.match(r'^[-\s]*(?:select|choose)(?:\b|\.{3})', existing_value, re.I):
                    continue
                if (
                    profile.values.get("relationships", {}).get("employee_referral") is False
                    and any(term in normalized_label for term in ("refer", "referral"))
                    and any(term in normalized_label for term in (
                        "full name", "referrer name", "name of referral", "who referred",
                    ))
                    and control.get_attribute("required") is None
                    and control.get_attribute("aria-required") != "true"
                ):
                    # Leave optional referrer-name fields empty when there was
                    # no referral.  Otherwise the generic identity mapping can
                    # mistake "full name" for the applicant's own name.
                    continue
                answer = profile.answer(
                    label, profile.jurisdiction(job), job["company"] or "", job["location"] or ""
                )
                if answer is None:
                    continue
                if control.get_attribute("role") == "combobox":
                    if self._select_combobox_option(page, surface, control, answer, label):
                        filled += 1
                elif tag == "select":
                    options = control.locator("option").all_text_contents()
                    option = self._matching_option_text(options, answer, label)
                    if option is not None:
                        control.select_option(label=option)
                        control.focus()
                        control.press('Tab')
                        filled += 1
                else:
                    if kind == 'tel' or incomplete_phone:
                        # Use only equivalent formats of the saved number.
                        # Test HTML constraints on a detached clone; never guess
                        # a new number or repeatedly submit invalid forms.
                        digits = re.sub(r'\D', '', answer)
                        candidates = [answer, digits]
                        national = digits[1:] if len(digits) == 11 and digits.startswith('1') else digits
                        if len(national) == 10:
                            candidates.extend([national, f'{national[:3]}-{national[3:6]}-{national[6:]}',
                                f'({national[:3]}) {national[3:6]}-{national[6:]}'])
                        answer = control.evaluate('''(el, values) => {
                            const clone = el.cloneNode(false);
                            return values.find(value => { clone.value = value;
                                const max = el.maxLength;
                                return (max < 0 || value.length <= max) && clone.checkValidity();
                            }) || null;
                        }''', list(dict.fromkeys(candidates)))
                        if answer is None:
                            continue
                    control.fill(answer)
                    filled += 1
            except Exception as exc:
                if date_section in {'dateSectionMonth-input', 'dateSectionDay-input', 'dateSectionYear-input'}:
                    raise ValueError('Calendar date could not be filled and verified; draft retained') from exc
                continue
        # Workday renders required select fields as buttons rather than native
        # select/combobox inputs.
        prompts = surface.locator('button[aria-haspopup="listbox"]:visible, '
                                 '.fab-Select button[aria-haspopup="true"]:visible, '
                                 '[data-fabric-component="Select"] button[aria-haspopup="true"]:visible')
        for index in range(prompts.count()):
            control = prompts.nth(index)
            try:
                paired = control.evaluate('el => {' + QUESTION_HELPERS + 'return !!pairedSelect(el);}')
                if paired and control.evaluate('el => {' + QUESTION_HELPERS + 'return selected(el);}'):
                    continue
                current = normalize_question(control.inner_text())
                if not paired and current not in {"select one", "select", ""}:
                    continue
                label = self._label(control)
                if not paired and not re.match(r'^(employment|education) record \d+ ', label):
                    label = control.evaluate(
                        "el => el.closest('[data-automation-id^=formField]')?.innerText || "
                        "el.parentElement?.parentElement?.parentElement?.innerText || ''"
                    )
                answer = profile.answer(
                    label, profile.jurisdiction(job), job["company"] or "", job["location"] or ""
                )
                if answer is None:
                    continue
                control.click(timeout=3000)
                page.wait_for_timeout(600)
                # Workday briefly keeps the previous dropdown's options in
                # the DOM. Select from this button's active list, not a stale
                # Yes/No option belonging to a different question.
                active_list = control.get_attribute('data-menu-id') if paired else control.get_attribute('aria-controls')
                # Paired menu widgets must identify their own menu; never pick
                # a stale menu item or unrelated page-navigation action.
                if paired and not active_list:
                    control.press('Escape')
                    continue
                role = 'menuitem' if paired else 'option'
                options = surface.locator('[id="{}"]'.format(active_list.replace('"', '\\"'))).get_by_role(role) if active_list else surface.get_by_role(role)
                option = self._matching_option(options.all(), answer, label)
                if option is not None:
                    option_text = option.inner_text().strip()
                    option.click(timeout=3000)
                    if paired:
                        retained = control.evaluate('el => {' + QUESTION_HELPERS + 'return selectedAnswer(el);}')
                        if normalize_question(retained) != normalize_question(option_text):
                            continue
                    filled += 1
                else:
                    control.press("Escape")
            except Exception:
                continue
        return filled

    @staticmethod
    def _advance_to_application(page, job=None):
        # Never mistake an unexpanded returning-applicant form for a landing
        # page and click its final Apply button without a submission intent.
        if PlaywrightFormAdapter._form_surface(page) is not None:
            return page
        # A careers index often links by role title, not by an Apply label.
        # Match only a unique exact title (or an explicit bilingual component),
        # never the first opening or a general-application/newsletter link.
        if job is not None:
            title = str(job['title'] or '')
            titles = {normalize_question(part) for part in [title] + re.split(r'\s+/\s+', title)}
            titles = {part for part in titles if len(part.split()) >= 2}
            matches = {}
            for link in page.locator('a[href]:visible').all():
                label = normalize_question(link.inner_text())
                target = urljoin(page.url, link.get_attribute('href') or '')
                if (label in titles and public_http_url(target)
                        and not urlparse(target).username
                        and not prohibited_automation_url(target) and not aggregator_url(target)
                        and target.split('#', 1)[0].rstrip('/') != page.url.split('#', 1)[0].rstrip('/')):
                    matches[target] = link
            if len(matches) == 1:
                page.goto(next(iter(matches)), wait_until='domcontentloaded', timeout=30000)
                return page
            if len(matches) > 1:
                return page  # Ambiguous duplicate titles require disambiguation.
        # Ashby labels its navigation tab "Application", not "Apply".
        if 'jobs.ashbyhq.com/' in page.url:
            # The client-rendered overview often appears after DOMContentLoaded.
            try:
                page.get_by_role('link', name=re.compile(r'^Apply for this Job$', re.I)).first.wait_for(state='visible', timeout=10000)
            except Exception:
                pass
            application = page.get_by_role('link', name=re.compile(r'^Application$'))
            for link in application.all():
                href = link.get_attribute('href')
                if link.is_visible() and href:
                    page.goto(urljoin(page.url, href), wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_timeout(1500)
                    return page
        # Some ATS method choosers use an anchor with role=button. Prefer
        # Apply Manually over returning to the posting's Apply button.
        manual = page.locator('a[data-automation-id="applyManually"]:visible')
        if manual.count():
            href = manual.last.get_attribute('href')
            if href and urljoin(page.url, href).startswith(('https://', 'http://')):
                page.goto(urljoin(page.url, href), wait_until='domcontentloaded', timeout=30000)
                return page
        pattern = re.compile(r"^\s*(?:apply(?: now| for (?:this )?(?:job|position)| to (?:this )?position)?|i['’]m interested|apply manually|postuler(?: maintenant| à (?:cette offre(?: d['’]emploi)?|ce poste))?|je suis intéressé(?:e)?)\s*[»›→!]*\s*$", re.I)
        links = page.locator('a').filter(has_text=pattern).all()
        link = next((item for item in links if item.is_visible() and item.is_enabled()), None)
        if link is not None and link.get_attribute("href"):
            href = urljoin(page.url, link.get_attribute('href'))
            if href.startswith(('https://', 'http://')) and not link.get_attribute('href').startswith('#'):
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
            else:
                link.click(timeout=5000)
            return page
        buttons = page.get_by_role("button", name=pattern).all()
        button = next((item for item in buttons if item.is_visible() and item.is_enabled()), None)
        if button is not None:
            button.click(timeout=5000)
            return page
        # Employer pages commonly embed Jobvite/iCIMS/Greenhouse inside a
        # frame. Follow only visible application controls in those frames.
        for frame in getattr(page, 'frames', [])[1:]:
            for role in ('link', 'button'):
                candidates = frame.get_by_role(role, name=pattern).all()
                visible = [c for c in candidates if c.is_visible() and c.is_enabled()]
                if len(visible) == 1:
                    visible[0].click(timeout=5000)
                    return page
        return page

    @staticmethod
    def _native_chrome_path() -> Optional[Path]:
        configured = os.environ.get("JOBFLOW_NATIVE_CHROME", "").strip()
        candidates = [
            Path(configured) if configured else None,
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        return next((path for path in candidates if path is not None and path.is_file()), None)

    @staticmethod
    def _free_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _devtools_targets(endpoint: str) -> List[Dict]:
        try:
            with urlopen(endpoint + "/json", timeout=1) as response:
                payload = json.load(response)
            return payload if isinstance(payload, list) else []
        except Exception:
            return []

    @staticmethod
    def _handoff_confirmation(targets: List[Dict], expected_url: str = "") -> Optional[str]:
        for target in targets:
            url = str(target.get("url") or "")
            if expected_url:
                expected, actual = urlparse(expected_url), urlparse(url)
                if actual.netloc != expected.netloc:
                    continue
                if 'lever.co' in expected.netloc and actual.path.rstrip('/') != expected.path.rstrip('/').removesuffix('/apply') + '/thanks':
                    continue
                if 'lever.co' not in expected.netloc:
                    base = expected.path.rstrip('/')
                    if actual.path.rstrip('/') != base and not actual.path.startswith(base + '/'):
                        continue
                    # Hosted embed forms share one path: the job token must also match.
                    if '/embed/' in base and actual.query != expected.query:
                        continue
            title = str(target.get("title") or "")
            signal = normalize_question("{} {}".format(url, title))
            if any(marker in signal for marker in (
                "thank you", "thankyou", "thank-you", "application submitted",
                "application received", "application complete", "submission confirmed",
            )):
                return url
            if re.search(r"/(?:thanks|thank-you|confirmation|submitted)(?:[/?#]|$)", url, re.I):
                return url
        return None

    @staticmethod
    def _inspect_required(surface):
        """Read actual dropdown choices without filling or submitting anything."""
        fields = surface.evaluate(REQUIRED_QUESTIONS)
        for field in fields:
            if field.get('selected') or field.get('role') != 'combobox' or not field.get('id'):
                continue
            control = surface.locator('[id="{}"]'.format(field['id'].replace('"', '\\"')))
            try:
                control.click(timeout=2000)
                options = AssistedApplicationService._combobox_options(surface, control)
                try:
                    options.first.wait_for(state='visible', timeout=1000)
                except Exception:
                    pass  # Async location/school searches may initially have no options.
                field['options'] = [text for text in options.all_text_contents()
                    if text.strip() and not re.fullmatch(r'[-\s]*(?:select|choose)(?: one)?[.\s]*', text, re.I)]
            finally:
                control.press('Escape', timeout=2000)
        return fields

    @staticmethod
    def _preflight_unknown(surface, job, profile, answer_for=None, stop_after_first=False, batch_for=None) -> List[str]:
        """Read required questions before uploading the resume or filling the form."""
        fields = AssistedApplicationService._inspect_required(surface)
        if isinstance(profile, LocalAnswerProfile):
            profile.local_answers.clear()
        profile.bind_form_countries(fields, job)
        binding_error = bind_existing_records(profile.values, fields)
        if binding_error:
            return [binding_error]
        # Employer wording can establish a work country that discovery omitted.
        # Share that evidence only within this job, never across applications.
        form_countries = set()
        for field in fields:
            prompt = normalize_question(field['question'])
            if not re.search(r'authoriz|authoris|right to work|sponsor|visa', prompt):
                continue
            if re.search(r'\b(?:united states|us|u s|h 1b|h1b)\b', prompt):
                form_countries.add('US')
            if re.search(r'\bcanada\b', prompt):
                form_countries.add('CA')
        if len(form_countries) == 1 and not profile.jurisdiction(job):
            profile._form_country_evidence = (profile._job_key(job), next(iter(form_countries)))
        elif len(form_countries) > 1:
            profile._form_country_evidence = None
        scoped = profile.values.setdefault('additional_answers', {})
        for question, generated in getattr(profile, '_conditional_answers', {}).items():
            if scoped.get(question) == generated:
                scoped.pop(question)
        profile._conditional_answers = {}
        unknown = []
        # When a required personal acknowledgment cannot go to the model,
        # detect it before spending minutes generating other answers. Do not
        # preempt conditional questions whose applicability is resolved below.
        if stop_after_first and answer_for is not None:
            from .answer_policy import manual_only
            for field in fields:
                question = field['question']
                if (field.get('selected') or field['kind'] == 'file'
                        or normalize_question(question).startswith(('if ', 'unless '))):
                    continue
                if (manual_only(question) and profile.answer(question, profile.jurisdiction(job),
                        job['company'] or '', job['location'] or '') is None):
                    return [question + (' [options: ' + ' / '.join(field['options']) + ']' if field['options'] else '')]
        if batch_for is not None:
            batch_for(fields)
        previous_question = ''
        previous_answer = None
        for field in fields:
            question = field['question']
            if field['kind'] == 'file':
                previous_question, previous_answer = '', None
                continue
            normalized = normalize_question(question)
            if field['selected']:
                previous_question = normalized
                selected_answer = normalize_question(field.get('selectedAnswer') or '')
                previous_answer = 'No' if selected_answer == 'no' else 'Yes' if selected_answer == 'yes' else None
                continue
            if normalized.startswith('current employment end ') and profile.values.get('current_employment', {}).get('current') is True:
                continue
            current_record = re.match(r'^(employment record \d+) end ', normalized)
            if current_record and profile.answer(current_record.group(1) + ' current', '') == 'Yes':
                continue
            scoped = profile.values.setdefault('additional_answers', {})
            if 'application password' in normalized:
                description = html.unescape(re.sub('<[^>]+>', ' ', job['description'] or ''))
                password = re.search(r'Application password\s*:\s*([\w-]+)', description, re.I)
                if password:
                    scoped[question] = password.group(1)
            if normalized.startswith('if yes') and previous_answer == 'No' and question not in scoped:
                scoped[question] = 'Not applicable'
                profile._conditional_answers[question] = scoped[question]
            elif normalized.startswith('if yes') and 'government' in previous_question and previous_answer == 'Yes' and question not in scoped:
                detail = profile.values.get('government_history', {}).get('resume_roles')
                if detail:
                    scoped[question] = detail
                    profile._conditional_answers[question] = detail
            if 'commuting distance' in normalized and all(city in normalized for city in ('nyc', 'chicago', 'sf', 'la')):
                if normalize_question(profile.values.get('identity', {}).get('city', '')) == 'gatineau':
                    scoped[question] = 'No'
            if field['kind'] == 'checkbox' and 'demographic' in normalized and 'consent' in normalized:
                # Separate optional demographic processing from required application notices.
                answer = profile.values.get('demographic_processing_consent')
                if answer is not None:
                    scoped[question] = 'Yes' if answer else 'No'
            options = field['options']
            if normalized in {'important', 'required', 'consent', 'acknowledgement', 'acknowledgment'} and len(options) == 1:
                privacy = application_privacy_answer(options[0], profile.values)
                if privacy is not None:
                    scoped[question] = privacy
                    profile._conditional_answers[question] = privacy
            answer = (answer_for(question, options) if answer_for is not None else
                      profile.answer(question, profile.jurisdiction(job), job['company'] or '', job['location'] or ''))
            # Searchable school/city lists are partial until typed, not exhaustive.
            dynamic_search = (normalized in {'school', 'school *', 'school or university', 'location city', 'location city *'}
                or (normalized == 'phone country code' and field.get('role') == 'combobox')
                or bool(re.fullmatch(r'education record \d+ school', normalized)))
            # A standalone acknowledgement checkbox exposes its own full
            # prompt as its option, not a separate "Yes" label. Only a saved
            # affirmative answer to that exact prompt authorizes checking it.
            acknowledged_checkbox = (field['kind'] == 'checkbox' and len(options) == 1
                and normalize_question(options[0]) == normalized
                and normalize_question(str(answer or '')) in {'yes', 'i agree', 'acknowledge', 'confirmed'})
            if answer is None or (options and not dynamic_search and not acknowledged_checkbox
                                 and AssistedApplicationService._matching_option_text(options, answer, question) is None):
                unknown.append((question or 'Unlabelled required field') + (' [options: ' + ' / '.join(options) + ']' if options else ''))
                # One unanswerable required field already prevents submission.
                # Do not spend more model calls collecting every other blocker.
                if stop_after_first:
                    return unknown
            previous_question, previous_answer = normalized, answer
        return list(dict.fromkeys(unknown))

    @staticmethod
    def _missing_required(surface) -> List[str]:
        """Capture question prompts and choices, including custom ATS required groups."""
        return surface.evaluate(MISSING_QUESTIONS)

    def _set_residence_country(self, page, surface, profile):
        """Set saved residence before reading dependent province/postal fields.

        Employer forms often default to the job's country, not the candidate's.
        Only exact identity Country fields are eligible, never citizenship or
        work-authorization questions.
        """
        country = profile.values.get('identity', {}).get('country')
        if not country:
            return False
        changed = False
        for control in surface.locator('select:visible').all():
            if normalize_question(self._label(control)) != 'country':
                continue
            option = self._matching_option_text(control.locator('option').all_text_contents(), country, 'Country')
            if option and control.locator('option:checked').inner_text().strip() != option.strip():
                control.select_option(label=option)
                page.wait_for_timeout(700)
                changed = True
        return changed

    @staticmethod
    def _submission_controls(surface):
        controls = surface.get_by_role('button', name=APPLICATION_SUBMIT_BUTTON)
        parsed = urlparse(surface.url)
        if (parsed.hostname or '').endswith('.successfactors.com') and parse_qs(parsed.query).get('career_job_req_id'):
            controls = controls.or_(surface.locator('form#careerform [id$="_submitBtn"][role="button"]'))
        return controls

    def _application_surface(self, page):
        return self.adapter._form_surface(page)

    def _submit_choices(self, surface):
        return [b for b in self._submission_controls(surface).all() if b.is_visible() and b.is_enabled()]

    def _fill_assessed_field(self, page, surface, control, question, field, job, resume, profile):
        """Planner interprets the question; the existing answer service owns facts."""
        if field['kind'] in {'file','password','hidden','submit'}:
            return False
        options = control.evaluate('el => {' + QUESTION_HELPERS + 'return choicesOf(el);}')
        if field['role'] == 'combobox':
            options = self._combobox_options(surface, control).all_text_contents()
        answer = self.adapter._answer_for(profile, question, job, resume, options)
        if answer is None:
            return False
        if field['kind'] == 'radio':
            name = control.get_attribute('name')
            if not name:
                return False
            peers = surface.locator('input[type=radio][name=' + json.dumps(name) + ']').all()
            labels = [c.evaluate("el=>el.labels?.[0]?.innerText || el.value") for c in peers]
            match = self._matching_option_text(labels, answer, question)
            if match is None or labels.count(match) != 1:
                return False
            chosen = peers[labels.index(match)]
            identifier = chosen.get_attribute('id')
            label = surface.locator('label[for=' + json.dumps(identifier) + ']') if identifier else None
            if not chosen.is_checked():
                if label is not None and label.count()==1 and label.is_visible():
                    label.click(timeout=3000)
                else:
                    chosen.check(timeout=3000)
            return chosen.is_checked()
        if field['kind'] == 'checkbox':
            normalized = normalize_question(answer)
            if normalized not in {'yes','no','i agree','acknowledge','confirmed'}:
                return False
            expected = normalized != 'no'
            control.set_checked(expected, timeout=3000)
            return control.is_checked() == expected
        if field['role'] == 'combobox':
            changed = self._select_combobox_option(page, surface, control, answer, question)
        elif field['kind'] in {'select-one','select-multiple'}:
            match = self._matching_option_text(control.locator('option').all_text_contents(), answer, question)
            if match is None:
                return False
            control.select_option(label=match)
            changed = True
        else:
            if control.get_attribute('readonly') is not None:
                return False
            control.fill(str(answer), timeout=3000)
            control.press('Tab', timeout=1000)
            changed = control.input_value().strip() == str(answer).strip()
        return changed and control.evaluate('el => {' + QUESTION_HELPERS + 'return selected(el);}')

    def _recover_page(self, page, job, resume, profile, deadline, goal):
        # Unknown pages are handed back to the user; no model-based navigation.
        return SimpleNamespace(state='blocked', page=page, surface=None, control=None, reason=goal)

    def _prepare_application_steps(self, page, surface, job, resume, profile, deadline):
        """Traverse draft steps only. Final submission stays with the durable caller."""
        seen = set()
        for _ in range(12):
            if time.monotonic() >= deadline:
                return page, surface, 'Application preparation time limit reached; draft retained'
            if self.adapter._captcha_present(page):
                return page, surface, 'CAPTCHA_REQUIRED: solve the visible challenge'
            # Consent managers can mount after initial navigation or on each
            # draft step. Dismiss via their normal controls before inspecting
            # dropdowns; never force-click through an overlay.
            self.adapter._dismiss_consent(page)
            expand = surface.get_by_role('button', name='Expand all sections', exact=True)
            if expand.count() and expand.first.is_visible():
                expand.first.click(timeout=5000)
            self._set_residence_country(page, surface, profile)
            if getattr(self.adapter, 'answer_provider', None) is not None:
                unknown = self._preflight_unknown(surface, job, profile,
                    answer_for=lambda question, options: self.adapter._answer_for(profile, question, job, resume, options),
                    batch_for=lambda fields: self.adapter._prefetch_answers(fields, profile, job, resume),
                    stop_after_first=True)
            else:
                unknown = self._preflight_unknown(surface, job, profile, stop_after_first=True)
            if unknown:
                recovery = self._recover_page(page, job, resume, profile, deadline,
                    'Question interpretation or saved answer failed: ' + '; '.join(unknown))
                if recovery.state == 'captcha':
                    return recovery.page, surface, recovery.reason
                if recovery.state in {'advanced','form'}:
                    page = recovery.page
                    surface = self._application_surface(page) or surface
                    continue
                return page, surface, 'Required questions need saved answers: ' + '; '.join(unknown)
            self._prefill(page, surface, job, resume, profile)
            page.wait_for_timeout(1200)
            self._prefill(page, surface, job, resume, profile)
            missing = self._missing_required(surface)
            if missing:
                recovery = self._recover_page(page, job, resume, profile, deadline,
                    'Required fields failed to fill: ' + '; '.join(missing))
                if recovery.state == 'captcha':
                    return recovery.page, surface, recovery.reason
                if recovery.state in {'advanced','form'}:
                    page = recovery.page
                    surface = self._application_surface(page) or surface
                    continue
                return page, surface, 'Required fields not completed: ' + '; '.join(missing)
            if len(self._submit_choices(surface)) == 1:
                return page, surface, ''
            next_pattern = re.compile(
                r'^(?:save and continue|continue(?: (?:your )?application)?|next(?: step)?|go to (?:the )?next step|review application)\s*[→›»]*$', re.I)
            next_controls = surface.get_by_role('button', name=next_pattern).or_(surface.get_by_role('link', name=next_pattern))
            choices = [b for b in next_controls.all() if b.is_visible() and b.is_enabled()]
            ready_deadline = min(deadline, time.monotonic() + 10)
            while not choices and time.monotonic() < ready_deadline:
                # Resume parsing and step rendering can temporarily disable or
                # remove Next after fields have finished loading.
                page.wait_for_timeout(500)
                choices = [b for b in next_controls.all() if b.is_visible() and b.is_enabled()]
            if len(choices) != 1:
                recovery = self._recover_page(page, job, resume, profile, deadline,
                    'Find the draft Next control or identify the final application submission control; do not click final submit')
                if recovery.state == 'captcha':
                    return recovery.page, surface, recovery.reason
                if recovery.state == 'submit_ready':
                    return recovery.page, recovery.surface, ''
                if recovery.state in {'advanced','form'}:
                    page = recovery.page
                    surface = self._application_surface(page) or surface
                    continue
                return page, surface, 'No unique application Next/Continue or Submit control found'
            fingerprint = (surface.url, surface.locator('body').inner_text())
            if fingerprint in seen:
                errors = surface.locator('[role=alert], [data-automation-id="errorMessage"]').all_text_contents()
                recovery = self._recover_page(page, job, resume, profile, deadline,
                    'Draft did not advance; assess visible errors: ' + '; '.join(errors))
                if recovery.state == 'captcha':
                    return recovery.page, surface, recovery.reason
                if recovery.state in {'advanced','form'}:
                    page = recovery.page
                    surface = self._application_surface(page) or surface
                    continue
                return page, surface, 'Application step did not advance: ' + '; '.join(errors or ['employer validation needs inspection'])
            seen.add(fingerprint)
            previous_pages = list(page.context.pages)
            previous_url = page.url
            choices[0].click(timeout=10000)
            page.wait_for_timeout(2000)
            opened = [candidate for candidate in page.context.pages
                      if candidate not in previous_pages and not candidate.is_closed()
                      and candidate.opener() in (None, page)]
            if len(opened) > 1:
                return page, None, 'Application opened multiple destination tabs; draft retained for verification'
            if opened:
                page = opened[0]
                page.wait_for_load_state('domcontentloaded', timeout=30000)
            if opened or page.url != previous_url:
                if not public_http_url(page.url) or prohibited_automation_url(page.url) or aggregator_url(page.url):
                    return page, None, 'Next control led outside an allowed employer/ATS page; no personal data entered'
                self.adapter._dismiss_consent(page)
                # Identity-intake pages may hand off to an ATS login/manual
                # application chooser instead of directly to another form.
                for _handoff in range(3):
                    if self.adapter._captcha_present(page):
                        return page, None, 'CAPTCHA_REQUIRED: solve the visible challenge'
                    if self.adapter._form_surface(page) is not None:
                        break
                    if self.adapter.account_automation.looks_like_login(page):
                        resolution = self.adapter.account_automation.resolve(
                            page, page.url, profile.values.get('identity', {}), job.get('company', '') if isinstance(job, dict) else job['company'], page.url, profile=profile)
                        if resolution.state != 'resolved':
                            return page, None, resolution.detail
                    else:
                        self._advance_to_application(page, job)
                        page.wait_for_timeout(1500)
                    if not public_http_url(page.url) or prohibited_automation_url(page.url) or aggregator_url(page.url):
                        return page, None, 'ATS handoff led outside an allowed application page'
            surface = self.adapter._form_surface(page)
            # Workday can briefly remove the form while loading the next step.
            # Observe the transition; never click Next a second time to hurry it.
            transition_deadline = min(deadline, time.monotonic() + 15)
            while surface is None and time.monotonic() < transition_deadline:
                page.wait_for_timeout(500)
                surface = self.adapter._form_surface(page)
            if surface is None:
                recovery = self._recover_page(page, job, resume, profile, deadline,
                    'After advancing the draft, the next application form could not be recognized')
                if recovery.state == 'captcha':
                    return recovery.page, None, recovery.reason
                if recovery.state in {'advanced','form'}:
                    page = recovery.page
                    surface = self._application_surface(page)
                    if surface is not None:
                        continue
                return page, surface, 'Next application step could not be recognized; draft retained'
        return page, surface, 'Application exceeded 12 draft steps; no submission attempted'

    def _privacy_intake(self, page, profile):
        """Handle a dedicated ATS privacy gate before its application exists."""
        if not profile.values.get('application_privacy_consent'):
            return False
        for surface in page.frames:
            headings = ' '.join(surface.locator('h1,h2,h3,[role=heading]').all_text_contents())
            if not re.search(r'data consent|privacy (?:notice|consent|policy)', headings, re.I):
                continue
            changed = False
            for control in surface.locator('select:visible').all():
                label = self._label(control)
                if not re.search(r'country|location of residence', label, re.I):
                    continue
                country = str(profile.values.get('identity', {}).get('country') or '')
                options = control.locator('option').all_text_contents()
                match = next((option for option in options if option.strip().casefold() == country.casefold()), None)
                if match and control.locator('option:checked').inner_text().strip() != match.strip():
                    control.select_option(label=match)
                    page.wait_for_timeout(1200)
                    changed = True
            # Classify the consent control, not every sentence of the policy:
            # "we do not sell data" is not the applicant declining consent.
            unknown = self.adapter.account_automation._consents(surface, profile)
            buttons = surface.get_by_role('button', name=re.compile(r'^(?:I agree|Agree|Accept|I accept|Accept and continue|Continue)$', re.I))
            choices = [b for b in buttons.all() if b.is_visible() and b.is_enabled()]
            if not unknown and len(choices) == 1 and application_privacy_answer(
                'Privacy notice: ' + headings + ' ' + choices[0].inner_text(), profile.values) == 'Yes':
                choices[0].click(timeout=5000)
                page.wait_for_timeout(1500)
                return True
            if changed:
                return True
        return False

    @staticmethod
    def _submit_after_email_verification(page) -> bool:
        """Finish an already-started submission if verification did not auto-submit.

        The caller must persist submission intent before entering this step.
        An entered email code is not itself a successful application.
        """
        confirmed, _ = PlaywrightFormAdapter._confirmation(page, [])
        if confirmed or PlaywrightFormAdapter._captcha_present(page):
            return False
        surface = PlaywrightFormAdapter._form_surface(page)
        if surface is None or AssistedApplicationService._missing_required(surface):
            return False
        buttons = AssistedApplicationService._submission_controls(surface)
        enabled = [button for button in buttons.all() if button.is_visible() and button.is_enabled()]
        if len(enabled) != 1:
            return False
        enabled[0].click(timeout=10000)
        return True

    def _prepare_form_resume(self, job, resume):
        """Keep an immutable copy of the unchanged file used for this application."""
        return self.repository.snapshot_resume(job['id'], resume)

    def _tailor_ready_form(self, surface, job, original):
        """Never spend model work on an unqualified or incomplete application.

        Draft steps may require the original file first. Only replace it where
        the final form still exposes an explicit resume upload control.
        """
        from .daily import candidate_reason
        from .policy import settings
        reason = candidate_reason(self.repository.job_input(job['id']), settings())
        if reason:
            print('RESUME_ORIGINAL: job #{}; fit not established: {}'.format(job['id'], reason), flush=True)
            return self.repository.snapshot_resume(job['id'], original)
        if self._missing_required(surface):
            return self.repository.snapshot_resume(job['id'], original)
        replaceable = False
        for control in surface.locator('input[type=file]').all():
            identity = normalize_question(' '.join(filter(None, (
                control.get_attribute('id'), control.get_attribute('name'),
                control.get_attribute('aria-label'), self._label(control)))))
            if re.search(r'resume|curriculum|\bcv\b', identity) and 'cover' not in identity and control.is_enabled():
                replaceable = True
                break
        if not replaceable:
            print('RESUME_ORIGINAL: job #{}; final step has no verified resume replacement control'.format(job['id']), flush=True)
            return self.repository.snapshot_resume(job['id'], original)
        selected = self._prepare_form_resume(job, original)
        # _prefill intentionally skips occupied file controls. Explicitly
        # replace the draft original here or the audit would name the wrong file.
        control.set_input_files(str(selected))
        attached = control.evaluate('(el) => Array.from(el.files || []).map(f => ({name:f.name,size:f.size}))')
        if attached != [{'name': selected.name, 'size': selected.stat().st_size}]:
            raise ValueError('Tailored resume replacement was not verified; do not submit')
        return selected

    def _run_native_chrome_handoff(
        self,
        job_id: int,
        job,
        url: str,
        resume: Path,
        profile: ApplicationProfile,
        deadline: float,
        sync_playwright,
        auto_submit: bool = False,
        allow_preference_override: bool = False,
        skip_captcha_wait: bool = False,
    ) -> SubmissionDecision:
        """Prefill in regular Chrome, disconnect automation, then watch only the tab URL/title."""
        chrome_path = self._native_chrome_path()
        if chrome_path is None:
            return SubmissionDecision("failed", "Regular Google Chrome is not installed")

        self.browser_profile.mkdir(parents=True, exist_ok=True)
        existing_endpoint = os.environ.get('JOBFLOW_EXISTING_CHROME_ENDPOINT', '')
        if existing_endpoint and not re.fullmatch(r'http://127\.0\.0\.1:\d+', existing_endpoint):
            return SubmissionDecision('failed', 'Existing Chrome endpoint must be local')
        port = self._free_local_port() if not existing_endpoint else None
        endpoint = existing_endpoint or "http://127.0.0.1:{}".format(port)
        process = None if existing_endpoint else subprocess.Popen(
            [
                str(chrome_path),
                "--remote-debugging-port={}".format(port),
                "--user-data-dir={}".format(self.browser_profile),
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        submitted_click = False
        captcha_waiting = False
        receipt = None
        chrome_scope = None
        created_target_id = None
        terminal_state = None
        resume_prepared = False
        def finish(state, detail, receipt_url=''):
            nonlocal terminal_state
            assessment = getattr(self, '_last_page_assessment', '')
            if state == 'manual_action_required' and assessment and assessment not in detail:
                detail += '; Local page assessment: ' + assessment
            self.repository.record_submission(job_id, None, provider_for_url(url), state, receipt_url, detail,
                                              resume_path=resume if resume_prepared else None)
            terminal_state = state
            if state == 'submitted':
                self.repository.transition(job_id, 'submitted', detail)
            return SubmissionDecision(state, detail, receipt_url)
        try:
            for _ in range(150):
                if process is not None and process.poll() is not None:
                    return SubmissionDecision("failed", "Regular Chrome exited before handoff")
                try:
                    with urlopen(endpoint + "/json/version", timeout=0.25):
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                return SubmissionDecision("failed", "Regular Chrome debugging handoff did not start")

            # Attach only long enough to populate truthful saved answers.  The
            # connection is gone before the user solves hCaptcha or submits.
            playwright = sync_playwright().start()
            try:
                # Select a tab from Chrome's lightweight target index before
                # Playwright connects; attaching every historical tab can hang.
                targets = self._devtools_targets(endpoint)
                candidates = [SimpleNamespace(url=t.get('url', ''), target_id=t['id'])
                              for t in targets if t.get('type') == 'page']
                selected = self._existing_application_page(candidates, url)
                new_scoped_tab = selected is None
                if selected is None:
                    with urlopen(Request(endpoint + '/json/new?about:blank', method='PUT'), timeout=5) as response:
                        target = json.load(response)
                    selected = SimpleNamespace(url='about:blank', target_id=target['id'])
                    created_target_id = target['id']
                chrome_scope = ScopedChromeConnection(endpoint, selected.target_id)
                scoped_endpoint = chrome_scope.__enter__()
                browser = playwright.chromium.connect_over_cdp(scoped_endpoint, timeout=30000)
                pages = [page for context in browser.contexts for page in context.pages]
                page = pages[0] if new_scoped_tab and len(pages) == 1 else self._existing_application_page(pages, url)
                if new_scoped_tab and page is not None:
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if page is None and not existing_endpoint:
                    page = next((candidate for candidate in pages if candidate.url.startswith("http")), None)
                if page is None:
                    context = browser.contexts[0]
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                else:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                self.adapter._dismiss_consent(page)
                if not allow_preference_override and cybersecurity_role(job['title'], page.locator('body').inner_text()):
                    return finish('blocked', 'Live posting is cybersecurity-related; excluded by saved preferences')
                surface = self._application_surface(page)
                for _navigation_step in range(10):
                    from .applications import application_provider_allowed
                    from .policy import settings
                    if not application_provider_allowed(dict(job, canonical_url=page.url), settings()):
                        return finish('blocked', 'Redirected application provider is disabled by user; no form filled')
                    if surface is not None:
                        break
                    if self.adapter._captcha_present(page):
                        return finish('captcha_waiting', 'CAPTCHA_REQUIRED: {} — {} (#{}); browser left open for human completion'.format(job['company'], job['title'], job_id))
                    if self._privacy_intake(page, profile):
                        surface = self.adapter._form_surface(page)
                        continue
                    if self.adapter.account_automation.looks_like_login(page):
                        resolution = self.adapter.account_automation.resolve(
                            page, url, profile.values.get('identity', {}), job['company'] or '', url, profile=profile)
                        if resolution.state != 'resolved':
                            if resolution.state == 'captcha':
                                return finish('captcha_waiting', resolution.detail)
                            recovery = self._recover_page(page, job, resume, profile, deadline,
                                'Account helper could not advance: ' + resolution.detail)
                            if recovery.state in {'advanced','form'}:
                                page = recovery.page
                                surface = self._application_surface(page)
                                continue
                            return finish('manual_action_required', resolution.detail)
                        surface = self.adapter._form_surface(page)
                        if surface is not None:
                            break
                    previous_url = page.url
                    previous_body = page.locator('body').inner_text()
                    previous_pages = list(page.context.pages)
                    try:
                        self._advance_to_application(page, job)
                    except Exception as exc:
                        recovery = self._recover_page(page, job, resume, profile, deadline,
                            'Application navigation failed: ' + type(exc).__name__)
                        if recovery.state in {'advanced','form'}:
                            page = recovery.page
                        elif recovery.state == 'captcha':
                            return finish('captcha_waiting', recovery.reason)
                        else:
                            return finish('manual_action_required', 'Application navigation recovery failed: ' + recovery.reason)
                    page.wait_for_timeout(2000)
                    new_pages = [p for p in page.context.pages if p not in previous_pages and not p.is_closed()]
                    if len(new_pages) == 1:
                        page = new_pages[0]
                        page.wait_for_load_state('domcontentloaded', timeout=30000)
                    if not public_http_url(page.url) or prohibited_automation_url(page.url) or aggregator_url(page.url):
                        return finish('manual_action_required', 'Apply control led outside an allowed employer/ATS page; no personal data entered')
                    self.adapter._dismiss_consent(page)
                    if 'jobs.ashbyhq.com/' in page.url:
                        try:
                            page.locator('input[type=email]').first.wait_for(state='visible', timeout=10000)
                        except Exception:
                            pass
                    surface = self._application_surface(page)
                    if surface is None and page.url == previous_url and page.locator('body').inner_text() == previous_body:
                        recovery = self._recover_page(page, job, resume, profile, deadline,
                            'Locate and open this role\'s application form; ordinary navigation found no form')
                        if recovery.state in {'advanced','form'}:
                            page = recovery.page
                            surface = self._application_surface(page)
                            continue
                        if recovery.state == 'captcha':
                            return finish('captcha_waiting', recovery.reason)
                        break
                if surface is None and job['source'] == 'greenhouse':
                    registry = Path(os.environ.get('JOBFLOW_GREENHOUSE_REGISTRY', 'data/greenhouse-expanded-registry.csv'))
                    if registry.is_file() and str(job['external_id'] or '').isdigit():
                        with registry.open(newline='') as entries:
                            board = next((r['board_token'] for r in csv.DictReader(entries)
                                          if r['company'].casefold() == job['company'].casefold()), '')
                        if re.fullmatch(r'[a-zA-Z0-9_-]+', board):
                            hosted = 'https://job-boards.greenhouse.io/embed/job_app?for={}&token={}'.format(board, job['external_id'])
                            page.goto(hosted, wait_until='networkidle', timeout=30000)
                            # Verify the same employer and title before entering personal data.
                            title = normalize_question(page.title())
                            if normalize_question(job['company']) in title and normalize_question(job['title']) in title:
                                surface = self.adapter._form_surface(page)
                                if surface is not None:
                                    url = page.url
                if surface is None:
                    body = page.locator('body').inner_text()
                    if page.locator('input[type="password"]:visible').count():
                        return finish('manual_action_required', 'Employer account sign-in or registration is required; application form not reached')
                    headings = ' '.join(page.locator('h1,h2,h3,[role=heading]').all_text_contents())
                    if re.search(r'^(?:privacy notice|data consent|privacy consent)$', headings.strip(), re.I) and page.locator('input[type="email"]:visible').count():
                        return finish('manual_action_required', 'Email/account intake did not advance after approved privacy handling')
                    return finish("manual_action_required", "Application form was not found in regular Chrome")
                if not application_provider_allowed(dict(job, canonical_url=surface.url), settings()):
                    return finish('blocked', 'Embedded application provider is disabled by user; no form filled')
                if self.adapter._captcha_present(page):
                    return finish('captcha_waiting',
                        'CAPTCHA_SKIPPED: application held; no resume generated' if skip_captcha_wait else
                        'CAPTCHA_REQUIRED: application form blocked; no resume generated')
                original_resume = resume
                resume = self.repository.snapshot_resume(job['id'], resume)
                resume_prepared = True
                if auto_submit:
                    page, surface, preparation_error = self._prepare_application_steps(page, surface, job, resume, profile, deadline)
                    if preparation_error:
                        return finish('captcha_waiting' if preparation_error.startswith('CAPTCHA_REQUIRED') else 'manual_action_required', preparation_error)
                    preparation_started = time.monotonic()
                    resume = self._tailor_ready_form(surface, job, original_resume)
                    # Model computation must not consume the browser interaction budget.
                    deadline += time.monotonic() - preparation_started
                    surface = self._application_surface(page)
                    if surface is None:
                        return finish('manual_action_required', 'Application form disappeared during resume preparation; no upload attempted')
                self._prefill(page, surface, job, resume, profile)
                page.wait_for_timeout(1800)
                # A second pass catches fields revealed or reset after resume parsing.
                surface = self._application_surface(page)
                if surface is not None:
                    self._prefill(page, surface, job, resume, profile)
                if auto_submit and surface is not None:
                    page.wait_for_timeout(1800)
                    missing = self._missing_required(surface)
                    if self.adapter._captcha_present(page):
                        captcha_waiting = True
                    elif missing:
                        return finish("manual_action_required", "Skipped unanswered required questions: " + "; ".join(missing))
                    else:
                        buttons = self._submit_choices(surface)
                        if len(buttons) == 1:
                            # Blur a staged calendar through normal UI before
                            # recording irreversible submission intent. An
                            # actionability-only trial never dispatches a click.
                            if surface.locator('.ui-datepicker:visible').count():
                                headings = surface.locator('h1:visible,h2:visible')
                                if headings.count():
                                    headings.first.click(timeout=3000)
                            buttons[0].click(trial=True, timeout=5000)
                            # Persist before the irreversible click. If the process
                            # dies during submission, future runs cannot blindly retry.
                            intent_id = self.repository.record_submission(job_id, None, provider_for_url(url), 'confirmation_unknown',
                                                              detail='Submission started; awaiting receipt', resume_path=resume)
                            submitted_click = True
                            verification_started = int(time.time())
                            buttons[0].click(timeout=10000)
                            page.wait_for_timeout(2000)
                            verification_error = ''
                            if self.adapter._confirmation(page, [])[0]:
                                receipt = page.url
                            if self.gmail_client is not None and not receipt:
                                try:
                                    filled_code = self.gmail_client.fill_code_challenge(
                                        page, verification_started, job['company'] or '', timeout_seconds=45)
                                    if filled_code:
                                        print('Email code entered for job #{}; awaiting employer confirmation.'.format(job_id), flush=True)
                                        self._submit_after_email_verification(page)
                                except Exception as exc:
                                    verification_error = 'Email verification failed: ' + type(exc).__name__
                            for _ in range(30):
                                if receipt:
                                    break
                                page.wait_for_timeout(1000)
                                receipt = self._handoff_confirmation([{'url': page.url, 'title': page.title()}], url)
                                if receipt:
                                    break
                                if self.adapter._confirmation(page, [])[0]:
                                    receipt = page.url
                                    break
                                if self.adapter._captcha_present(page):
                                    captcha_waiting = True
                                    break
                            if not receipt and not captcha_waiting:
                                evidence = self.adapter._capture_evidence(page, job_id)
                                errors = surface.locator('[role=alert],.field-error,.input-error,.error-message,[id$="-error"]').all_text_contents()
                                errors = list(dict.fromkeys(text.strip() for text in errors if text.strip()))
                                if any('Your form needs corrections' in error and 'Missing entry for required field' in error for error in errors):
                                    rejection = 'Employer rejected incomplete form; not submitted: ' + '; '.join(errors)[:1800] + '. Evidence: ' + str(evidence)
                                    self.repository.reconcile_rejected_intent(job_id, intent_id, rejection)
                                    return finish('manual_action_required', rejection)
                                return finish('confirmation_unknown', 'Submit clicked; no verifiable receipt after 30 seconds. '
                                              'Held against duplicate retries. Visible errors: ' + '; '.join(errors)[:1800] +
                                              '. ' + verification_error + '. Evidence: ' + str(evidence))
                        else:
                            return finish('manual_action_required', 'No unique enabled Submit button found')
            finally:
                # Stopping the CDP client disconnects from externally launched
                # Chrome without closing it.  No automation is attached while
                # the CAPTCHA is solved or the application is submitted.
                try:
                    playwright.stop()
                finally:
                    if chrome_scope is not None:
                        chrome_scope.__exit__(None, None, None)
            if receipt:
                return finish('submitted', 'Submission confirmed by employer success page', receipt)
            if captcha_waiting:
                if skip_captcha_wait:
                    return finish('captcha_waiting', 'CAPTCHA_SKIPPED: this run moves on; application held against duplicate retries')
                print('CAPTCHA_REQUIRED: {} — {} (#{}). Open your Mac through Chrome Remote Desktop.'.format(job['company'], job['title'], job_id), flush=True)
                self.repository.record_submission(job_id, None, provider_for_url(url), 'captcha_waiting', detail='Browser open for human CAPTCHA completion', resume_path=resume)
            print("Regular Chrome is ready; form control is detached. Submission confirmation is being checked.", flush=True)
            from .application_budget import application_phase_budget
            from .policy import settings
            from contextlib import nullcontext
            wait_seconds = settings().get('captcha_wait_seconds', 3600) if captcha_waiting else max(1, deadline - time.monotonic())
            wait_deadline = time.monotonic() + wait_seconds
            with application_phase_budget(wait_seconds + 5) if captcha_waiting else nullcontext():
                while time.monotonic() < wait_deadline:
                    if process is not None and process.poll() is not None:
                        break
                    receipt = self._handoff_confirmation(self._devtools_targets(endpoint), url)
                    if receipt:
                        detail = "Application confirmed in detached regular Chrome"
                        self.repository.record_submission(
                            job_id, None, provider_for_url(url), "submitted", receipt, detail, resume_path=resume
                        )
                        self.repository.transition(job_id, "submitted", detail)
                        if process is not None:
                            process.terminate()
                        return SubmissionDecision("submitted", detail, receipt)
                    time.sleep(1)
        except Exception as exc:
            detail = "Regular Chrome handoff ended: {}".format(exc)
            return finish('confirmation_unknown' if submitted_click else 'manual_action_required', detail)
        finally:
            if getattr(self, 'page_navigator', None) is not None:
                self.page_navigator.save(job_id)
            if created_target_id and not captcha_waiting and (
                terminal_state == 'submitted' or
                (not submitted_click and terminal_state in {'manual_action_required', 'blocked'}
                 and os.environ.get('JOBFLOW_KEEP_BLOCKED_TABS') != '1')
            ):
                # Close only this attempt's newly created tab after durable
                # evidence exists. Preserve pre-existing tabs and all holds.
                try:
                    with urlopen(endpoint + '/json/close/' + created_target_id, timeout=3):
                        pass
                except Exception:
                    pass  # Cleanup failure must not overwrite submission state.
            elif created_target_id and skip_captcha_wait and terminal_state in {'captcha_waiting','confirmation_unknown'}:
                # Skip-mode holds are durable records, not requests to keep
                # hundreds of unfinished tabs resident in memory. Never close
                # a pre-existing user tab or remove the duplicate-prevention hold.
                try:
                    with urlopen(endpoint + '/json/close/' + created_target_id, timeout=3):
                        pass
                except Exception:
                    pass
            if process is not None and process.poll() is None and not captcha_waiting:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        if captcha_waiting:
            return finish('captcha_waiting', 'CAPTCHA_REQUIRED: browser left open for human completion; submission not confirmed')
        detail = "Submission confirmation timed out without a verifiable receipt; no automatic retry"
        return finish('confirmation_unknown' if submitted_click else 'manual_action_required', detail)

    def run(
        self,
        job_id: int,
        resume: Path,
        profile: ApplicationProfile,
        timeout_seconds: int = 1800,
        override_eligibility: bool = False,
        native_chrome: bool = False,
        auto_submit: bool = False,
        explicitly_approved_job_ids: tuple = (),
        skip_captcha_wait: bool = False,
    ) -> SubmissionDecision:
        import fcntl

        job = self.repository.job(job_id)
        from .policy import settings
        from .applications import application_provider_allowed
        policy = settings()
        if not application_provider_allowed(job, policy):
            return SubmissionDecision('blocked', 'Application provider disabled by user; only approved ATS providers may run')
        skip_captcha_wait = skip_captcha_wait or policy.get('skip_captchas', False)
        guard = application_guard(job)
        # A fixed, explicitly approved batch can waive selection preferences,
        # never the candidate's answers, consent, URL or duplicate safeguards.
        if guard and job_id not in explicitly_approved_job_ids:
            return SubmissionDecision('blocked', guard)
        if job_id not in explicitly_approved_job_ids and cybersecurity_role(job['title'], job['description']):
            return SubmissionDecision('blocked', 'Cybersecurity-related role excluded by your current preferences')
        # Contextual answers are scoped to this employer, never carried to another job.
        profile = LocalAnswerProfile(copy.deepcopy(profile.values))
        self._assessed_surface = None
        self._assessed_submit = None
        self._last_page_assessment = ''
        self.page_navigator = None
        profile.evidence_resume = Path(resume).resolve()
        latest = self.repository.submissions(job_id)
        if any(item['state'] == 'submitted' for item in latest) or (latest and latest[0]['state'] in {'confirmation_unknown', 'captcha_waiting'}):
            return SubmissionDecision("blocked", "Already submitted or awaiting confirmation; no duplicate attempt")
        url = job["canonical_url"] or ""
        # Employer wrappers can embed the same public Greenhouse application
        # alongside marketing/challenge frames. Explicit board + posting IDs
        # identify its official ATS page without guessing company domains.
        query = parse_qs(urlparse(url).query)
        board = query.get('board', [''])[0]
        posting_id = query.get('gh_jid', [''])[0]
        if (provider_for_url(url) == 'employer' and re.fullmatch(r'[A-Za-z0-9_-]+', board)
                and posting_id.isdigit()):
            url = 'https://job-boards.greenhouse.io/' + board + '/jobs/' + posting_id
            job = dict(job, canonical_url=url)
        if job["eligibility_queue"] != "qualified" and not override_eligibility:
            raise ValueError("Assisted mode is limited to qualified jobs")
        if not public_http_url(url) or prohibited_automation_url(url) or aggregator_url(url):
            raise ValueError("A verified direct employer/ATS URL is required")
        resume = Path(resume).resolve()
        if not resume.is_file():
            raise ValueError("Shared application resume is missing")
        self.browser_profile.mkdir(parents=True, exist_ok=True)
        lock_path = self.browser_profile.parent / "assisted-browser.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return SubmissionDecision("blocked", "Another assisted browser session is already open")
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return SubmissionDecision("failed", "Playwright is not installed")

            deadline = time.monotonic() + max(60, min(int(timeout_seconds), 3600))
            if native_chrome:
                print('FINDING_APPLICATION_FORM: {} — {} (#{})'.format(job['company'], job['title'], job_id), flush=True)
                return self._run_native_chrome_handoff(
                    job_id, job, url, resume, profile, deadline, sync_playwright, auto_submit,
                    allow_preference_override=job_id in explicitly_approved_job_ids,
                    skip_captcha_wait=skip_captcha_wait,
                )
            responses: List[Dict] = []
            prefilled = set()
            verification_attempts = set()
            resume_prepared = False
            try:
                with sync_playwright() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        str(self.browser_profile), headless=False,
                    )
                    page = context.pages[0] if context.pages else context.new_page()

                    def capture_response(response):
                        try:
                            if response.request.method == "POST" and re.search(
                                r"apply|application|candidate|submission", response.url, re.I
                            ):
                                body = response.text()[:5000] if response.status < 400 else ""
                                responses.append({"url": response.url, "status": response.status, "body": body})
                        except Exception:
                            return

                    page.on("response", capture_response)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    attempted_advance = set()
                    dismissed_consent = set()
                    while time.monotonic() < deadline:
                        live_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
                        if not live_pages:
                            break
                        page = live_pages[-1]
                        if page.url not in dismissed_consent:
                            self.adapter._dismiss_consent(page)
                            dismissed_consent.add(page.url)
                        for frame in page.frames:
                            try:
                                # Assisted mode must require a visible success
                                # page/URL. Resume uploads and other background
                                # ATS requests can contain generic "success" or
                                # candidate fields before the user submits.
                                confirmed, signal = self.adapter._confirmation(frame, [])
                            except Exception:
                                continue
                            if confirmed:
                                evidence = self.adapter._capture_evidence(page, job_id)
                                receipt = frame.url
                                context.close()
                                detail = "Assisted application confirmed by {}. Evidence: {}".format(signal, evidence)
                                self.repository.record_submission(
                                    job_id, None, provider_for_url(url), "submitted", receipt, detail,
                                    resume_path=resume if resume_prepared else None
                                )
                                self.repository.transition(job_id, "submitted", detail)
                                return SubmissionDecision("submitted", detail, receipt)
                        surface = self.adapter._form_surface(page)
                        if surface is not None:
                            if self.adapter._captcha_present(page):
                                self.repository.record_submission(job_id, None, provider_for_url(url), 'captcha_waiting',
                                    detail='CAPTCHA_REQUIRED: application form blocked before resume upload')
                                return SubmissionDecision('captcha_waiting', 'CAPTCHA_REQUIRED: application form blocked before resume upload')
                            if not resume_prepared:
                                # This assisted path cannot establish final-step
                                # readiness. Keep the original, never tailor early.
                                resume = self.repository.snapshot_resume(job['id'], resume)
                                resume_prepared = True
                            key = surface.url
                            if key not in prefilled:
                                self._prefill(page, surface, job, resume, profile)
                                prefilled.add(key)
                        elif page.url not in attempted_advance:
                            attempted_advance.add(page.url)
                            try:
                                self._advance_to_application(page, job)
                            except Exception:
                                pass
                        if self.gmail_client is not None and page.url not in verification_attempts:
                            try:
                                body = page.locator("body").inner_text()
                                if re.search(
                                    r"(?:check your email|email).{0,50}(?:code|verify)|verification code",
                                    body, re.I | re.S,
                                ):
                                    verification_attempts.add(page.url)
                                    self.gmail_client.fill_code_challenge(
                                        page, int(time.time()) - 120, job["company"] or "", timeout_seconds=90
                                    )
                            except Exception:
                                pass
                        page.wait_for_timeout(1000)
                    try:
                        context.close()
                    except Exception:
                        pass
            except Exception as exc:
                detail = "Assisted browser ended: {}".format(exc)
                self.repository.record_submission(job_id, None, provider_for_url(url), "manual_action_required", detail=detail,
                                                  resume_path=resume if resume_prepared else None)
                return SubmissionDecision("manual_action_required", detail)
            detail = "Assisted session ended without verifiable submission confirmation"
            self.repository.record_submission(job_id, None, provider_for_url(url), "manual_action_required", detail=detail,
                                              resume_path=resume if resume_prepared else None)
            return SubmissionDecision("manual_action_required", detail)


class BulkApplicationService:
    def __init__(self, repository: Repository, adapter=None):
        self.repository = repository
        if adapter is not None:
            self.adapter = adapter
        else:
            gmail = GmailVerificationClient.from_environment()
            self.adapter = PlaywrightFormAdapter(
                gmail_client=gmail if gmail.configured() and gmail.connected() else None
            )

    def run(
        self, resume: Path, profile: ApplicationProfile, limit: int = 2000, retry_manual: bool = False,
        strong_only: bool = False, selected_job_ids=None, allow_review: bool = False,
    ) -> BulkApplicationReport:
        resume = Path(resume).resolve()
        if not resume.is_file():
            raise ValueError("Shared application resume is missing: {}".format(resume))
        rows = (
            [self.repository.job(job_id) for job_id in dict.fromkeys(selected_job_ids)]
            if selected_job_ids is not None else self.repository.surfaced_jobs(limit)
        )
        permitted_queues = {"qualified", "estimated_pay_review"} if allow_review else {"qualified"}
        allowed_sources = {
            value.strip().lower()
            for value in os.environ.get("JOBFLOW_APPLICATION_SOURCES", "").split(",")
            if value.strip()
        }
        if allowed_sources:
            rows = [row for row in rows if str(row["source"] or "").lower() in allowed_sources]
        if strong_only:
            rows = [row for row in rows if is_preparable(row)]
        counts = {
            "submitted": 0, "skipped_existing": 0, "manual_action_required": 0,
            "confirmation_unknown": 0, "blocked": 0, "failed": 0,
        }
        for job in rows:
            attempt_resume = None
            existing = self.repository.submissions(job["id"])
            latest = existing[0] if existing else None
            if latest and latest["state"] in {"submitted", "confirmation_unknown"}:
                counts["skipped_existing"] += 1
                continue
            if latest and latest["state"] in {"manual_action_required", "blocked"}:
                detail = (latest["detail"] or "").lower()
                automatic_login_retry = (
                    "requires an account or login" in detail
                    or "login_retry_pending" in detail
                    or "automatic_retry" in detail
                )
                if (not retry_manual and not automatic_login_retry) or job["eligibility_queue"] not in permitted_queues:
                    counts["skipped_existing"] += 1
                    continue
            url = job["canonical_url"] or ""
            guard = application_guard(job)
            if guard:
                decision = SubmissionDecision('blocked', guard)
                provider = provider_for_url(url)
            elif job["eligibility_queue"] not in permitted_queues:
                decision = SubmissionDecision("manual_action_required", "Eligibility evidence is incomplete; automatic submission is blocked")
                provider = provider_for_url(url)
            elif prohibited_automation_url(job["url"] or "") and not public_http_url(url):
                decision = SubmissionDecision("manual_action_required", "Restricted board listing; open it in user-assisted mode")
                provider = "prohibited"
            elif not public_http_url(url) or prohibited_automation_url(url) or aggregator_url(url):
                decision = SubmissionDecision("manual_action_required", "No verified direct employer application URL")
                provider = provider_for_url(url)
            else:
                provider = provider_for_url(url)
                attempt_resume = self.repository.snapshot_resume(job["id"], resume)
                decision = self.adapter.submit(job, attempt_resume, profile)
            state = decision.state if decision.state in counts else "failed"
            counts[state] += 1
            self.repository.record_submission(job["id"], None, provider, state, decision.receipt, decision.reason, resume_path=attempt_resume)
            # An unanswered required question is a per-job exception, never a
            # batch-level blocker. Record it, leave the form unsubmitted, and
            # immediately advance to the next job.
            if state == "manual_action_required":
                continue
            if state == "submitted":
                self.repository.transition(job["id"], "submitted", "Confirmed bulk application: " + decision.receipt)
        return BulkApplicationReport(len(rows), **counts)
