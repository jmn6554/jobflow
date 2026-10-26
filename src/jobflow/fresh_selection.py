"""Positive eligibility evidence before fresh discovery becomes approval."""
import re
from .eligibility import application_guard, _country
from .models import JobInput


def fresh_application_hold(row):
    if re.search(r'\b(?:co[- ]?op|intern(?:ship)?|student|contract(?:or)?|temporary|part[- ]time)\b', row['title'], re.I):
        return 'Student, internship, contract or other non-permanent role excluded'
    job = JobInput(title=row['title'], company=row['company'], location=row.get('location', ''),
        url=row['url'], description=row.get('description', ''), source=row.get('source', ''),
        country=row.get('country', ''), workplace_type=row.get('workplace', 'unspecified'))
    country = _country(job)
    if country not in {'CA', 'US'}:
        return 'Outside approved geography' if country == 'OTHER' else 'Country eligibility needs evidence'
    # A review bucket is not permission to waive location, residency, title,
    # or experience constraints. Preserve uncertain rows in the report.
    return application_guard(job) or ''
