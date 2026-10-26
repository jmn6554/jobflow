"""Role-specific workplace evidence, independent of employer or ATS.

Company remote-first branding, remote-work benefits, and occasional work from
home are not proof that this vacancy can be performed remotely.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkplaceEvidence:
    mode: str = 'unspecified'
    snippets: tuple = ()
    conflict: bool = False


_QUALIFIER = r'(?:(?:fully|100%|entirely|permanently|full[- ]time)\s+)*'
_MODE = r'(remote|hybrid|on[- ]?site|in[- ]office)'
_PATTERNS = (
    re.compile(r'\b(?:this|the|our)\s+(?:role|position|job)\s+(?:is|will be|can be)\s+(?:a\s+)?' + _QUALIFIER + _MODE + r'\b', re.I),
    re.compile(r'\bthis\s+is\s+(?:a\s+)?' + _QUALIFIER + _MODE + r'\s+(?:role|position|job)\b', re.I),
    re.compile(r'\b(?:location|work\s+arrangement|workplace\s+type|location\s+type)\s*:\s*' + _QUALIFIER + _MODE + r'\b', re.I),
    re.compile(r'\b(?:this|the)\s+(?:role|position|job)\s+(?:can|may)\s+be\s+(?:performed|done)\s+(remotely)\b', re.I),
)
_NO_REMOTE = re.compile(
    r'\b(?:this|the)\s+(?:role|position|job)\s+(?:is not|cannot be|is not available)\s+(?:a\s+)?(?:performed\s+)?(?:fully\s+)?remote(?:ly)?\b'
    r'|\bremote\s+(?:work(?:ing)?\s+)?(?:option\s+)?is\s+not\s+(?:available|permitted|offered)\b', re.I)
_REQUIRED_OFFICE = re.compile(
    r'\b(?:you|individuals|the successful candidate|the successful applicant|employees|team members)\s+'
    r'(?:must|are expected to|are required to|will be required to)\s+(?:work|be|attend)\s+'
    r'(?:in[- ]office|on[- ]?site|in the office|from the office)\b', re.I)


def _conditional(text, start):
    # Compensation/legal boilerplate often says "If this role is remote".
    # A hypothetical workplace statement is not evidence of this job's mode.
    prefix = re.split(r'[.!?\n]', text[:start])[-1]
    return bool(re.search(r'\b(?:if|whether|unless|in case)\b[^,;:]{0,100}$', prefix, re.I))


def description_workplace(text: str) -> WorkplaceEvidence:
    modes, snippets = set(), []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text or ''):
            if _conditional(text, match.start()):
                continue
            value = match[1].lower().replace(' ', '-')
            mode = 'remote' if value == 'remotely' else 'on-site' if value in {'onsite', 'on-site', 'in-office'} else value
            modes.add(mode)
            snippets.append(match[0])
    for pattern in (_NO_REMOTE, _REQUIRED_OFFICE):
        for match in pattern.finditer(text or ''):
            if _conditional(text, match.start()):
                continue
            modes.add('on-site')
            snippets.append(match[0])
    # Hybrid and on-site both entail an office; remote versus either requires
    # resolution instead of guessing which statement overrides the other.
    conflict = 'remote' in modes and bool(modes & {'hybrid', 'on-site'})
    mode = ('unspecified' if conflict or not modes else
            'hybrid' if 'hybrid' in modes else 'on-site' if 'on-site' in modes else 'remote')
    return WorkplaceEvidence(mode, tuple(dict.fromkeys(snippets)), conflict)
