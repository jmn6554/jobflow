"""Approved implementation/integration vocabulary; discovery is not approval."""
import re
import unicodedata

SEARCH_TERMS = (
    'implementation', 'integration', 'technical consultant', 'application consultant',
    'business applications analyst', 'business systems consultant', 'functional',
    'technical onboarding', 'professional services consultant', 'implantation',
)


def normalize(text):
    return re.sub(r'\s+', ' ', ''.join(c for c in unicodedata.normalize('NFKD', text or '')
                                      if not unicodedata.combining(c)).lower()).strip()


def focused_title_family(title):
    text = normalize(title)
    french = bool(re.search(r'\banalyste\b|\b(?:consultant|conseiller)\s+(?:en|d[e’\x27])\b', text))
    leadership = r'\b(?:manager|director|lead|head|staff|vp|directeur|gestionnaire|chef)\b'
    if re.search(leadership, text) or (not french and re.search(r'\bprincipal\b', text)):
        return ''
    if re.search(r'\b(?:cybersecurity|security|gtm|marketing|sales|hardware|construction|settlements?)\b|go.to.market', text):
        return ''
    senior = bool(re.search(r'\b(?:senior|sr|principal(?:e)?)\b', text))
    if senior and (re.search(r'\b(?:business(?:\s+(?:systems?|it|technical))?|(?:it|technical)\s+business|systems?|applications?)\s+analysts?\b', text)
                   or re.search(r'\banalyst\s*[,/-]\s*(?:business(?: systems?)?|systems?|applications?)\s*(?:$|[(/–—-])', text)
                   or (french and re.search(r"\b(?:affaires|systemes?|fonctionnel(?:le)?|applicatif)\b", text))):
        return 'Senior business/systems analyst'
    if re.search(r'\b(?:implementation|integration)s?\s+(?:analyst|consultant|specialist|engineer)\b', text):
        return 'Implementation/integration'
    if french and re.search(r'\b(?:integration|implantation)\b', text):
        return 'Implementation/integration'
    if re.search(r'\b(?:technical|applications?|software implementation|functional|professional services)\s+consultant\b|\bbusiness\s+(?:applications? analyst|systems? consultant)\b|\bfunctional analyst\b|\btechnical onboarding specialist\b', text):
        return 'Adjacent technical implementation'
    return ''


def responsibility_review(title, description):
    """Retain ambiguous matches for review, never discard for absent keywords."""
    family = focused_title_family(title)
    if not family or family == 'Senior business/systems analyst':
        return ''
    if re.search(r'\b(?:software|SaaS|SQL|APIs?|configuration|data migration|systems integration|ERP|CRM|implementation of systems)\b', description, re.I):
        return ''
    return 'Technical/software implementation responsibilities need confirmation'
