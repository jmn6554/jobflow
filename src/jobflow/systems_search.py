"""Company-independent systems-analyst discovery vocabulary, not application approval."""
import re
import unicodedata
from urllib.parse import urlencode


def systems_title_kind(title):
    text=''.join(c for c in unicodedata.normalize('NFKD',title.lower()) if not unicodedata.combining(c))
    text=re.sub(r'[-–—/,]+',' ',text)
    text=re.sub(r'\s+',' ',text)
    french=bool(re.search(r'\banalyste\b',text))
    senior=bool(re.search(r'\b(?:senior|sr|seniore)\b',text) or
                (french and re.search(r'\bprincipal(?:e)?\b',text)))
    if not senior or re.search(r'\b(?:manager|director|lead|head|staff|cybersecurity|settlements?|gtm)\b|go.to.market',text):
        return ''
    if not french and re.search(r'\bprincipal\b',text):return ''
    if re.search(r'\bbusiness\s+(?:(?:technical|information|it)\s+)?systems?\s+analyst\b|\banalyst\s+(?:sr\.?\s+|senior\s+)?business systems\b',text):
        return 'technical_business_review'
    if re.search(r'\bbusiness analyst\b|\banalyste d.affaires\b',text):return ''
    if re.search(r'\bsystems?\s+(?:(?:integration|application|applications|technical|information)\s+)?analyst\b|\banalyst\s+(?:(?:senior|sr|iii|iv)\s+)?systems?\b',text):
        return 'systems'
    if french and re.search(r'\banalyste\s+(?:(?:principal|principale|senior|seniore|de|des|en)\s+){0,3}systemes?\b',text):
        return 'systems'
    if re.search(r'\bapplications?\s+(?:support\s+)?analyst\b',text) or (french and re.search(r'\banalyste\s+(?:(?:principal|principale|senior|seniore)\s+)?(?:fonctionnel|applicatif)',text)):
        return 'adjacent_application'
    return ''


def systems_search_plan():
    queries=[
        '("systems analyst" OR "system analyst")',
        '("systems integration analyst" OR "application systems analyst" OR "systems application analyst")',
        '("analyste systèmes" OR "analyste de systèmes" OR "analyste des systèmes" OR "analyste fonctionnel")',
        '("application analyst" OR "applications analyst" OR "application support analyst")',
        '("business systems analyst" OR "technical business analyst" OR "IT analyst" OR "information technology analyst")',
        '("integration analyst" OR "functional analyst" OR "analyste applicatif")',
    ]
    return [{'query':q,'url':'https://www.linkedin.com/jobs/search/?'+urlencode({
        'keywords':q,'location':'Canada','geoId':'101174742','f_WT':'2','sortBy':'DD'})} for q in queries]
