"""Deterministic job eligibility gates and transparent ranking."""

import re
from datetime import datetime, timezone
from typing import Dict, Iterable, Tuple

from .models import EligibilityDecision, JobInput, MatchScore
from .policy import local_office, us_office, settings
from .titles import relevant_title, excluded_title, systems_analyst_title, title_review_reason, management_filter_title, software_role_exclusion, project_role_exclusion, product_role_exclusion
from .workplace import description_workplace


RESPONSIBILITY_TERMS = (
    "design",
    "build",
    "pipeline",
    "ingestion",
    "transform",
    "validation",
    "deduplication",
    "stakeholder",
    "production",
    "reporting",
    "analytics",
)

# Canonical JD skills and common wording variants. The supported set is
# deliberately limited to claims already present in the base resume.
SKILL_ALIASES = (
    ("Python", ("python",)),
    ("SQL", ("sql",)),
    ("PySpark", ("pyspark",)),
    ("Apache Spark", ("apache spark", "spark sql")),
    ("Databricks", ("databricks",)),
    ("Delta Lake", ("delta lake",)),
    ("Power BI", ("power bi",)),
    ("Alteryx", ("alteryx",)),
    ("ETL/ELT", ("etl", "elt")),
    ("REST APIs", ("rest api", "restful api")),
    ("SOAP APIs", ("soap api",)),
    ("Data modelling", ("data modeling", "data modelling", "dimensional modeling", "dimensional modelling")),
    ("Data quality", ("data quality",)),
    ("Lakehouse", ("lakehouse", "medallion architecture")),
    ("Reconciliation", ("reconciliation",)),
    ("Settlements", ("settlements",)),
    ("Market data", ("market data",)),
    ("AWS", ("aws", "amazon web services")),
    ("Azure", ("azure",)),
    ("Google Cloud", ("gcp", "google cloud")),
    ("Snowflake", ("snowflake",)),
    ("dbt", ("dbt",)),
    ("Airflow", ("airflow",)),
    ("Kafka", ("kafka",)),
    ("Tableau", ("tableau",)),
    ("Looker", ("looker",)),
    ("Java", ("java",)),
    ("Scala", ("scala",)),
    ("R", ("r programming", "language r")),
    ("Terraform", ("terraform",)),
    ("Kubernetes", ("kubernetes",)),
    ("Docker", ("docker",)),
    ("CI/CD", ("ci/cd", "continuous integration", "continuous delivery")),
    ("Machine learning", ("machine learning",)),
    ("Microsoft Fabric", ("microsoft fabric",)),
    ("Azure Synapse", ("azure synapse", "synapse analytics")),
    ("Informatica", ("informatica",)),
)

REQUIRED_MARKERS = ("required", "must", "minimum", "need", "proficient", "strong experience")
PREFERRED_MARKERS = ("preferred", "nice to have", "bonus", "asset", "desirable")


def _contains(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w){}(?!\w)".format(re.escape(phrase)), text, re.I))


def _contains_any(text: str, phrases: Iterable[str]) -> bool:
    return any(_contains(text, phrase) for phrase in phrases)


def _minimum_experience_years(text: str) -> Tuple[int, ...]:
    """Extract explicit minimum years tied directly to experience."""
    values = []
    # Also recognize qualifications such as "eight years building pipelines".
    # Do not interpret preferred experience or company age as a hard minimum.
    numbers = dict(zip(('one', 'two', 'three', 'four', 'five', 'six', 'seven',
                        'eight', 'nine', 'ten', 'eleven', 'twelve'), range(1, 13)))
    text = re.sub(r'\b(' + '|'.join(numbers) + r')\b(?=\s*\+?\s+years?\b)',
                  lambda m: str(numbers[m[0].lower()]), text, flags=re.I)
    patterns = (
        r"\b(\d{1,2})\s*\+?\s*(?:(?:[-–—]|to)\s*\d{1,2})?\s*years?\b[^.\n]{0,60}\bexperience\b",
        r"\bexperience\b[^.\n:]{0,35}(?:of|:)?\s*(\d{1,2})\s*\+?\s*(?:(?:[-–—]|to)\s*\d{1,2})?\s*years?\b",
        r"\b(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2})?\s*years?\s+(?:of\s+)?(?:building|developing|designing|operating|delivering|managing|working)\b",
    )
    preferred = False
    for line in text.splitlines():
        stripped = line.strip().strip(':').lower()
        if re.fullmatch(r'(?:preferred(?: qualifications| experience| skills)?|nice[- ]to[- ]haves?|bonus(?: points(?: for)?| qualifications| skills(?: & experience)?)?)', stripped):
            preferred = True
            continue
        if re.fullmatch(r'(?:required(?: qualifications| experience| skills)?|what (?:you bring|you.ll need)|qualifications|requirements|responsibilities|benefits)', stripped):
            preferred = False
        if preferred:
            continue
        for sentence in re.split(r'(?<=[.!?])\s+', line):
            if re.match(r'\s*(?:we|our (?:company|team|business))\s+(?:have|has|bring|brings)\b', sentence, re.I):
                continue
            if re.search(r'\b(?:preferred|nice[- ]to[- ]have|a bonus|an asset|not required)\b', sentence, re.I):
                continue
            for index, pattern in enumerate(patterns):
                for match in re.finditer(pattern, sentence, re.I):
                    prefix = sentence[:match.start()].strip(' \t-*•')
                    if index == 2 and prefix and not re.search(
                        r'(?:\b(?:minimum(?: of)?|at least|requires?|must have|must bring)|'
                        r'\b(?:you|candidates?|applicants?|someone)\b[^.]{0,60}(?:have|with|bring))\s*$',
                        prefix, re.I):
                        continue
                    values.append(int(match[1]))
    return tuple(values)


_US_NAME = r'(?:United States(?: of America)?|U\.?S\.?A?\.?)'


def _us_residency_required(text):
    pattern = (
        r'\b(?:must|are required to|is required to)\s+(?:currently\s+)?'
        r'(?:reside|live|be based|be located)(?:\s+and\s+work)?\s+in\s+(?:the\s+)?' + _US_NAME + r'(?!\w)')
    for match in re.finditer(pattern, text, re.I):
        # US-or-Canada is not a US-only residence requirement.
        tail = text[match.end():]
        if re.match(r'\s*(?:or|and/or|,)\s*(?:in\s+)?(?:the\s+)?Canada\b', tail, re.I):
            continue
        return True
    return False


def _us_citizenship_required(text):
    # Include common affirmative variants without matching "not required".
    pattern = (
        r'\b(?:requires?|must (?:have|hold))\s+(?:a\s+)?' + _US_NAME + r'\s+citizenship\b|'
        r'\b' + _US_NAME + r'\s+citizenship\s+(?:is\s+)?required\b(?!\s*:\s*no\b)|'
        r'\bmust be\s+(?:a\s+)?' + _US_NAME + r'\s+citizen\b')
    return any(not re.search(r'\b(?:not|never)\s*$', text[max(0, m.start()-20):m.start()], re.I)
               for m in re.finditer(pattern, text, re.I))


def _canadian_remote_restriction(text):
    """Explicit hiring regions, not offices, travel or workforce statistics."""
    allowed = settings().get('residence_province_aliases', [])
    if not allowed:
        return ''
    province = r'\b(?:Ontario|British Columbia|Alberta|Nova Scotia|Saskatchewan|Manitoba|New Brunswick|Newfoundland|Prince Edward Island|Quebec|Québec|ON|BC|AB|NS|SK|MB|NB|NL|PE|QC)\b'
    for line in text.splitlines():
        if re.search(r'\b(?:majority|most|some)\b', line, re.I):
            continue
        restricted = re.search(
            r'\b(?:must (?:reside|live|be based)|(?:only|exclusively) (?:hire|hiring)|'
            r'(?:only|currently)\s+(?:able to\s+)?(?:consider|accept)\s+applications?\s+from\s+candidates?\s+based\s+in|'
            r'(?:hiring|remote work) (?:is )?(?:limited|restricted) to|'
            r'(?:employees|team members).{0,60}live and work in)\b', line, re.I)
        if not restricted:
            continue
        # A description of current employees only establishes hiring scope
        # when the JD explicitly says it hires from established hub regions.
        if re.search(r'\b(?:employees|team members)\b', restricted[0], re.I) and not re.search(
            r'\bhub regions\b.{0,100}\b(?:we hire|hiring)\b', text, re.I):
            continue
        region_text = line[restricted.end():]
        provinces = re.findall(province, region_text)  # case-sensitive abbreviations, not "on"
        if provinces and not re.search(r'\b(?:' + '|'.join(re.escape(p) for p in allowed) + r'|across Canada|anywhere in Canada)\b', region_text, re.I):
            return 'Canadian remote hiring regions do not include configured residence'
    return ''


def _is_ottawa_gatineau(location: str) -> bool:
    return local_office(location)


def _is_approved_us_office_location(location: str) -> bool:
    return us_office(location)


def _country(job: JobInput) -> str:
    location = job.location
    # Board feeds often omit the structured country for these explicit
    # locations. Unknown metadata must not make foreign jobs eligible.
    if re.search(r'\b(?:China|Hong Kong|South Africa|Cape Town|Johannesburg)\b', location, re.I) and not re.search(
        r'\b(?:Canada|United States|USA|US|Worldwide|Global|North America)\b', location, re.I
    ):
        return 'OTHER'
    if re.match(r'^(?:IN|GB|DE|FR|PL|AU|SG|BR|MX|JP|KR|TW|PH|MY)-', location):
        return 'OTHER'
    if re.search(r'\b(?:Argentina|Malaysia|Johor|Kuala Lumpur|Singapore|South Korea|Seoul|France|Paris|Slovakia|Germany|Bangkok|Thailand|Taiwan|Philippines|Manila|Taguig|India|Mexico|Brazil|Europe|EMEA|LATAM|United Kingdom|Spain|Ukraine|Poland|Romania|Australia|El Salvador)\b', location, re.I) and not re.search(r'\b(?:Canada|United States|USA|Worldwide|Global|North America)\b', location, re.I):
        return 'OTHER'
    explicit = job.country.upper().strip()
    if explicit in {"CA", "CAN", "CANADA"}:
        return "CA"
    if explicit in {"US", "USA", "UNITED STATES"}:
        return "US"
    if explicit:
        return "OTHER"
    if _contains_any(job.location, ("europe", "emea", "latam", "spain", "ukraine", "poland", "romania", "india")) and not _contains_any(
        job.location, ("canada", "united states", "usa", "u.s.", "us")
    ):
        return "OTHER"
    if _contains_any(job.location, ("united states", "usa", "u.s.", "us")):
        return "US"
    text = job.location
    if _contains_any(text, ("united states", "usa", "u.s.")):
        return "US"
    # A bare "US" cannot be treated as a general keyword because it is also
    # the English pronoun "us".  Employer postings commonly use the country
    # abbreviation inside unambiguous work-eligibility and location phrases,
    # though, so recognize those phrases before defaulting an unspecified
    # remote posting to Canada.
    if re.search(
        r"\b(?:authorized|authorised|eligible) to work (?:for any employer )?in (?:the )?US\b|"
        r"\b(?:remote|based|located|reside|residing|work) (?:in|within|from) (?:the )?US\b|"
        r"\bUS[- ](?:based|only)\b|\bwithin the US\b",
        job.description if location.strip().lower() in {'remote', 'unspecified', ''} else text,
        re.I,
    ):
        return "US"
    if _contains_any(text, ("mexico", "united kingdom", "england", "scotland", "wales", "india")):
        return "OTHER"
    if re.search(r'\b(?:Canada|Ottawa|Gatineau|Kanata|Nepean|Toronto|Montr[eé]al|Vancouver|Calgary|Edmonton|Quebec|Ontario|Worldwide|Global|North America)\b|,\s*(?:ON|QC|BC|AB|NS|MB|SK|NB|NL|PE)\b', location, re.I):
        return 'CA'
    if us_office(location):
        return 'US'
    return ''


def title_filter_enabled(job):
    return not (job.source in {'linkedin', 'linkedin_employer'} and
                not settings().get('linkedin_title_filter_enabled', True))


def role_exclusion(job):
    product_reason = product_role_exclusion(job.title)
    if product_reason:
        return product_reason
    project_reason = project_role_exclusion(job.title)
    if project_reason:
        return project_reason
    software_reason = software_role_exclusion(job.title)
    if software_reason:
        return software_reason
    filter_title = title_filter_enabled(job)
    if job.company.casefold().strip() in {c.casefold() for c in settings().get('excluded_companies', [])}:
        return 'employer excluded by candidate preference'
    if (filter_title and re.search(r'\bsettlements?\b', job.title, re.I)) or re.search(
        r'\b(?:responsible for|primary responsibility.{0,20}|manage|perform|process|oversee)\s+(?:daily\s+|trade\s+|energy\s+|cash\s+)*settlements?\b', job.description, re.I):
        return 'settlements-focused role excluded by candidate preference'
    if filter_title and re.search(r'\b(?:junior|jr\.?|intern|internship|graduate|entry[- ]level|manager|director|vp|lead|gtm)\b|\bhead of\b|go[- ]to[- ]market|data scientist', management_filter_title(job.title), re.I):
        return 'excluded role or career level'
    if filter_title and excluded_title(job.title):
        return 'excluded role or career level'
    if re.search(r'\bserve as\b[^.]{0,100}\blead (?:analyst|engineer)\b', job.description, re.I):
        return 'lead role identified in job responsibilities'
    if re.search(
        r'\b(?:data foundations?|curated datasets?|data models?|analytics)\b[^.\n]{0,90}'
        r'\b(?:power|for|support)\s+(?:the\s+)?(?:GTM|go[- ]to[- ]market)\b',
        job.description, re.I):
        return 'go-to-market reporting is a core role responsibility'
    if re.search(r'\bown\b[^.\n]{0,45}\bdaily\b[^.\n]{0,40}\breconciliations?\b', job.description, re.I) and re.search(
        r'\b(?:card[- ]network|banking|payment)\b[^.\n]{0,60}\bsettlements?\b', job.description, re.I):
        return 'settlements-focused operational reconciliation role'
    if filter_title and settings().get('systems_analyst_senior_only', True) and systems_analyst_title(job.title) and not re.search(r'\b(?:senior|sr\.?)\b', job.title, re.I):
        return 'systems analyst role must be senior'
    if cybersecurity_role(job.title, job.description):
        return 'cybersecurity-related role excluded by candidate preference'
    return ''


def application_guard(job):
    """Hard preferences, including geography, cannot be waived by an eligibility override."""
    if not isinstance(job, JobInput):
        row = dict(job)
        job = JobInput(**{k: row[k] for k in ('title','company','location','url','description','country','workplace_type','source') if k in row})
    reason = role_exclusion(job)
    if reason:
        return reason
    if any(years > settings().get('maximum_required_experience', 7) for years in _minimum_experience_years(job.description)):
        return 'required experience exceeds configured maximum'
    country, workplace = _country(job), _workplace(job)
    # Recheck these restrictions at application time too. A saved job may have
    # passed an older discovery policy before these facts were recognized.
    if country == 'US':
        if _us_residency_required(job.description):
            return 'US residence is required'
        if _us_citizenship_required(job.description):
            return 'US citizenship is required'
    if country == 'CA' and workplace == 'remote':
        restriction = _canadian_remote_restriction(job.description)
        if restriction:
            return restriction
    if country == 'OTHER':
        return 'Outside approved geography'
    if not country:
        return 'Country eligibility needs evidence'
    if workplace == 'remote':
        return ''
    if (country == 'CA' and local_office(job.location)) or (country == 'US' and us_office(job.location)):
        return ''
    return 'Office location outside approved geography or remote status unverified'


def _workplace(job: JobInput) -> str:
    evidence = description_workplace(job.description)
    if evidence.conflict:
        return 'unspecified'
    explicit = job.workplace_type.lower().strip()
    if explicit in {"remote", "hybrid", "on-site", "onsite"}:
        if evidence.mode != 'unspecified' and (explicit == 'remote') != (evidence.mode == 'remote'):
            return 'unspecified'
        return "on-site" if explicit == "onsite" else explicit
    location = job.location or ""
    if _contains(location, "hybrid"):
        return "hybrid"
    if re.search(r"\b(on[- ]?site|in[- ]office|office[- ]based)\b", location, re.I):
        return "on-site"
    if re.search(r"\b(remote(?:-first)?|telecommut(?:e|ing)|home[- ]based)\b", location, re.I):
        if evidence.mode in {'hybrid', 'on-site'}:
            return 'unspecified'
        return "remote"
    return evidence.mode


def cybersecurity_role(title: str, description: str = '') -> bool:
    # Structured board descriptions distinguish the employer's business lines
    # from this vacancy's duties. Strip only an explicitly bounded company
    # introduction for role classification; retain the original description
    # for citizenship, workplace and other eligibility checks.
    description = re.sub(
        r'^\s*Company Description\s*\n.*?(?=^\s*(?:Job Description|Qualifications)\s*$)',
        '', description, flags=re.I | re.M | re.S)
    # A company-wide code-of-conduct promise is not this role's subject matter.
    description = re.sub(r'Our Trust Charter is our Code of Conduct[^.]*\.', '', description, flags=re.I)
    # A list of corporate departments is not the vacancy's function. This
    # exclusion is structural and applies to any employer; actual duties below
    # the introduction still participate in the cybersecurity check.
    description = ''.join(
        sentence for sentence in re.split(r'(?<=[.\n])', description)
        if not (re.search(r'\bfoundational teams\b', sentence, re.I)
                and re.search(r'\band more\b', sentence, re.I)))
    return bool(re.search(
        r'\b(?:cyber[ -]?security|infosec|information security|cyber defense|cyber threat|'
        r'security operations cent(?:er|re)|penetration testing|threat intelligence)\b',
        title + ' ' + description, re.I) or re.search(
        r'\b(?:security (?:analyst|engineer|architect)|soc analyst|incident response|cloud security|application security)\b',
        title, re.I))


def evaluate(job: JobInput, ignore_salary: bool = False) -> EligibilityDecision:
    reasons = []
    filter_title = title_filter_enabled(job)
    exclusion = role_exclusion(job)
    if exclusion:
        reasons.append(exclusion)
    if cybersecurity_role(job.title, job.description) and not exclusion:
        reasons.append('cybersecurity-related role excluded by candidate preference')
    if filter_title and re.search(r'\bdata scientist\b', job.title, re.I):
        reasons.append('data scientist role excluded by candidate preference')
    if re.search(r'\b(?:Philippines|Taguig|Manila|El Salvador)\b', job.location, re.I) and not re.search(
        r'\b(?:Canada|United States|USA|worldwide|global)\b', job.location, re.I):
        reasons.append('role is outside approved geography')
    warnings = []
    if filter_title and title_review_reason(job.title, job.description):
        warnings.append(title_review_reason(job.title, job.description))
    text = "{} {} {}".format(job.title, job.location, job.description)
    employment = job.employment_type.lower().replace("_", "-")

    if _contains_any(job.title, ("salary guide", "average salary", "salary in", "salary report")):
        reasons.append("search result is salary content rather than a job posting")

    if job.closes_at:
        try:
            closes_at = datetime.fromisoformat(job.closes_at.replace("Z", "+00:00"))
            if closes_at.tzinfo is None:
                closes_at = closes_at.replace(tzinfo=timezone.utc)
            if closes_at < datetime.now(timezone.utc):
                reasons.append("posting has expired")
        except ValueError:
            warnings.append("posting expiry date needs confirmation")

    if not _contains_any(employment, ("full-time", "full time", "permanent")):
        reasons.append("not permanent full-time")
    if _contains_any(employment, ("contract", "temporary", "part-time", "part time")):
        reasons.append("contract, temporary, or part-time")
    # The description can contain data/analytics terms for almost any technical
    # role. Keep eligibility tied to an explicitly relevant title; description
    # overlap belongs in ranking, not in the pass/fail gate.
    if filter_title and not relevant_title(job.title):
        reasons.append("outside target or resume-adjacent roles")
    if filter_title and _contains_any(job.title, ("people data", "people analytics", "people scientist", "talent management")):
        reasons.append("people or talent analytics role is outside the candidate's domain")
    if filter_title and re.search(r"\b(?:manager|director|vice president|vp)\b|\bhead of\b", management_filter_title(job.title), re.I):
        reasons.append("management role excluded by candidate preference")
    if filter_title and re.search(r"\blead\b", job.title, re.I):
        reasons.append("lead role excluded by candidate preference")
    if filter_title and re.search(r"\bgtm\b|\bgo[- ]to[- ]market\b", job.title, re.I):
        reasons.append("go-to-market role excluded by candidate preference")
    experience_years = _minimum_experience_years(job.description)
    if experience_years and max(experience_years) > settings().get('maximum_required_experience', 7):
        reasons.append("required experience exceeds configured maximum")
    if 'french' not in {str(language).lower() for language in settings().get('fluent_languages', [])} and _contains_any(
        "{} {}".format(job.language_requirements, job.description),
        ("bilingual required", "must be bilingual", "french required", "fluent french", "fluency in french"),
    ):
        reasons.append("mandatory French or bilingual proficiency")

    country = _country(job)
    workplace = _workplace(job)
    workplace_review = False
    sponsorship_review = False
    floor = 0.0
    currency = "CAD" if country == "CA" else "USD"
    if country == "CA":
        if workplace in {"hybrid", "on-site"}:
            floor = settings().get('minimum_salary_cad', 0)
            if not _is_ottawa_gatineau(job.location):
                reasons.append("Canadian office role outside approved Canadian locations")
        elif workplace == "remote":
            floor = settings().get('minimum_salary_cad', 0)
            regional_restriction = _canadian_remote_restriction(job.description)
            if regional_restriction:
                reasons.append(regional_restriction)
        else:
            floor = settings().get('minimum_salary_cad', 0)
            if not _is_ottawa_gatineau(job.location):
                workplace_review = True
                warnings.append("remote or office status needs confirmation")
    elif country == "US":
        floor = settings().get('minimum_salary_usd', 0)
        sponsorship_text = "{} {}".format(job.sponsorship, job.description)
        if workplace in {"hybrid", "on-site"}:
            if not _is_approved_us_office_location(job.location):
                reasons.append("US office role is outside configured locations")
        elif workplace != "remote" and not _is_approved_us_office_location(job.location):
            reasons.append("US workplace arrangement needs confirmation")
        if _contains_any(
            sponsorship_text,
            ("u.s. based only", "us based only", "remote within the united states only"),
        ) or _us_residency_required(sponsorship_text) or re.search(
            r'\b(?:applicants|candidates|employees|you)\s+(?:must|are required to)\s+'
            r'(?:currently\s+)?(?:reside|live|be based)(?:\s+and\s+work)?\s+in\s+'
            r'(?:(?:one of|any of)\s+)?(?:the|these|those)\s+(?:following\s+|listed\s+|approved\s+)?states\b',
            sponsorship_text, re.I,
        ):
            reasons.append("US residence is required")
        if _contains_any(
            sponsorship_text,
            (
                "no sponsorship", "unable to sponsor", "will not sponsor", "does not sponsor",
                "sponsorship unavailable", "sponsorship is not available", "without sponsorship",
                "without the need for sponsorship",
            ),
        ) or re.search(
            r"without the need for (?:current or future )?(?:employer )?sponsorship",
            sponsorship_text,
            re.I,
        ):
            reasons.append("US role explicitly does not offer sponsorship")
        elif _us_citizenship_required(sponsorship_text):
            reasons.append("US citizenship is required")
        elif re.search(
            r"(?:requires?|must have) access to export[- ]controlled|must (?:be|qualify as) (?:a )?u\.?s\.? person",
            sponsorship_text,
            re.I,
        ):
            reasons.append("US-person export-control status is required")
        elif job.sponsorship.lower() not in {"available", "yes", "canadian eligible"}:
            sponsorship_review = True
            warnings.append("US sponsorship or Canadian eligibility needs confirmation")
    elif country == 'OTHER':
        reasons.append("role is outside Canada and the United States")
    else:
        warnings.append('Country eligibility needs evidence')

    clearance = "{} {}".format(job.security_clearance, job.description)
    if country == "US" and _contains_any(
        "{} {}".format(job.title, clearance),
        ("clearance required", "active security clearance", "active secret clearance", "must possess a security clearance"),
    ):
        reasons.append("US security clearance is required")
    elif _contains_any(clearance, ("security clearance", "secret clearance", "reliability status")):
        warnings.append("security-clearance requirement needs confirmation")

    if ignore_salary:
        if reasons:
            return EligibilityDecision("rejected", "rejected", tuple(reasons), tuple(warnings), floor, currency)
        review = workplace_review or sponsorship_review or bool(warnings)
        return EligibilityDecision("review" if review else "eligible", "estimated_pay_review" if review else "qualified", (), tuple(warnings), floor, currency)

    compensation = job.compensation.annualized() if job.compensation else None
    if compensation is None or compensation.maximum is None:
        if reasons:
            return EligibilityDecision("rejected", "rejected", tuple(reasons), tuple(warnings), floor, currency)
        return EligibilityDecision(
            "review",
            "estimated_pay_review",
            (),
            tuple(warnings + ["base salary is undisclosed or incomplete"]),
            floor,
            currency,
        )
    currency_mismatch = compensation.currency.upper() != currency
    if currency_mismatch:
        warnings.append("salary currency needs confirmation")
    if compensation.is_estimated:
        warnings.append("salary is an unconfirmed estimate")
    if compensation.includes_bonus_or_equity:
        warnings.append("salary may include bonus or equity rather than base only")
    if not currency_mismatch and compensation.maximum <= floor:
        reasons.append("maximum annual base does not exceed {} {:,.0f}".format(currency, floor))

    if reasons:
        return EligibilityDecision("rejected", "rejected", tuple(reasons), tuple(warnings), floor, currency)
    if (
        workplace_review
        or sponsorship_review
        or compensation.is_estimated
        or currency_mismatch
        or compensation.includes_bonus_or_equity
    ):
        return EligibilityDecision("review", "estimated_pay_review", (), tuple(warnings), floor, currency)
    return EligibilityDecision("eligible", "qualified", (), tuple(warnings), floor, currency)


def skill_match(job: JobInput) -> Dict:
    """Compare JD skills with resume-supported skills using local evidence only."""
    text = "{}. {}".format(job.title, job.description)
    lowered = text.lower()

    def sentence_at(position: int) -> str:
        starts = [lowered.rfind(mark, 0, position) for mark in (".", "\n", ";")]
        start = max(starts) + 1
        ends = [index for mark in (".", "\n", ";") for index in [lowered.find(mark, position)] if index >= 0]
        end = min(ends) if ends else len(lowered)
        return lowered[start:end]

    found = []
    required = []
    preferred = []
    for name, aliases in SKILL_ALIASES:
        positions = []
        for alias in aliases:
            positions.extend(match.start() for match in re.finditer(
                r"(?<!\w){}(?!\w)".format(re.escape(alias)), lowered, re.I
            ))
        if not positions:
            continue
        found.append(name)
        contexts = [sentence_at(position) for position in positions]
        if any(marker in context for context in contexts for marker in PREFERRED_MARKERS):
            preferred.append(name)
        elif any(marker in context for context in contexts for marker in REQUIRED_MARKERS):
            required.append(name)

    general = [name for name in found if name not in required and name not in preferred]
    matched = [name for name in found if name in set(settings().get('resume_supported_skills', []))]
    missing = [name for name in found if name not in set(settings().get('resume_supported_skills', []))]
    weights = {
        name: 2.0 if name in required else 0.5 if name in preferred else 1.0
        for name in found
    }
    total_weight = sum(weights.values())
    matched_weight = sum(weights[name] for name in matched)
    percent = round(100 * matched_weight / total_weight) if total_weight else 0
    if not found:
        confidence = "insufficient"
    elif job.source in {"brave", "adzuna"} and not job.canonical_url:
        confidence = "low (search snippet)"
    elif len(job.description) < 500:
        confidence = "medium (short description)"
    else:
        confidence = "high (full description)"
    return {
        "percent": percent,
        "jd_skills": tuple(found),
        "matched": tuple(matched),
        "missing": tuple(missing),
        "matched_required": tuple(name for name in required if name in set(settings().get('resume_supported_skills', []))),
        "missing_required": tuple(name for name in required if name not in set(settings().get('resume_supported_skills', []))),
        "matched_preferred": tuple(name for name in preferred if name in set(settings().get('resume_supported_skills', []))),
        "missing_preferred": tuple(name for name in preferred if name not in set(settings().get('resume_supported_skills', []))),
        "general": tuple(general),
        "confidence": confidence,
    }


def score(job: JobInput) -> MatchScore:
    text = "{} {} {}".format(job.title, job.description, job.location).lower()
    comparison = skill_match(job)
    skills = comparison["matched"]
    responsibilities = tuple(term for term in RESPONSIBILITY_TERMS if _contains(text, term))

    responsibility_points = round(40 * min(1.0, len(responsibilities) / 7))
    coverage_factor = min(1.0, len(comparison["jd_skills"]) / 4)
    skill_points = round(25 * comparison["percent"] / 100 * coverage_factor)
    seniority_points = 15 if _contains_any(job.title, ("senior", "lead", "principal")) else 8
    domain_points = 10 if _contains_any(text, ("energy", "trading", "financial", "government", "operations")) else 5
    compensation_points = 0
    compensation = job.compensation.annualized() if job.compensation else None
    if compensation and compensation.maximum:
        if compensation.currency.upper() == "CAD":
            if compensation.minimum and compensation.minimum <= 170000 and compensation.maximum >= 130000:
                compensation_points = 10
            elif compensation.maximum > 120000:
                compensation_points = 6
        elif compensation.currency.upper() == "USD" and compensation.maximum > 150000:
            compensation_points = 10
    total = min(100, responsibility_points + skill_points + seniority_points + domain_points + compensation_points)
    missing = comparison["missing"]
    explanation = (
        "responsibilities {}/40, skills {}/25 ({}% JD skill match), seniority {}/15, domain {}/10, compensation {}/10"
    ).format(
        responsibility_points, skill_points, comparison["percent"], seniority_points,
        domain_points, compensation_points,
    )
    return MatchScore(
        total=total,
        responsibilities=responsibility_points,
        skills=skill_points,
        seniority=seniority_points,
        domain=domain_points,
        compensation=compensation_points,
        explanation=explanation,
        matched_terms=skills + responsibilities,
        missing_terms=missing,
        skill_match_percent=comparison["percent"],
        jd_skills=comparison["jd_skills"],
        matched_skills=comparison["matched"],
        missing_skills=comparison["missing"],
        matched_required=comparison["matched_required"],
        missing_required=comparison["missing_required"],
        matched_preferred=comparison["matched_preferred"],
        missing_preferred=comparison["missing_preferred"],
        skill_confidence=comparison["confidence"],
    )


def to_jsonable(decision: EligibilityDecision, match: MatchScore) -> Tuple[Dict, Dict]:
    return decision.__dict__.copy(), match.__dict__.copy()
