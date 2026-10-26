"""Shared preferences, independent of runners and browser implementations."""
import json
import os
import re
from functools import lru_cache
from pathlib import Path

CONFIG = Path(os.environ.get('JOBFLOW_CONFIG', 'config/daily.json'))

@lru_cache(maxsize=1)
def settings():
    return json.loads(CONFIG.read_text())

def skill_threshold_met(percent):
    """Zero disables skill-based exclusion, including missing skill-score evidence."""
    threshold = settings().get('minimum_skill_match', 0)
    return threshold <= 0 or (percent or 0) >= threshold

def local_office(location):
    return any(re.search(r'\b' + re.escape(name) + r'\b', location, re.I)
               for name in settings()['canadian_office_locations'])

def us_office(location):
    if re.search(r'\bwashington\s*,?\s*d\.?c\.?\b|district of columbia', location, re.I):
        return False
    states = settings()['us_office_states']
    return any(re.search(r'\b' + re.escape(name) + r'\b', location, re.I)
               for name in settings()['us_office_locations']) or bool(states and re.search(
                   r'(?:^|,\s*)(?:' + '|'.join(re.escape(s) for s in states) + r')(?:\s*,|\s*$)', location, re.I))
