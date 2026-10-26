"""Minimal SMTP summaries that deliberately exclude sensitive application data."""

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable
from .providers import public_http_url


def build_discovery_email(run, jobs: Iterable) -> EmailMessage:
    jobs = list(jobs)
    message = EmailMessage()
    message["Subject"] = "JobFlow: {} qualified job{}".format(len(jobs), "" if len(jobs) == 1 else "s")
    message["From"] = os.environ.get("JOBFLOW_EMAIL_FROM", "")
    message["To"] = os.environ.get("JOBFLOW_EMAIL_TO", "")
    lines = [
        "Your JobFlow discovery run is complete.",
        "",
        "Discovered: {} | Qualified: {} | Pay review: {}".format(
            run["discovered_count"], run["qualified_count"], run["review_count"]
        ),
        "",
    ]
    for job in jobs[:30]:
        lines.append("{} — {} — {}/100".format(job["title"], job["company"], job["fit_score"] or 0))
        url = job["canonical_url"] or job["url"]
        lines.append(url if public_http_url(url) else "Link unavailable")
        lines.append("")
    lines.append("Open the local dashboard to review details and choose which resume to prepare.")
    message.set_content("\n".join(lines))
    return message


def send_discovery_email(run, jobs: Iterable) -> bool:
    """Send only when all SMTP settings are present; return False if disabled."""
    host = os.environ.get("JOBFLOW_SMTP_HOST", "")
    username = os.environ.get("JOBFLOW_SMTP_USERNAME", "")
    password = os.environ.get("JOBFLOW_SMTP_PASSWORD", "")
    if not all((host, username, password, os.environ.get("JOBFLOW_EMAIL_FROM"), os.environ.get("JOBFLOW_EMAIL_TO"))):
        return False
    port = int(os.environ.get("JOBFLOW_SMTP_PORT", "465"))
    message = build_discovery_email(run, jobs)
    if os.environ.get("JOBFLOW_SMTP_STARTTLS", "").lower() in {"1", "true", "yes"}:
        with smtplib.SMTP(host, port, timeout=30) as client:
            client.starttls(context=ssl.create_default_context())
            client.login(username, password)
            client.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as client:
            client.login(username, password)
            client.send_message(message)
    return True
