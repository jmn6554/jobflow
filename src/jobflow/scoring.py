import json
import re
from pathlib import Path
from typing import Dict

from .models import ScoreResult


def load_profile(path: str) -> Dict:
    with Path(path).open(encoding="utf-8") as handle:
        profile = json.load(handle)
    if not profile.get("name") or not isinstance(profile.get("skills", []), list):
        raise ValueError("Profile requires a name and a skills list")
    return profile


def _contains(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w){}(?!\w)".format(re.escape(phrase.lower())), text.lower()))


def score_job(job: Dict, profile: Dict) -> ScoreResult:
    text = "{} {} {}".format(job["title"], job["description"], job["location"])
    required = [str(x) for x in profile.get("target", {}).get("required_keywords", [])]
    preferred = [str(x) for x in profile.get("target", {}).get("preferred_keywords", [])]
    locations = [str(x) for x in profile.get("target", {}).get("locations", [])]
    skills = [str(x) for x in profile.get("skills", [])]

    matched_required = tuple(x for x in required if _contains(text, x))
    missing_required = tuple(x for x in required if x not in matched_required)
    matched_preferred = tuple(x for x in preferred if _contains(text, x))
    matched_skills = tuple(x for x in skills if _contains(text, x))

    required_points = 45 * len(matched_required) / max(1, len(required))
    preferred_points = 20 * len(matched_preferred) / max(1, len(preferred))
    skill_points = 25 * len(matched_skills) / max(1, len(skills))
    location_points = 10 if not locations or any(_contains(job["location"], x) for x in locations) else 0
    score = min(100, round(required_points + preferred_points + skill_points + location_points))
    explanation = "required {}/{}, preferred {}/{}, profile skills {}/{}, location {}".format(
        len(matched_required), len(required), len(matched_preferred), len(preferred),
        len(matched_skills), len(skills), "matched" if location_points else "not matched"
    )
    return ScoreResult(score, matched_required, missing_required, matched_preferred, explanation)
