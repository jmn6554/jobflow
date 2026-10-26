"""Questions that require explicit saved answers rather than generated guesses."""
import re
import unicodedata
from .applications import MANUAL_ONLY_QUESTION_TERMS, normalize_question


def manual_only(question):
    normalized = normalize_question(question)
    folded = normalize_question(''.join(c for c in unicodedata.normalize('NFKD', question)
                                        if not unicodedata.combining(c)))
    if re.search(r'\b(?:preferred (?:communication )?language|language.{0,30}prefer\w*|langue.{0,30}prefer\w*)\b', folded):
        return True
    if re.search(r'\b(?:safe sender|sender.{0,30}safe|allowlist|whitelist)\b', normalized):
        return True
    return any(re.search(r'\b' + re.escape(term) + r'\b', normalized)
               for term in MANUAL_ONLY_QUESTION_TERMS) or bool(re.search(
        r'\b(?:consent|agree|agreement|privacy|signature|signer|otp|verification code|'
        r'political|pregnan|medical|marital|pronouns|birth|bank|credit card)\b', normalized))
