"""Narrow, shared consent policy for application forms and account intake."""

import re


def application_privacy_answer(question: str, values: dict):
    text = re.sub(r"[^a-z0-9]+", " ", question.lower())
    if not values.get('application_privacy_consent'):
        return None
    if re.search(r'\b(?:experience|certification|expertise|proficiency|skills)\b', text) and not re.search(
        r'\b(?:consent|agree|acknowledge|accept)\b', text):
        return None
    if values.get('all_application_privacy_consent') and re.fullmatch(
        r'\s*processing of personal data\s*', text):
        return 'Yes'
    if values.get('all_application_privacy_consent') and re.fullmatch(
        r'\s*(?:required )?allow us to (?:process|collect and process) your personal (?:information|data)\s*', text):
        return 'Yes'
    if values.get('all_application_privacy_consent') and (
        re.search(r'\bprivacy (?:notice|policy|statement|consent|terms)\b|\bdata protection\b', text)
        or (re.search(r'\b(?:personal|demographic|application|candidate)\b', text)
            and re.search(r'\b(?:data|information|responses)\b', text)
            and re.search(r'\b(?:consent|agree|acknowledge)\b', text))
    ):
        return 'No' if re.search(r'\b(?:do not|decline|refuse|opt out)\b', text) else 'Yes'
    # Bundled permissions must not silently inherit privacy approval.
    if re.search(r'\b(marketing|newsletter|sms|text messages|whatsapp|record(?:ing|ings)?|transcrib\w*|brighthire|'
                 r'demographic|arbitration|arbitrate|waiv\w*|background check|'
                 r'future (?:job|opportunit)|talent (?:pool|community|network))\b', text):
        return None
    if re.search(r'\bprivacy\b|\bdata protection\b', text) or (
        re.search(r'\b(?:personal data|personal information|application information)\b', text)
        and re.search(r'\b(?:process\w*|collect\w*|consent)\b', text)
        and re.search(r'\b(?:application|recruit\w*|candidate)\b', text)
    ):
        # A refusal is not an acknowledgement, even if its label cites privacy.
        if re.search(r'\b(?:do not|decline|refuse|opt out)\b', text):
            return 'No'
        return 'Yes'
    return None
