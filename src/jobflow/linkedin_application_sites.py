"""Route LinkedIn employer links by application support, not job suitability."""
from urllib.parse import parse_qs, urlsplit

from .applications import provider_for_url
from .providers import aggregator_url


def application_site(url, config):
    provider = provider_for_url(url)
    if provider == 'prohibited' or aggregator_url(url):
        return {'provider': provider, 'preferred': False,
                'reason': 'Direct employer application link required; no LinkedIn Easy Apply'}
    # Greenhouse also embeds forms on employer domains with this posting ID.
    if provider == 'employer' and parse_qs(urlsplit(url).query).get('gh_jid', [''])[0].isdigit():
        provider = 'greenhouse'
    focus = config.get('application_site_focus', {})
    preferred = not focus.get('enabled', False) or provider in focus.get('preferred_providers', [])
    return {'provider': provider, 'preferred': preferred,
            'reason': '' if preferred else 'Application site outside preferred supported platforms; retain for manual review'}
