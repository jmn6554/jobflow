"""Supported job-discovery adapters.

Only public APIs and search indexes are queried. Indeed, Glassdoor, and
LinkedIn URLs are retained as link-only results and are never scraped.
"""

import html
import ipaddress
import csv
import json
import os
import re
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

from .models import Compensation, JobInput
from .policy import settings
from .titles import relevant_title, excluded_title
from .workplace import description_workplace


PROHIBITED_AUTOMATION_HOSTS = (
    "indeed.com",
    "indeed.ca",
    "indeed.co.uk",
    "glassdoor.com",
    "glassdoor.ca",
    "glassdoor.co.uk",
    "glassdoor.sg",
    "linkedin.com",
)

AGGREGATOR_HOSTS = (
    "jobright.ai",
    "remoterocketship.com",
    "rejobs.org",
    "trabajo.org",
    "adzuna.com",
    "adzuna.ca",
    "adzuna.co.uk",
    "analyticsengineerjobs.com",
    "arc.dev",
    "bebee.com",
    "builtin.com",
    "canadajobs.works",
    "dice.com",
    "dailyremote.com",
    "equest.com",
    "internshala.com",
    "jobleads.com",
    "jobglance.app",
    "codingjobboard.com",
    "haystackapp.io",
    "qualitycontracts.co.uk",
    "simplyhired.ca",
    "simplyhired.com",
    "pnet.co.za",
    "rigzone.com",
    "shine.com",
    "totaljobs.com",
    "usaremotejobs.app",
    "visajobshq.com",
    "wearedevelopers.com",
    "ziprecruiter.com",
)

DIRECT_ATS_HOSTS = (
    "ashbyhq.com",
    "bamboohr.com",
    "breezy.hr",
    "greenhouse.io",
    "icims.com",
    "jobvite.com",
    "lever.co",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "recruitee.com",
    "smartrecruiters.com",
    "taleo.net",
    "teamtailor.com",
    "workable.com",
)

# These organizations publish third-party listings rather than their own jobs.
# Reading their entire ATS board would recreate the noisy aggregator problem
# that direct employer feeds are intended to avoid.
ATS_AGGREGATOR_BOARDS = {
    "lever": {"jobgether"},
    "ashby": {"careerswift.ai"},
}


def aggregator_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == item or host.endswith("." + item) for item in AGGREGATOR_HOSTS)


def prohibited_automation_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == item or host.endswith("." + item) for item in PROHIBITED_AUTOMATION_HOSTS)


def direct_ats_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == item or host.endswith("." + item) for item in DIRECT_ATS_HOSTS)


def public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def strip_html(value: str) -> str:
    # Greenhouse can HTML-encode the entire markup, not just its text nodes.
    # Decode before stripping so tags do not leak into requirement parsing.
    for _ in range(2):
        value = html.unescape(value or "")
    value = re.sub(r"<(br|/p|/li|/div|/h\d)>\s*", "\n", value or "", flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", value)).strip()


def normalize_employment_type(value: str, default: str = "full-time") -> str:
    """Normalize common ATS enum spellings before eligibility checks."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", str(value or default)).replace("_", "-").lower()
    return {
        "fulltime": "full-time",
        "parttime": "part-time",
    }.get(text.replace("-", ""), text)


def infer_compensation(text: str, default_currency: str = "CAD") -> Optional[Compensation]:
    candidates = []
    pattern = re.compile(
        r"(?<![\w])(?:(USD|CAD|US\$|CA\$|\$)\s*)?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?)\s*([kK])?(?![\w%])"
    )
    for match in pattern.finditer(text):
        start, end = max(0, match.start() - 90), min(len(text), match.end() + 90)
        context = text[start:end]
        salary_context = re.search(
            r"\b(base salary|salary|pay range|compensation|annual|annum|per year|yearly|hourly|per hour)\b",
            context,
            re.I,
        )
        currency_marker = match.group(1) or re.search(r"\b(?:USD|CAD)\b|(?:US|CA)?\$", context, re.I)
        if not salary_context and not currency_marker:
            continue
        if match.group(3) and float(match.group(2).replace(",", "")) == 401:
            continue
        number = float(match.group(2).replace(",", ""))
        if match.group(3):
            number *= 1000
        hourly = bool(re.search(r"(?:/|per\s+)hour|hourly", context, re.I))
        if hourly:
            if not 10 <= number <= 500:
                continue
            interval = "hour"
        else:
            if not 50000 <= number <= 1000000:
                continue
            interval = "year"
        marker = str(match.group(1) or (currency_marker.group(0) if currency_marker else "")).upper()
        currency = "USD" if marker in {"USD", "US$"} else "CAD" if marker in {"CAD", "CA$"} else default_currency
        candidates.append((number, interval, currency))
    if not candidates:
        return None
    annual = [item for item in candidates if item[1] == "year"]
    selected = annual or candidates
    currencies = [item[2] for item in selected]
    currency = max(set(currencies), key=currencies.count)
    values = [item[0] for item in selected if item[2] == currency]
    return Compensation(min(values), max(values), currency, selected[0][1])


class DiscoveryProvider(ABC):
    name = "provider"

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client or httpx.Client(timeout=25, follow_redirects=True)

    @abstractmethod
    def fetch(self) -> Iterable[JobInput]:
        raise NotImplementedError


class AdzunaProvider(DiscoveryProvider):
    name = "adzuna"

    def __init__(
        self, app_id: str, app_key: str, countries=("ca", "us"), pages: int = 1,
        max_days_old: int = 30, client=None,
    ):
        super().__init__(client)
        self.app_id, self.app_key, self.countries = app_id, app_key, countries
        self.pages = max(1, min(pages, 10))
        self.max_days_old = max(1, min(max_days_old, 90))

    def fetch(self) -> Iterable[JobInput]:
        queries = (
            "senior data engineer",
            "data engineer",
            "analytics engineer",
            "senior data analyst",
            "data analyst",
            "senior systems analyst",
            "systems analyst",
            "business systems analyst",
            "business intelligence analyst",
            "business intelligence engineer",
            "data platform engineer",
            "data integration engineer",
            "databricks engineer",
            "data architect",
        )
        for country in self.countries:
            if country == "ca":
                searches = [
                    (query, "") for query in queries
                ] + [
                    (query + " remote", "") for query in queries
                ] + [
                    (query, location)
                    for query in queries
                    for location in ("Ottawa, Ontario", "Gatineau, Quebec")
                ]
            else:
                searches = [(query + " remote", "") for query in queries]
            for query, location_filter in searches:
                for page in range(1, self.pages + 1):
                    params = {
                        "app_id": self.app_id,
                        "app_key": self.app_key,
                        "what": query,
                        "results_per_page": 50,
                        "max_days_old": self.max_days_old,
                        "full_time": 1,
                        "permanent": 1,
                        "content-type": "application/json",
                    }
                    if location_filter:
                        params["where"] = location_filter
                    response = None
                    for attempt in range(3):
                        try:
                            response = self.client.get(
                                "https://api.adzuna.com/v1/api/jobs/{}/search/{}".format(country, page),
                                params=params,
                            )
                            response.raise_for_status()
                            break
                        except httpx.HTTPError:
                            if attempt == 2:
                                response = None
                    if response is None:
                        break
                    for item in response.json().get("results", []):
                        description = strip_html(item.get("description", ""))
                        minimum, maximum = item.get("salary_min"), item.get("salary_max")
                        compensation = None
                        if minimum is not None or maximum is not None:
                            compensation = Compensation(
                                minimum,
                                maximum,
                                "CAD" if country == "ca" else "USD",
                                is_estimated=bool(item.get("salary_is_predicted")),
                                estimate_source="Adzuna prediction" if item.get("salary_is_predicted") else "",
                                estimate_confidence="provider estimate" if item.get("salary_is_predicted") else "",
                            )
                        url = item.get("redirect_url", "")
                        location = (item.get("location") or {}).get("display_name", "")
                        workplace_text = "{} {}".format(location, description)
                        yield JobInput(
                            title=item.get("title", ""),
                            company=(item.get("company") or {}).get("display_name", "Unknown employer"),
                            location=location,
                            url=url,
                            canonical_url="",
                            description=description,
                            source=self.name,
                            external_id=str(item.get("id", "")) or None,
                            workplace_type="remote" if re.search(
                                r"\b(remote|work from home|work[- ]from[- ]anywhere|distributed)\b",
                                workplace_text,
                                re.I,
                            ) else "unspecified",
                            country=country.upper(),
                            employment_type="{} {}".format(
                                item.get("contract_type", ""), item.get("contract_time", "full-time")
                            ).strip(),
                            compensation=compensation,
                            posted_at=item.get("created", ""),
                            raw_payload=json.dumps(item, sort_keys=True),
                        )


class BraveProvider(DiscoveryProvider):
    name = "brave"

    def __init__(self, api_key: str, pages: int = 1, client=None):
        super().__init__(client)
        self.api_key = api_key
        self.pages = max(1, min(pages, 10))

    def fetch(self) -> Iterable[JobInput]:
        titles = (
            "senior data engineer", "data engineer", "lead data engineer", "analytics engineer",
            "senior data analyst", "data analyst", "senior systems analyst", "systems analyst",
            "business systems analyst", "business intelligence analyst", "business intelligence engineer",
            "data platform engineer", "data integration engineer", "Databricks engineer", "data architect",
            "data warehouse engineer", "BI developer", "Power BI developer", "ETL developer",
            "reporting analyst", "data governance analyst", "data quality analyst", "SQL developer",
            "database analyst", "technical systems analyst", "data operations analyst",
            "data visualization analyst", "reporting developer", "cloud data engineer",
        )
        markets = (
            ("remote Canada jobs", "CA", "Remote Canada"),
            ("Ottawa Ontario jobs", "CA", "Ottawa-Gatineau, Canada"),
            ("Gatineau Quebec jobs", "CA", "Ottawa-Gatineau, Canada"),
            ("remote United States jobs salary", "US", "Remote United States"),
        )
        searches = [
            ('"{}" {}'.format(title, suffix), country, location)
            for title in titles
            for suffix, country, location in markets
        ]
        for host in (
            "jobs.lever.co", "job-boards.greenhouse.io", "jobs.ashbyhq.com", "apply.workable.com",
            "jobs.smartrecruiters.com", "myworkdayjobs.com", "careers.icims.com", "jobs.jobvite.com",
            "breezy.hr", "recruitee.com", "bamboohr.com", "teamtailor.com",
        ):
            searches.extend((
                ('("data engineer" OR "analytics engineer" OR "data analyst") remote Canada site:{}'.format(host), "CA", "Remote Canada"),
                ('("data engineer" OR "analytics engineer" OR "data analyst") remote "United States" site:{}'.format(host), "US", "Remote United States"),
            ))
        for query, country, location_hint in searches:
            for offset in range(self.pages):
                response = self.client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={
                        "q": query, "freshness": "pm", "count": 20, "offset": offset,
                        "country": country, "extra_snippets": "true",
                    },
                    headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("web", {}).get("results", []):
                    url = item.get("url", "")
                    snippets = " ".join([item.get("description", "")] + item.get("extra_snippets", []))
                    compensation = infer_compensation(snippets, "USD" if country == "US" else "CAD")
                    if compensation:
                        compensation = replace(
                            compensation,
                            is_estimated=True,
                            estimate_source="search snippet",
                            estimate_confidence="low",
                        )
                    organization = item.get("organization") or {}
                    company = organization.get("name") or "Employer to verify"
                    yield JobInput(
                        title=item.get("title", "").split(" - ")[0],
                        company=company,
                        location=location_hint,
                        url=url,
                        canonical_url="",
                        description=strip_html(snippets),
                        source=self.name,
                        external_id=url,
                        workplace_type="remote" if re.search(r"\bremote\b", snippets, re.I) else "unspecified",
                        employment_type="full-time",
                        country=country,
                        compensation=compensation,
                        raw_payload=json.dumps(item, sort_keys=True),
                    )
                if not payload.get("query", {}).get("more_results_available", False):
                    break


class LinkedInLinkProvider(DiscoveryProvider):
    """Collect public search-index links; never request a LinkedIn page."""

    name = "linkedin"
    SEARCHES = (
        ('"systems analyst" "Ottawa"', "Ottawa, Canada"),
        ('"systems analyst" "Gatineau"', "Gatineau, Canada"),
        ('"business systems analyst" "Ottawa"', "Ottawa, Canada"),
        ('"data analyst" "remote" "Canada"', "Remote Canada"),
        ('"senior data analyst" "remote" "Canada"', "Remote Canada"),
        ('"data engineer" "remote" "Canada"', "Remote Canada"),
        ('"analytics engineer" "remote" "Canada"', "Remote Canada"),
        ('"business intelligence analyst" "remote" "Canada"', "Remote Canada"),
        ('"energy" "data analyst" "remote" "Canada"', "Remote Canada"),
        ('"renewable" "systems analyst" "Ottawa"', "Ottawa, Canada"),
    )

    def __init__(self, api_key: str, pages: int = 1, client=None):
        super().__init__(client)
        self.api_key = api_key
        self.pages = max(1, min(int(pages), 3))

    def fetch(self) -> Iterable[JobInput]:
        seen = set()
        for search, _location_hint in self.SEARCHES:
            for offset in range(self.pages):
                response = self.client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": "site:linkedin.com/jobs/view/ " + search,
                            "freshness": "pm", "count": 20, "offset": offset, "country": "CA"},
                    headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("web", {}).get("results", []):
                    parsed = urlparse(item.get("url", ""))
                    host = (parsed.hostname or "").lower()
                    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
                        continue
                    match = re.search(r"/jobs/view/.*?(\d{8,})/?$", parsed.path)
                    if not match or match.group(1) in seen:
                        continue
                    raw_title = strip_html(item.get("title", ""))
                    title, company, location = _linkedin_result_fields(raw_title, "Canada")
                    if not title or not company:
                        continue
                    snippet = strip_html(item.get("description", ""))
                    workplace = _workplace_from_text(raw_title + " " + snippet)
                    if not re.search(r"\b(?:ottawa|gatineau|kanata|nepean)\b", location, re.I) and workplace != "remote":
                        continue
                    seen.add(match.group(1))
                    # A search query is not evidence of a job's workplace type.
                    # Leave it unverified until the candidate opens the listing.
                    yield JobInput(
                        title=title, company=company, location=location,
                        url="https://www.linkedin.com/jobs/view/{}/".format(match.group(1)),
                        description=snippet, source=self.name, external_id=match.group(1),
                        workplace_type=workplace,
                        employment_type="contract" if re.search(r"\b(contract|temporary|part[- ]time)\b", snippet, re.I) else "full-time",
                        country="CA",
                        raw_payload=json.dumps(item, sort_keys=True),
                    )
                if not payload.get("query", {}).get("more_results_available", False):
                    break


def _linkedin_result_fields(raw_title: str, fallback_location: str):
    text = re.sub(r"\s*\|\s*LinkedIn(?: Jobs)?\s*$", "", raw_title, flags=re.I).strip()
    hiring = re.match(r"(.+?) hiring (.+?) in (.+)$", text, re.I)
    if hiring:
        return hiring.group(2).strip(), hiring.group(1).strip(), hiring.group(3).strip()
    at = re.match(r"(.+?) at (.+?)(?:\s+[—–-]\s+(.+))?$", text, re.I)
    if at:
        return at.group(1).strip(), at.group(2).strip(), (at.group(3) or fallback_location).strip()
    return "", "", fallback_location


class GreenhouseProvider(DiscoveryProvider):
    name = "greenhouse"

    def __init__(self, boards: Dict[str, str], client=None, prefilter: Optional[bool] = None):
        super().__init__(client)
        self.boards = boards
        self.board_results: List[Dict] = []
        self.workers = 1 if client is not None else max(1, min(_env_int("JOBFLOW_GREENHOUSE_WORKERS", 8), 16))
        self.prefilter = (
            os.environ.get("JOBFLOW_GREENHOUSE_PREFILTER", "false").lower()
            in {"1", "true", "yes", "on"}
            if prefilter is None else prefilter
        )

    @staticmethod
    def _index_match(item: Dict) -> bool:
        """Cheap title gate; full posting evidence determines geography."""
        title = str(item.get("title") or "")
        location = str((item.get("location") or {}).get("name") or "")
        return _target_index_match(title, location)

    @staticmethod
    def _job(company: str, item: Dict) -> JobInput:
        description = strip_html(item.get("content", ""))
        ranges = item.get("pay_input_ranges") or []
        compensation = None
        if ranges:
            salary = ranges[0]
            compensation = Compensation(
                salary.get("min_cents", 0) / 100,
                salary.get("max_cents", 0) / 100,
                salary.get("currency_type", ""),
            )
        url = item.get("absolute_url", "")
        location = str((item.get("location") or {}).get("name") or "")
        workplace = _workplace_from_text(location)
        if workplace == "unspecified":
            workplace = description_workplace(description).mode
        country = _country_code_from_location(location, "")
        return JobInput(
            item.get("title", ""), company, location, url, description,
            "greenhouse", str(item.get("id", "")), url,
            workplace, "full-time", country, "", "unknown", "", item.get("first_published") or "",
            item.get("application_deadline") or "", compensation, json.dumps(item, sort_keys=True),
        )

    def fetch(self) -> Iterable[JobInput]:
        self.board_results = []

        def fetch_board(company, token):
            result = {"company": company, "board_token": token, "state": "completed",
                      "raw_count": 0, "index_count": 0, "emitted_count": 0, "error": ""}
            jobs = []
            try:
                response = self.client.get(
                    "https://boards-api.greenhouse.io/v1/boards/{}/jobs".format(quote(token)),
                    # Fetch descriptions once per board. Location labels alone
                    # can hide remote eligibility; avoid a detail call per job.
                    params={"content": "true", "pay_transparency": "true"},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list):
                    raise TypeError('Greenhouse board response has no jobs list')
                summaries = payload['jobs']
                result["raw_count"] = len(summaries)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                # A renamed or retired company board must not prevent all of
                # the other known employer feeds from being searched.
                result["state"] = "failed"
                result["error"] = type(exc).__name__
                return result, jobs
            for summary in summaries:
                item = summary
                if self.prefilter:
                    if not self._index_match(summary):
                        continue
                    result["index_count"] += 1
                    try:
                        if summary.get("content"):
                            item = summary
                        else:
                            detail = self.client.get(
                                "https://boards-api.greenhouse.io/v1/boards/{}/jobs/{}".format(
                                    quote(token), quote(str(summary.get("id", "")))
                                ), params={"pay_transparency": "true"},
                            )
                            detail.raise_for_status()
                            item = detail.json()
                    except (httpx.HTTPError, ValueError, TypeError):
                        continue
                else:
                    result["index_count"] += int(self._index_match(summary))
                job = self._job(company, item)
                if self.prefilter:
                    from .eligibility import evaluate
                    if evaluate(job, ignore_salary=settings()['ignore_salary']).queue == "rejected":
                        continue
                result["emitted_count"] += 1
                jobs.append(job)
            return result, jobs

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(fetch_board, company, token) for company, token in self.boards.items()]
            for future in as_completed(futures):
                result, jobs = future.result()
                self.board_results.append(result)
                yield from jobs


class LeverProvider(DiscoveryProvider):
    name = "lever"

    def __init__(self, sites: Dict[str, str], client=None, prefilter: Optional[bool] = None):
        super().__init__(client)
        self.sites = sites
        self.board_results: List[Dict] = []
        self.workers = 1 if client is not None else max(1, min(_env_int("JOBFLOW_LEVER_WORKERS", 8), 16))
        self.prefilter = (
            os.environ.get("JOBFLOW_LEVER_PREFILTER", "false").lower()
            in {"1", "true", "yes", "on"}
            if prefilter is None else prefilter
        )

    @staticmethod
    def _description(item: Dict) -> str:
        sections = [item.get("descriptionPlain") or "", item.get("additionalPlain") or ""]
        for block in item.get("lists") or []:
            sections.extend((block.get("text") or "", strip_html(block.get("content") or "")))
        return "\n\n".join(section.strip() for section in sections if section and section.strip())

    @classmethod
    def _job(cls, company: str, item: Dict) -> JobInput:
        categories = item.get("categories") or {}
        title = str(item.get("text") or "")
        location = str(categories.get("location") or "")
        description = cls._description(item)
        salary = item.get("salaryRange") or {}
        country = _country_code_from_location("{} {}".format(title, location), "")
        compensation = None
        if salary and (salary.get("min") is not None or salary.get("max") is not None):
            compensation = Compensation(
                salary.get("min"), salary.get("max"), salary.get("currency") or "",
                salary.get("interval") or "year",
            )
        compensation = compensation or infer_compensation(
            "{} {}".format(item.get("salaryDescriptionPlain") or "", description),
            "USD" if country == "US" else "CAD",
        )
        url = str(item.get("hostedUrl") or item.get("applyUrl") or "")
        workplace = _workplace_from_text(location, str(item.get("workplaceType") or "unspecified").lower())
        return JobInput(
            title, company, location, url, description, cls.name, str(item.get("id") or ""), url,
            workplace, normalize_employment_type(categories.get("commitment")), country, "", "unknown", "",
            "", "", compensation, json.dumps(item, sort_keys=True),
        )

    def fetch(self) -> Iterable[JobInput]:
        self.board_results = []

        def fetch_board(company, site):
            result = {"company": company, "board_token": site, "state": "completed",
                      "raw_count": 0, "index_count": 0, "emitted_count": 0, "error": ""}
            jobs = []
            try:
                response = self.client.get(
                    "https://api.lever.co/v0/postings/{}".format(quote(site)), params={"mode": "json"}
                )
                response.raise_for_status()
                items = response.json()
                if not isinstance(items, list):
                    raise TypeError("Lever board response is not a list")
                result["raw_count"] = len(items)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                result["state"] = "failed"
                result["error"] = type(exc).__name__
                return result, jobs
            for item in items:
                title = str(item.get("text") or "")
                location = str((item.get("categories") or {}).get("location") or "")
                matched = _target_index_match(title, location)
                result["index_count"] += int(matched)
                if self.prefilter and not matched:
                    continue
                job = self._job(company, item)
                if self.prefilter:
                    from .eligibility import evaluate
                    if evaluate(job, ignore_salary=settings()['ignore_salary']).queue == "rejected":
                        continue
                result["emitted_count"] += 1
                jobs.append(job)
            return result, jobs

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(fetch_board, company, site) for company, site in self.sites.items()]
            for future in as_completed(futures):
                result, jobs = future.result()
                self.board_results.append(result)
                yield from jobs


class AshbyProvider(DiscoveryProvider):
    name = "ashby"

    def __init__(self, boards: Dict[str, str], client=None, prefilter: Optional[bool] = None):
        super().__init__(client)
        self.boards = boards
        self.board_results: List[Dict] = []
        self.prefilter = (
            os.environ.get("JOBFLOW_ASHBY_PREFILTER", "false").lower()
            in {"1", "true", "yes", "on"}
            if prefilter is None else prefilter
        )

    def fetch(self) -> Iterable[JobInput]:
        self.board_results = []
        def fetch_board(company, board):
            result = {"company": company, "board_token": board, "state": "completed",
                      "raw_count": 0, "index_count": 0, "emitted_count": 0, "error": ""}
            try:
                response = self.client.get(
                    "https://api.ashbyhq.com/posting-api/job-board/{}".format(quote(board)),
                    params={"includeCompensation": "true"},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError('Ashby board response is not an object')
                items = payload.get("jobs", [])
                if not isinstance(items, list):
                    raise TypeError("Ashby board response is not a list")
                result['raw_count'] = len(items)
                return result, items
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                result['state'] = 'failed'
                result['error'] = ('HTTP ' + str(exc.response.status_code)
                                   if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__)
                return result, []

        workers = max(1, min(_env_int("JOBFLOW_ASHBY_WORKERS", 12), 24, len(self.boards) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(fetch_board, company, board)
                for company, board in self.boards.items()
            ]
            for future in as_completed(futures):
                result, items = future.result()
                company = result['company']
                self.board_results.append(result)
                for item in items:
                    if item.get('isListed') is False:
                        continue
                    description = strip_html(item.get("descriptionHtml") or item.get("descriptionPlain") or "")
                    location = item.get("location") or ""
                    title = item.get("title") or ""
                    # Prefer explicit location data over branded labels such as
                    # "KOHO (CAN)". Include secondary offices for multi-location roles.
                    locations = [item] + (item.get('secondaryLocations') or [])
                    countries = []
                    for place in locations:
                        if not isinstance(place, dict):
                            continue
                        address = place.get('address') or {}
                        if not isinstance(address, dict):
                            continue
                        postal = address.get('postalAddress') or {}
                        if not isinstance(postal, dict):
                            continue
                        value = postal.get('addressCountry') or ''
                        if isinstance(value, str) and value:
                            code = {'CA': 'CA', 'CAN': 'CA', 'CANADA': 'CA', 'US': 'US',
                                    'USA': 'US', 'UNITED STATES': 'US'}.get(value.upper(), value.upper())
                            countries.append(code)
                    secondary = [p['location'] for p in locations[1:]
                                 if isinstance(p, dict) and isinstance(p.get('location'), str) and p['location']]
                    location = '; '.join(dict.fromkeys([location] + secondary))
                    country = ('CA' if 'CA' in countries else 'US' if 'US' in countries else
                               countries[0] if countries else _country_code_from_location(location, ''))
                    # Preserve explicit eligible countries in the human-readable
                    # label so downstream multi-country validation agrees.
                    if country in {'CA', 'US'}:
                        name = 'Canada' if country == 'CA' else 'United States'
                        if name.casefold() not in location.casefold():
                            location += '; ' + name
                    compensation = _ashby_compensation(item) or infer_compensation(
                        json.dumps(item.get("compensation") or item.get("compensationTierSummary") or ""),
                        "USD" if country == "US" else "CAD",
                    ) or infer_compensation(description, "USD" if country == "US" else "CAD")
                    url = item.get("jobUrl") or item.get("applyUrl") or ""
                    job = JobInput(
                        title, company, location, url, description,
                        self.name, item.get("id"), url, item.get("workplaceType") or ('remote' if item.get('isRemote') else 'unspecified'),
                        normalize_employment_type(item.get("employmentType")), country, "", "unknown", "",
                        item.get("publishedAt") or "",
                        "", compensation, json.dumps(item, sort_keys=True),
                    )
                    matched = _target_index_match(title, location)
                    result['index_count'] += int(matched)
                    if self.prefilter:
                        if not matched:
                            continue
                        from .eligibility import evaluate
                        if evaluate(job, ignore_salary=settings()['ignore_salary']).queue == "rejected":
                            continue
                    result['emitted_count'] += 1
                    yield job


def _target_index_match(title: str, location: str) -> bool:
    """Cheap title gate only; full evidence, not display labels, gates geography."""
    return relevant_title(title) and not excluded_title(title)


def _ashby_compensation(item) -> Optional[Compensation]:
    """Read Ashby's structured salary component without guessing its currency."""
    compensation = item.get("compensation") or {}
    components = compensation.get("summaryComponents") or []
    for component in components:
        if str(component.get("compensationType") or "").lower() != "salary":
            continue
        minimum, maximum = component.get("minValue"), component.get("maxValue")
        if minimum is None and maximum is None:
            continue
        interval_text = str(component.get("interval") or "year").lower()
        interval = next((value for value in ("hour", "day", "week", "month", "year") if value in interval_text), "year")
        return Compensation(minimum, maximum, component.get("currencyCode") or "", interval)
    return None


def _mapping(raw: str) -> Dict[str, str]:
    result = {}
    for value in filter(None, (part.strip() for part in raw.split(","))):
        company, _, key = value.partition(":")
        result[company.strip()] = (key or company).strip()
    return result


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _row_text(row, key: str) -> str:
    try:
        return str(row[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def inferred_ats_boards(known_jobs: Iterable) -> Dict[str, Dict[str, str]]:
    """Infer public employer board identifiers from previously discovered URLs."""
    boards: Dict[str, Dict[str, str]] = {"greenhouse": {}, "lever": {}, "ashby": {}}
    unknown_companies = {"", "Employer to verify", "Unknown employer"}
    for row in known_jobs:
        company = _row_text(row, "company").strip()
        for field in ("canonical_url", "url"):
            parsed = urlparse(_row_text(row, field))
            host = (parsed.hostname or "").lower()
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if not parts:
                continue
            source = ""
            if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
                source = "greenhouse"
            elif host == "jobs.lever.co":
                source = "lever"
            elif host == "jobs.ashbyhq.com":
                source = "ashby"
            if not source:
                continue
            token = parts[0]
            if token.lower() in ATS_AGGREGATOR_BOARDS.get(source, set()):
                continue
            label = company if company not in unknown_companies else token.replace("-", " ").replace(".", " ").title()
            if label in boards[source] and boards[source][label].lower() != token.lower():
                label = "{} ({})".format(label, token)
            boards[source][label] = token
    return boards


def _enabled_sources() -> Optional[set]:
    raw = os.environ.get("JOBFLOW_DISCOVERY_SOURCES", "").strip()
    if not raw:
        return None
    return {value.strip().lower() for value in raw.split(",") if value.strip()}


def _ats_registry(path_value: str) -> Dict[str, str]:
    """Load company/board mappings from a reviewable CSV registry."""
    if not path_value.strip():
        return {}
    path = Path(path_value).expanduser()
    if not path.is_file():
        return {}
    mappings: Dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            company = (row.get("company") or row.get("board_token") or "").strip()
            token = (row.get("board_token") or "").strip()
            if company and token:
                label = company if company not in mappings else "{} ({})".format(company, token)
                mappings[label] = token
    return mappings


def configured_providers(client=None, known_jobs: Iterable = ()) -> List[DiscoveryProvider]:
    providers: List[DiscoveryProvider] = []
    enabled = _enabled_sources()
    inferred = inferred_ats_boards(known_jobs)

    def selected(name: str) -> bool:
        return enabled is None or name in enabled

    if selected("adzuna") and os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
        providers.append(AdzunaProvider(
            os.environ["ADZUNA_APP_ID"], os.environ["ADZUNA_APP_KEY"],
            pages=_env_int("JOBFLOW_ADZUNA_PAGES", 1),
            max_days_old=_env_int("JOBFLOW_MAX_DAYS_OLD", 30), client=client,
        ))
    if selected("brave") and os.environ.get("BRAVE_SEARCH_API_KEY"):
        providers.append(BraveProvider(
            os.environ["BRAVE_SEARCH_API_KEY"],
            pages=_env_int("JOBFLOW_BRAVE_PAGES", 1),
            client=client,
        ))
    if selected("linkedin") and os.environ.get("BRAVE_SEARCH_API_KEY"):
        providers.append(LinkedInLinkProvider(
            os.environ["BRAVE_SEARCH_API_KEY"],
            pages=_env_int("JOBFLOW_LINKEDIN_PAGES", 1), client=client,
        ))
    registry = _ats_registry(os.environ.get("JOBFLOW_GREENHOUSE_REGISTRY", ""))
    explicit_greenhouse = _mapping(os.environ.get("JOBFLOW_GREENHOUSE_BOARDS", ""))
    # An explicit registry is an allow-list. Mixing every board inferred from
    # historical URLs back into it makes a "top N" tier impossible to audit.
    greenhouse = {} if registry or explicit_greenhouse else dict(inferred["greenhouse"])
    greenhouse.update(registry)
    greenhouse.update(explicit_greenhouse)
    lever_registry = _ats_registry(os.environ.get("JOBFLOW_LEVER_REGISTRY", ""))
    explicit_lever = _mapping(os.environ.get("JOBFLOW_LEVER_SITES", ""))
    lever = {} if lever_registry or explicit_lever else dict(inferred["lever"])
    lever.update(lever_registry)
    lever.update(explicit_lever)
    ashby_registry = _ats_registry(os.environ.get("JOBFLOW_ASHBY_REGISTRY", ""))
    explicit_ashby = _mapping(os.environ.get("JOBFLOW_ASHBY_BOARDS", ""))
    ashby = {} if ashby_registry or explicit_ashby else dict(inferred["ashby"])
    ashby.update(ashby_registry)
    ashby.update(explicit_ashby)
    if selected("greenhouse") and greenhouse:
        providers.append(GreenhouseProvider(greenhouse, client))
    if selected("lever") and lever:
        providers.append(LeverProvider(lever, client))
    if selected("ashby") and ashby:
        providers.append(AshbyProvider(ashby, client))
    if selected('workday'):
        from .workday import WorkdayProvider, load_registry
        workday = load_registry(os.environ.get('JOBFLOW_WORKDAY_REGISTRY', 'config/workday-boards.csv'))
        if workday:
            providers.append(WorkdayProvider(workday, client,
                max_pages=_env_int('JOBFLOW_WORKDAY_MAX_PAGES', 100),
                workers=_env_int('JOBFLOW_WORKDAY_WORKERS', 4)))
    if selected('smartrecruiters'):
        from .smartrecruiters import SmartRecruitersProvider
        boards = _ats_registry(os.environ.get('JOBFLOW_SMARTRECRUITERS_REGISTRY', 'config/smartrecruiters-boards.csv'))
        if boards:
            providers.append(SmartRecruitersProvider(boards, client,
                max_pages=_env_int('JOBFLOW_SMARTRECRUITERS_MAX_PAGES', 100),
                workers=_env_int('JOBFLOW_SMARTRECRUITERS_WORKERS', 4)))
    return providers


def _job_posting(text: str):
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text,
        flags=re.I | re.S,
    )
    for block in blocks:
        try:
            # Script JSON is raw text, not an HTML attribute. Decoding entities
            # first turns valid description &quot; into unescaped JSON quotes.
            try:
                data = json.loads(block.strip())
            except ValueError:
                data = json.loads(html.unescape(block).strip())
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for candidate in candidates:
            kinds = candidate.get("@type", []) if isinstance(candidate, dict) else []
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if isinstance(candidate, dict) and "JobPosting" in kinds:
                return candidate
    return None


def _safe_direct_candidate(url: str) -> bool:
    return public_http_url(url) and not prohibited_automation_url(url) and not aggregator_url(url)


def _workplace_from_text(text: str, fallback: str = "unspecified") -> str:
    # Hybrid descriptions frequently also say "remote" (for example,
    # "3 days in office / 2 days remote"), so the stricter signal wins.
    if re.search(r"\bhybrid\b", text, re.I):
        return "hybrid"
    if re.search(r"\b(on[- ]?site|in[- ]office|office[- ]based)\b", text, re.I):
        return "on-site"
    if re.search(r"\b(remote(?:-first)?|telecommut(?:e|ing)|work(?:ing)? from home|home[- ]based)\b", text, re.I):
        return "remote"
    return fallback


def _sponsorship_from_text(text: str, fallback: str = "unknown") -> str:
    if re.search(r"\b(?:no|without) (?:visa |employment )?sponsorship|(?:unable|will not|does not) sponsor\b", text, re.I):
        return "not available"
    if re.search(r"\b(?:visa |employment )?sponsorship (?:is )?(?:available|provided)|\bcan sponsor\b", text, re.I):
        return "available"
    return fallback


def _looks_like_individual_job(url: str, text: str) -> bool:
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title_text = strip_html(title.group(1)) if title else ""
    if re.search(r"\b(salary guide|jobs at|job search options|remote jobs|careers home)\b", title_text, re.I):
        return False
    path = urlparse(url).path.lower()
    job_path = bool(re.search(r"/(?:job|jobs|position|positions|careers)/(?:[^/]+/)*[a-z0-9_-]{4,}", path))
    apply_signal = bool(re.search(r"\bapply(?: now| for this job| for this position)?\b", strip_html(text), re.I))
    return direct_ats_url(url) or (job_path and apply_signal)


def _outbound_job_links(text: str, base_url: str, posting=None) -> List[str]:
    """Return likely original employer/ATS links from a permitted aggregator page."""
    weighted = []
    if isinstance(posting, dict) and posting.get("url"):
        weighted.append((100, str(posting["url"])))
    canonical = re.search(
        r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)', text, re.I
    )
    if canonical:
        weighted.append((35, canonical.group(1)))
    for match in re.finditer(r'<a\b([^>]*)href=["\']([^"\']+)["\']([^>]*)>(.*?)</a>', text, re.I | re.S):
        attrs = "{} {}".format(match.group(1), match.group(3))
        label = strip_html(match.group(4)) + " " + attrs
        score = 0
        if re.search(r"\bapply(?: now)?\b|original (?:job|posting)|employer (?:site|website)", label, re.I):
            score += 50
        candidate = urljoin(base_url, html.unescape(match.group(2)))
        if direct_ats_url(candidate):
            score += 40
        if score:
            weighted.append((score, candidate))
    found, seen = [], set()
    for _, candidate in sorted(weighted, key=lambda item: item[0], reverse=True):
        candidate = urljoin(base_url, candidate)
        if candidate not in seen and _safe_direct_candidate(candidate):
            seen.add(candidate)
            found.append(candidate)
    return found[:8]


def resolve_canonical_job(job: JobInput, client: Optional[httpx.Client] = None) -> JobInput:
    """Resolve an indexed result to a public employer/ATS posting.

    Restricted boards are retained as user-assisted links and are never fetched.
    Permitted aggregator pages may be read only to locate their outbound original
    posting, which is validated before it becomes the canonical application URL.
    """
    if job.source not in {"brave", "adzuna"}:
        return job
    if not public_http_url(job.canonical_url) or prohibited_automation_url(job.canonical_url):
        return replace(job, canonical_url="")
    client = client or httpx.Client(timeout=20, follow_redirects=True)
    response = client.get(job.canonical_url, headers={"User-Agent": "JobFlowLocal/0.3"})
    response.raise_for_status()
    response_url = str(response.url)
    if not public_http_url(response_url) or prohibited_automation_url(response_url):
        return replace(job, canonical_url="")
    posting = _job_posting(response.text)
    if aggregator_url(response_url):
        direct_response = None
        for candidate in _outbound_job_links(response.text, response_url, posting):
            try:
                attempted = client.get(candidate, headers={"User-Agent": "JobFlowLocal/0.3"})
                attempted.raise_for_status()
            except httpx.HTTPError:
                continue
            target = str(attempted.url)
            if not _safe_direct_candidate(target):
                continue
            target_posting = _job_posting(attempted.text)
            page_text = strip_html(attempted.text)
            if not target_posting and not (
                len(page_text) >= 300
                and re.search(r"\b(apply|application|job description|responsibilities|qualifications)\b", page_text, re.I)
            ):
                continue
            response, response_url, posting, direct_response = attempted, target, target_posting, attempted
            break
        if direct_response is None:
            return replace(job, canonical_url="")
    if not _safe_direct_candidate(response_url):
        return replace(job, canonical_url="")
    if not posting:
        page_text = strip_html(response.text)
        if len(page_text) < 300 or not _looks_like_individual_job(response_url, response.text):
            return replace(job, canonical_url="")
        resolved_country = _country_code_from_location(page_text, job.country) or job.country
        fallback_compensation = job.compensation if job.source == "adzuna" or (
            job.compensation and job.compensation.is_estimated
        ) else None
        return replace(
            job,
            canonical_url=response_url,
            description=page_text or job.description,
            country=resolved_country,
            workplace_type=_workplace_from_text(page_text, job.workplace_type),
            sponsorship=_sponsorship_from_text(page_text, job.sponsorship),
            compensation=infer_compensation(page_text, "USD" if resolved_country == "US" else "CAD") or fallback_compensation,
        )
    employer = posting.get("hiringOrganization") or {}
    location = posting.get("jobLocation") or posting.get("applicantLocationRequirements") or job.location
    if isinstance(location, list):
        location = ", ".join(_location_text(item) for item in location)
    elif isinstance(location, dict):
        location = _location_text(location)
    location = str(location or job.location)
    resolved_country = _country_code_from_location(location, job.country) or job.country
    description = strip_html(posting.get("description", "")) or job.description
    fallback_compensation = job.compensation if job.source == "adzuna" or (
        job.compensation and job.compensation.is_estimated
    ) else None
    compensation = _jsonld_compensation(posting.get("baseSalary")) or infer_compensation(
        description, "USD" if resolved_country == "US" else "CAD"
    ) or fallback_compensation
    return replace(
        job,
        title=posting.get("title") or job.title,
        company=employer.get("name") or job.company,
        location=location,
        description=description,
        canonical_url=response_url,
        workplace_type="remote" if posting.get("jobLocationType") == "TELECOMMUTE" or re.search(
            r"\bremote\b", location, re.I
        ) else _workplace_from_text(
            description, job.workplace_type
        ),
        employment_type=_employment_text(posting.get("employmentType")) or job.employment_type,
        country=resolved_country,
        sponsorship=_sponsorship_from_text(description, job.sponsorship),
        posted_at=posting.get("datePosted", "") or job.posted_at,
        closes_at=posting.get("validThrough", "") or job.closes_at,
        compensation=compensation,
        raw_payload=json.dumps(posting, sort_keys=True),
    )


def _location_text(value) -> str:
    if not isinstance(value, dict):
        return str(value or "")
    address = value.get("address", value)
    if not isinstance(address, dict):
        return str(address or "")
    rendered = ", ".join(
        str(address.get(key, "")) for key in ("addressLocality", "addressRegion", "addressCountry") if address.get(key)
    )
    return rendered or str(value.get("name", ""))


def _country_code_from_location(value: str, fallback: str = "") -> str:
    text = value.strip()
    mappings = (
        ("CA", r"\bCanada\b"),
        (
            "US",
            r"\b(?:United States|USA|U\.S\.|US|Florida|Miami|Fort Lauderdale|Tampa|Orlando|"
            r"Jacksonville|Tallahassee|West Palm Beach|Boca Raton|St\.? Petersburg|"
            r"Washington State|Seattle|Bellevue|Redmond|Tacoma|Kirkland)\b|"
            r"(?:^|,\s*)(?:FL|WA)(?:\s*,|\s*$)",
        ),
        ("OTHER", r"\b(?:Europe|EMEA|LATAM|Spain|Ukraine|Poland|Romania)\b"),
        ("MX", r"\bMexico\b"),
        ("IN", r"\bIndia\b"),
        ("GB", r"\b(?:United Kingdom|UK|England|Scotland|Wales)\b"),
        ("AE", r"\b(?:United Arab Emirates|UAE)\b"),
    )
    for code, pattern in mappings:
        if re.search(pattern, text, re.I):
            return code
    if re.fullmatch(r"CA|CAN|US|USA|MX|IN|GB|AE", text, re.I):
        return {"CAN": "CA", "USA": "US"}.get(text.upper(), text.upper())
    trailing = re.search(r"(?:,|\||\bremote)\s*(US|USA|CAN|MX|GB|AE)\s*$", text, re.I)
    if trailing:
        return {"CAN": "CA", "USA": "US"}.get(trailing.group(1).upper(), trailing.group(1).upper())
    if re.search(r",\s*IN\s*$", text, re.I) and fallback.upper() != "US":
        return "IN"
    return ""


def _employment_text(value) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return str(value or "").replace("_", "-").lower()


def _jsonld_compensation(value) -> Optional[Compensation]:
    if not isinstance(value, dict):
        return None
    currency = value.get("currency", "")
    data = value.get("value") or {}
    if not isinstance(data, dict):
        return None
    minimum = data.get("minValue") if data.get("minValue") is not None else data.get("value")
    maximum = data.get("maxValue") if data.get("maxValue") is not None else data.get("value")
    unit = str(data.get("unitText", "year")).lower()
    interval = "hour" if "hour" in unit else "year"
    return Compensation(minimum, maximum, currency, interval) if minimum is not None or maximum is not None else None
