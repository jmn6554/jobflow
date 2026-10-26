"""Outreach triage, not a fraud detector or an identity-verification service.

Search snippets cannot establish account history, network size or authority.
Those observations must be checked on the actual profile and corroborated.
"""
import re


PERSONAL_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'outlook.com', 'outlook.fr', 'hotmail.com',
    'hotmail.ca', 'hotmail.fr', 'live.com', 'live.ca', 'live.fr', 'msn.com',
    'yahoo.com', 'yahoo.ca', 'yahoo.fr', 'ymail.com', 'rocketmail.com',
    'aol.com', 'icloud.com', 'me.com', 'mac.com', 'proton.me', 'protonmail.com',
    'pm.me', 'mail.com', 'gmx.com', 'gmx.net', 'gmx.de', 'zoho.com',
})

REVIEW_CHECKLIST = (
    'Original hiring post is at most 21 days old, still open, and explicitly permits remote work from Canada. A repost or search crawl date is not the original post date.',
    'Inspect About this member: join date, recent photo/contact changes and available verifications. A new account plus recent changes warrants extra scrutiny; no badge alone is not proof of fraud.',
    'Record follower and connection counts (or unavailable) and assess relevant colleagues, genuine interactions and consistent employment history. Small networks warrant context; large counts do not authenticate a recruiter.',
    'Check the contact email domain against the independently located employer or agency website. Personal/free email is a warning, not proof of fraud; corporate-looking domains can also be impersonations.',
    'Corroborate the person and the vacancy through an official employer/agency source, not just their own profile. Resolve warning signs before sharing a resume. Never send money, banking details or identity documents to an unverified contact.',
)


def search_safety(text):
    """Flag visible domains; never infer network counts or trust snippet claims.

An email may belong to a commenter, not the author. Report domains rather than
asserting ownership. Missing email is unknown, not evidence of corporate email.
"""
    domains = sorted({match.group(1).casefold() for match in re.finditer(
        r"[\w.+%\-]+@([a-zA-Z0-9](?:[a-zA-Z0-9.\-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,})(?![\w-]|\.[a-zA-Z0-9])",
        str(text or ''),
    )})
    personal = [domain for domain in domains if domain in PERSONAL_DOMAINS]
    warnings = []
    if personal:
        warnings.append('Personal/free email domain appears in this evidence: '
                        + ', '.join(personal)
                        + '. Confirm whose address it is and independently verify the person before sharing a resume.')
    return {
        'status': 'review_required',
        'personal_email_domains': personal,
        'warnings': warnings,
        'account_history': 'not_checked',
        'followers': None,
        'connections': None,
        'original_post_date': None,
        'independent_affiliation': 'not_checked',
        'send_ready': False,
    }
