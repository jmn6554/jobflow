"""Shared, employer-independent role vocabulary.

This only recognizes role families. Geography, seniority, excluded domains and
required qualifications remain separate policy gates; JD keywords do not make
an unrelated title eligible.
"""
import re
from .policy import settings
from .systems_search import systems_title_kind
from .implementation_search import focused_title_family, SEARCH_TERMS, responsibility_review


SYSTEMS_ADJACENT_SEARCH_TERMS = (
    'application analyst', 'applications analyst', 'application systems analyst',
    'application support analyst', 'IT analyst', 'information technology analyst',
    'integration analyst', 'technical business analyst', 'functional analyst',
    'analyste systèmes', 'analyste fonctionnel', 'analyste applicatif',
) + SEARCH_TERMS

_PRODUCT_MANAGER = re.compile(
    r'\bproduct\s+managers?\b|\bmanagers?\s*[,/:–—-]\s*product\b', re.I,
)


def management_filter_title(title: str) -> str:
    """Only waive the generic manager exclusion when product roles are allowed."""
    if settings().get('exclude_product_management_roles', False):
        return title or ''
    return _PRODUCT_MANAGER.sub('', title or '')


_FORWARD = re.compile(
    r'\b(?:'
    r'business\s+(?:(?:(?:and|&)\s+)?systems?\s+)?analysts?|'
    r'business\s+(?:(?:technical|information|it)\s+)?(?:systems?|applications?)\s+analysts?|'
    r'(?:enterprise\s+)?applications?\s+(?:(?:systems?|support)\s+)?analysts?|'
    r'systems?\s+applications?\s+analysts?|'
    r'(?:it|information\s+technology|integration|functional)\s+analysts?|'
    r'analyste\s+(?:(?:senior|sénior|principal(?:e)?|de|des)\s+){0,3}'
    r'(?:syst[èe]mes?|fonctionnel(?:le)?|applicatif|applicative)|'
    r'(?:senior\s+)?(?:technical\s+|information\s+|it\s+)?systems?\s+analysts?|'
    r'data\s+(?:(?:quality|governance|operations|platform|integration|warehouse|analytics)\s+)?(?:analysts?|engineers?|developers?|architects?)|'
    r'(?:cloud\s+data|databricks)\s+engineers?|'
    r'(?:analytics|business\s+intelligence|bi|reporting)\s+(?:analysts?|engineers?|developers?)|'
    r'(?:bi|business\s+intelligence)\s+reports?\s+developers?|'
    r'(?:power\s*bi|sql|etl)\s+developers?|database\s+analysts?|'
    r'systems\s+integration\s+analysts?|product\s+analysts?'
    r')\b', re.I,
)
_REVERSED = re.compile(
    r'\b(?:analysts?|engineers?|developers?)(?:\s+(?:i{1,3}|iv|[1-4]|senior|sr\.?))?'
    r'\s*[,/:–—-]\s*'
    r'(?:data(?:\s+(?:quality|governance|operations|platform|systems|integrations?|warehouse|analytics|engineering))?'
    r'|business(?:\s+systems?)?|analytics|business\s+intelligence|bi|reporting|systems?'
    r'|applications?(?:\s+(?:systems?|support))?|it|information\s+technology|integration)\b'
    r'(?=\s*(?:$|[,(/–—-]|&\s*analytics\b))', re.I,
)


def relevant_title(title: str) -> bool:
    normalized = re.sub(r'\s+', ' ', title or '').strip()
    if project_role_exclusion(normalized) or product_role_exclusion(normalized):
        return False
    return bool(focused_title_family(normalized) or _PRODUCT_MANAGER.search(normalized) or systems_title_kind(normalized) == 'systems' or _FORWARD.search(normalized) or _REVERSED.search(normalized))


def systems_analyst_title(title: str) -> bool:
    return systems_title_kind(title) == 'systems' or bool(re.search(r'\bsystems?\s+analysts?\b|\banalysts?(?:\s+(?:i{1,3}|iv|[1-4]))?\s*[,/:–—-]\s*systems?\b', title, re.I))


def pipeline_responsibility_evidence(title: str, description: str) -> tuple:
    """Narrow, explainable route for data-systems/integration software titles.

    Not a keyword percentage or general software-role waiver: require actual
    ingestion and transformation duties alongside Python/SQL. Roles whose
    title promises full-stack, AI, research or platform work stay in review.
    """
    if not re.search(r'\bsoftware\s+engineers?(?:\s+(?:i{1,3}|iv|[1-4]))?\s*[,/:–—-]\s*data\s+(?:systems|integrations?)\b', title, re.I):
        return ()
    if not (re.search(r'\bPython\b', description, re.I) and re.search(r'\bSQL\b', description, re.I)):
        return ()
    duty_start = r'(?:^|\n|(?<=[.!?])\s+)\s*(?:[-•]\s*)?(?:you(?: will|.ll)\s+)?'
    patterns = (
        duty_start + r'(?:build|develop|design|implement|maintain)\b[^.\n]{0,70}\bdata pipelines\b',
        r'\b(?:ingest|ingestion)\b[^.\n]{0,80}\b(?:transform|transformation|sources?)\b',
        duty_start + r'(?:develop|build|support)\b[^.\n]{0,60}\bAPIs\b[^.\n]{0,70}\b(?:data integrations?|external systems)\b',
    )
    evidence = [re.search(pattern, description, re.I) for pattern in patterns]
    return tuple(match[0] for match in evidence) if all(evidence) else ()


def title_review_reason(title: str, description: str = '') -> str:
    implementation_review = responsibility_review(title, description)
    if implementation_review:
        return implementation_review
    # Data-platform software roles may mean infrastructure/backend engineering,
    # not the candidate's pipelines/BI work. Retain them, but do not auto-approve
    # the role merely because it contains "Engineer, Data".
    if _FORWARD.search(title or ''):
        return ''
    if pipeline_responsibility_evidence(title, description):
        return ''
    if relevant_title(title) and re.search(
        r'\b(?:software|full[- ]stack|ai|research|platform|endpoint|electrical|field)\s+'
        r'(?:engineers?|developers?)(?:\s+(?:i{1,3}|iv|[1-4]))?\s*[,/:–—-]', title, re.I):
        return 'Adjacent software-engineering role needs responsibility evidence'
    return ''


def software_role_exclusion(title: str) -> str:
    """Explicit title-only preference, independent of broad LinkedIn filtering.

    Do not exclude data/analytics engineers just because their descriptions
    mention software engineering practices or collaboration with developers.
    """
    if not settings().get('exclude_software_engineering_roles', False):
        return ''
    if re.search(
        r'\b(?:software(?:[\s-]+development)?|back[\s-]?end|front[\s-]?end|full[\s-]?stack|'
        r'python(?:\s*/\s*typescript)?|typescript|javascript|java|c\+\+)'
        r'[\s-]+(?:engineers?|developers?)\b|'
        r'\b(?:engineers?|developers?)\s*[,/:–—-]\s*(?:software|backend|frontend|full[\s-]?stack)\b|'
        r'\b(?:SWE|SDE)\b|'
        r'\b(?:ing[ée]nieur(?:e)?s?|d[ée]veloppeur(?:euse)?s?|d[ée]veloppeuses?)'
        r'\s+(?:(?:en|de)\s+)?(?:g[ée]nie\s+)?logiciel(?:le)?s?\b',
        title or '', re.I,
    ):
        return 'Software-engineering role excluded by candidate preference'
    return ''


def project_role_exclusion(title: str) -> str:
    """Exclude project managers, not product managers or project-related duties."""
    if settings().get('exclude_project_management_roles', False) and re.search(
        r'\bprojects?[\s-]+managers?\b|'
        r'\bmanagers?\s*[,/:–—-]\s*projects?\b|'
        r'\b(?:chef(?:fe)?s?|gestionnaires?|charg[ée](?:e)?s?)\s+(?:de|des)\s+projets?\b',
        title or '', re.I,
    ):
        return 'Project-manager role excluded by candidate preference'
    return ''


def product_role_exclusion(title: str) -> str:
    """Title-only restriction; product analysts and engineers remain eligible."""
    if settings().get('exclude_product_management_roles', False) and re.search(
        r'\bproduct[\s-]+(?:(?:technical|marketing|inbound|outbound|group|senior)[\s-]+){0,2}managers?\b|'
        r'\bmanagers?\s*[,/:–—-]\s*products?\b|'
        r'\b(?:chef(?:fe)?s?|gestionnaires?)\s+(?:de|des)\s+produits?\b',
        title or '', re.I,
    ):
        return 'Product-manager role excluded by candidate preference'
    return ''


def excluded_title(title: str) -> bool:
    """Consistent candidate-requested exclusions at intake and final screening."""
    if software_role_exclusion(title) or project_role_exclusion(title) or product_role_exclusion(title):
        return True
    title = management_filter_title(title)
    if (systems_title_kind(title or '') == 'systems' or focused_title_family(title)) and re.search(r'\banalyste\b', title, re.I):
        # French "analyste principal" denotes senior analyst, not an English
        # principal-level role. All other exclusions still apply below.
        title = re.sub(r'\bprincipal(?:e)?\b', '', title, flags=re.I)
    return bool(re.search(
        r'\b(?:manager|director|vice president|vp|lead|principal|staff|head of|'
        r'data scientist|gtm|go[- ]to[- ]market|revenue operations|revenue ops|revenue systems|revops|'
        r'sales operations|sales planning|sales analytics|sales forecasting|'
        r'people analytics|marketing|growth)\b', title or '', re.I,
    ))
