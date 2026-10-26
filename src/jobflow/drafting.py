from typing import Dict, Tuple


def _relevant_evidence(job: Dict, profile: Dict):
    job_text = "{} {}".format(job["title"], job["description"]).lower()
    evidence = []
    for item in profile.get("achievements", []):
        tags = [str(tag).lower() for tag in item.get("tags", [])]
        if not tags or any(tag in job_text for tag in tags):
            evidence.append(item.get("text", "").strip())
    return [item for item in evidence if item][:3]


def create_drafts(job: Dict, profile: Dict) -> Tuple[str, str]:
    evidence = _relevant_evidence(job, profile)
    skills = [skill for skill in profile.get("skills", []) if skill.lower() in job["description"].lower()]
    resume_lines = [
        "# Resume tailoring notes — {} at {}".format(job["title"], job["company"]),
        "",
        "Review these suggestions against the original resume. Do not add claims that are not true.",
        "",
        "## Relevant skills",
    ]
    resume_lines.extend("- {}".format(skill) for skill in skills[:10])
    resume_lines.extend(["", "## Evidence to emphasize"])
    resume_lines.extend("- {}".format(item) for item in evidence)
    resume_lines.extend(["", "## Gaps to address honestly", "- Review the posting for requirements not supported by the profile."])

    greeting = profile.get("cover_letter", {}).get("greeting", "Hiring team")
    intro = profile.get("cover_letter", {}).get(
        "intro", "I am interested in applying my experience to this opportunity."
    )
    body = "\n\n".join(evidence) if evidence else "[Add one verified example relevant to this role.]"
    cover = """# Cover letter draft

Dear {greeting},

I am writing to apply for the {title} role at {company}. {intro}

{body}

The role's focus on {skills} aligns with the experience summarized above. I would welcome the opportunity to discuss how I could contribute.

Sincerely,
{name}

---
Human review required: verify every claim, adjust tone, and submit manually.
""".format(
        greeting=greeting, title=job["title"], company=job["company"], intro=intro,
        body=body, skills=", ".join(skills[:4]) or "the stated responsibilities", name=profile["name"]
    )
    return "\n".join(resume_lines) + "\n", cover
