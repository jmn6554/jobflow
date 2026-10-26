"""Read-only Gmail OAuth and narrowly scoped application verification handling."""

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode, urlparse

import httpx

from .accounts import AccountStore

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
VERIFICATION_TERMS = re.compile(
    r"\b(?:verify|verification|confirm|confirmation|activate|activation|one[- ]time|security code|email code)\b", re.I
)
FORBIDDEN_TERMS = re.compile(
    r"\b(?:password reset|reset your password|account recovery|recover your account|unsubscribe)\b", re.I
)
TRUSTED_ATS_SENDER = re.compile(
    r"(?:greenhouse|lever|ashby|workday|smartrecruiters|icims|workable|teamtailor|jobvite|recruitee|bamboohr)",
    re.I,
)


class GmailConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class VerificationMessage:
    message_id: str
    sender: str
    subject: str
    received_ms: int
    code: str = ""
    links: tuple = ()


def _b64decode(value: str) -> str:
    if not value:
        return ""
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def _message_text(payload: dict) -> str:
    pieces: List[str] = []

    def visit(part: dict) -> None:
        mime = str(part.get("mimeType") or "")
        data = (part.get("body") or {}).get("data")
        if data and mime in {"text/plain", "text/html"}:
            value = _b64decode(data)
            if mime == "text/html":
                value = re.sub(r"<[^>]+>", " ", html.unescape(value))
            pieces.append(value)
        for child in part.get("parts") or []:
            visit(child)

    visit(payload or {})
    return "\n".join(pieces)


def _message_raw(payload: dict) -> str:
    pieces: List[str] = []

    def visit(part: dict) -> None:
        data = (part.get("body") or {}).get("data")
        if data and str(part.get("mimeType") or "") in {"text/plain", "text/html"}:
            pieces.append(_b64decode(data))
        for child in part.get("parts") or []:
            visit(child)

    visit(payload or {})
    return "\n".join(pieces)


def _header(payload: dict, name: str) -> str:
    wanted = name.lower()
    for item in payload.get("headers") or []:
        if str(item.get("name") or "").lower() == wanted:
            return str(item.get("value") or "")
    return ""


def _extract_code(text: str) -> str:
    patterns = (
        (r"copy and paste this code[^:]*:\s*([A-Z0-9]{4,12})\b", True),
        (r"(?:verification|security|one[- ]time|email|confirmation)\s*(?:code|pin)?\s*(?:is\s*:|is|:|-)?\s*([A-Z0-9]{4,8})\b", False),
        (r"\bcode\s*(?:is\s*:|is|:|-)\s*([A-Z0-9]{4,8})\b", False),
    )
    for pattern, strongly_anchored in patterns:
        for match in re.finditer(pattern, text, re.I):
            candidate = match.group(1)
            if strongly_anchored or re.search(r"\d", candidate):
                return candidate
    return ""


def _extract_links(raw: str) -> tuple:
    candidates = re.findall(r'https://[^\s<>"\']+', html.unescape(raw or ""), re.I)
    safe = []
    for candidate in candidates:
        candidate = candidate.rstrip(".,);]")
        parsed = urlparse(candidate)
        combined = (parsed.path + "?" + parsed.query).lower()
        if not parsed.hostname or FORBIDDEN_TERMS.search(combined.replace("-", " ")):
            continue
        if re.search(r"(?:verify|confirm|activate|activation|validate|token)", combined, re.I):
            safe.append(candidate)
    return tuple(dict.fromkeys(safe))


class GmailVerificationClient:
    """Owns a local Gmail OAuth token and finds recent verification messages."""

    def __init__(
        self,
        client_secret_path: Path = Path("private-data/google-oauth-client.json"),
        token_path: Path = Path("private-data/gmail-token.json"),
        redirect_uri: str = "http://127.0.0.1:8741/oauth/gmail/callback",
        transport=None,
    ):
        self.client_secret_path = Path(client_secret_path).resolve()
        self.token_path = Path(token_path).resolve()
        self.state_path = self.token_path.with_name("gmail-oauth-state.json")
        self.redirect_uri = redirect_uri
        self.transport = transport

    @classmethod
    def from_environment(cls):
        return cls(
            Path(os.environ.get("JOBFLOW_GMAIL_CLIENT_SECRET", "private-data/google-oauth-client.json")),
            Path(os.environ.get("JOBFLOW_GMAIL_TOKEN", "private-data/gmail-token.json")),
            os.environ.get("JOBFLOW_GMAIL_REDIRECT_URI", "http://127.0.0.1:8741/oauth/gmail/callback"),
        )

    def configured(self) -> bool:
        return self.client_secret_path.is_file()

    def connected(self) -> bool:
        if not self.token_path.is_file():
            return False
        try:
            token = self._read_json(self.token_path)
            return bool(token.get("access_token") or token.get("refresh_token"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _read_json(path: Path) -> dict:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def _client(self) -> dict:
        if not self.configured():
            raise GmailConfigurationError(
                "Google OAuth client file is missing: {}".format(self.client_secret_path)
            )
        config = self._read_json(self.client_secret_path)
        client = config.get("installed") or config.get("web")
        if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
            raise GmailConfigurationError("Google OAuth client JSON is invalid")
        return client

    def _get(self, url: str, **kwargs):
        with httpx.Client(transport=self.transport, timeout=30) as client:
            return client.get(url, **kwargs)

    def _post(self, url: str, **kwargs):
        with httpx.Client(transport=self.transport, timeout=30) as client:
            return client.post(url, **kwargs)

    def _write_private_json(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    def authorization_url(self) -> str:
        client = self._client()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self._write_private_json(self.state_path, {
            "state": state, "verifier": verifier, "created_at": int(time.time()),
        })
        return GOOGLE_AUTH_URL + "?" + urlencode({
            "client_id": client["client_id"],
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    def complete_authorization(self, code: str, state: str) -> None:
        saved = self._read_json(self.state_path)
        if not secrets.compare_digest(str(saved.get("state") or ""), str(state or "")):
            raise GmailConfigurationError("Gmail OAuth state did not match")
        if int(time.time()) - int(saved.get("created_at") or 0) > 600:
            raise GmailConfigurationError("Gmail OAuth request expired; start again")
        client = self._client()
        response = self._post(GOOGLE_TOKEN_URL, data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "code_verifier": saved["verifier"],
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        })
        response.raise_for_status()
        token = response.json()
        token["expires_at"] = int(time.time()) + int(token.get("expires_in") or 3600)
        self._write_private_json(self.token_path, token)
        self.state_path.unlink(missing_ok=True)

    def _access_token(self) -> str:
        token = self._read_json(self.token_path)
        if token.get("access_token") and int(token.get("expires_at") or 0) > int(time.time()) + 60:
            return token["access_token"]
        if not token.get("refresh_token"):
            raise GmailConfigurationError("Gmail authorization expired; reconnect Gmail")
        client = self._client()
        response = self._post(GOOGLE_TOKEN_URL, data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        })
        response.raise_for_status()
        refreshed = response.json()
        token.update(refreshed)
        token["expires_at"] = int(time.time()) + int(refreshed.get("expires_in") or 3600)
        self._write_private_json(self.token_path, token)
        return token["access_token"]

    def profile(self) -> dict:
        response = self._get(
            GMAIL_API + "/profile", headers={"Authorization": "Bearer " + self._access_token()},
        )
        response.raise_for_status()
        return response.json()

    def find_verification(self, after_epoch: int, company: str = "", limit: int = 10, expected_url: str = "", expected_recipient: str = "") -> Optional[VerificationMessage]:
        token = self._access_token()
        headers = {"Authorization": "Bearer " + token}
        query = 'after:{} {{subject:verify subject:verification subject:confirm subject:activate subject:activation subject:"security code"}}'.format(
            max(0, int(after_epoch) - 60)
        )
        listing = self._get(
            GMAIL_API + "/messages", headers=headers, params={"q": query, "maxResults": min(limit, 25)},
        )
        listing.raise_for_status()
        company_terms = [term for term in re.findall(r"[a-z0-9]+", company.lower()) if len(term) >= 4]
        matches = []
        for item in listing.json().get("messages") or []:
            response = self._get(
                GMAIL_API + "/messages/" + item["id"], headers=headers, params={"format": "full"},
            )
            response.raise_for_status()
            message = response.json()
            payload = message.get("payload") or {}
            if expected_recipient and expected_recipient.lower() not in re.findall(
                    r"[\w.+%-]+@[\w.-]+\.[a-z]{2,}", _header(payload, "To").lower()):
                continue
            subject = _header(payload, "Subject")
            sender = _header(payload, "From")
            raw_text = _message_text(payload)
            combined = "{}\n{}\n{}".format(subject, sender, raw_text)
            if not VERIFICATION_TERMS.search(combined) or FORBIDDEN_TERMS.search(combined):
                continue
            received_ms = int(message.get("internalDate") or 0)
            # Search indexing can include an earlier code from the same company.
            # Never apply a code issued before this particular form submission.
            if received_ms < max(0, int(after_epoch) - 3) * 1000:
                continue
            company_match = bool(company_terms and any(term in combined.lower() for term in company_terms))
            links = _extract_links(_message_raw(payload))
            if expected_url:
                expected_host = (urlparse(expected_url).hostname or '').lower()
                host_match = bool(expected_host and any(
                    (urlparse(link).hostname or '').lower() == expected_host
                    and AccountStore.key(link) == AccountStore.key(expected_url)
                    for link in links))
                # A recent message from *another* Workday employer is not this
                # account's verification email, even though the ATS is trusted.
                if not company_match and not host_match:
                    continue
            if company_terms and not company_match and not TRUSTED_ATS_SENDER.search(sender):
                continue
            matches.append(VerificationMessage(
                str(message.get("id") or ""), sender, subject, received_ms,
                _extract_code(combined), links,
            ))
        matches.sort(key=lambda item: item.received_ms, reverse=True)
        return matches[0] if matches else None

    def wait_for_verification(self, after_epoch: int, company: str = "", timeout_seconds: int = 90, expected_url: str = "", expected_recipient: str = ""):
        deadline = time.monotonic() + max(0, min(int(timeout_seconds), 180))
        while time.monotonic() <= deadline:
            message = (self.find_verification(after_epoch, company, expected_url=expected_url,
                                             expected_recipient=expected_recipient)
                       if expected_recipient else
                       self.find_verification(after_epoch, company, expected_url=expected_url)
                       if expected_url else self.find_verification(after_epoch, company))
            if message:
                return message
            if time.monotonic() + 5 > deadline:
                break
            time.sleep(5)
        return None

    def find_password_reset(self, after_epoch: int, expected_url: str, email: str):
        """Only recovery mail explicitly requested for this exact ATS tenant."""
        host = (urlparse(expected_url).hostname or '').lower()
        if not host or not email:
            return None
        headers = {"Authorization": "Bearer " + self._access_token()}
        listing = self._get(GMAIL_API + '/messages', headers=headers, params={
            'q': 'after:{} {{subject:"password reset" subject:"reset password" subject:"reset your password"}}'.format(max(0, after_epoch - 60)),
            'maxResults': 25,
        })
        listing.raise_for_status()
        matches = []
        for item in listing.json().get('messages') or []:
            response = self._get(GMAIL_API + '/messages/' + item['id'], headers=headers, params={'format': 'full'})
            response.raise_for_status()
            message = response.json()
            payload = message.get('payload') or {}
            received = int(message.get('internalDate') or 0)
            if received < (after_epoch - 3) * 1000:
                continue
            recipients = {address.lower() for _, address in getaddresses([_header(payload, 'To')])}
            if email.lower() not in recipients:
                continue
            subject = _header(payload, 'Subject')
            if not re.search(r'password.{0,20}reset|reset.{0,20}password', subject, re.I):
                continue
            links = []
            for raw in re.findall(r'https://[^\s<>"\']+', _message_raw(payload)):
                link = html.unescape(raw).rstrip(').,;')
                parsed = urlparse(link)
                if (parsed.hostname or '').lower() != host or parsed.username or parsed.password:
                    continue
                # Shared Workday hosts require the same recruiting tenant too.
                if host.endswith('.myworkdaysite.com'):
                    tenant = re.search(r'/recruiting/([^/]+)', urlparse(expected_url).path)
                    if not tenant or '/recruiting/' + tenant.group(1) + '/' not in parsed.path:
                        continue
                if re.search(r'reset|forgot', parsed.path + '?' + parsed.query, re.I) and link not in links:
                    links.append(link)
            if len(links) == 1:
                matches.append(VerificationMessage(str(message.get('id') or ''), _header(payload, 'From'), subject, received, links=tuple(links)))
        return max(matches, key=lambda x: x.received_ms) if matches else None

    def fill_code_challenge(self, page, after_epoch: int, company: str = "", timeout_seconds: int = 90) -> bool:
        """Fill one unambiguous email-code field; never handles CAPTCHA or account MFA."""
        prompt = re.compile(
            r"(?:check your email|email).{0,80}(?:code|verify)|"
            r"verification code(?: was)? sent|security code",
            re.I | re.S,
        )
        # Greenhouse renders the verification step asynchronously after the
        # initial submit response. Give that transition a short bounded wait
        # instead of checking the old form only once.
        prompt_deadline = time.monotonic() + 30
        body = ""
        while time.monotonic() < prompt_deadline:
            surfaces = page.frames if hasattr(page, "frames") else [page]
            body_parts = []
            for surface in surfaces:
                try:
                    body_parts.append(surface.locator("body").inner_text())
                except Exception:
                    continue
            body = "\n".join(body_parts)
            if prompt.search(body):
                break
            page.wait_for_timeout(500)
        if not prompt.search(body):
            return False
        surfaces = page.frames if hasattr(page, "frames") else [page]
        candidates = []
        for surface in surfaces:
            controls = surface.locator(
                'input:visible[name*="code" i], input:visible[id*="code" i], '
                'input:visible[autocomplete="one-time-code"], input[data-input-otp]'
            )
            split_controls = surface.locator(
                'input:visible[maxlength="1"], input:visible[data-testid*="code" i], '
                'input:visible[class*="otp" i]'
            )
            if controls.count() == 1 or split_controls.count() in range(4, 9):
                candidates.append((surface, controls, split_controls))
        if len(candidates) != 1:
            if os.environ.get("JOBFLOW_DEBUG_VERIFICATION", "").lower() in {"1", "true", "yes"}:
                for surface in surfaces:
                    try:
                        metadata = surface.locator("input").evaluate_all("""elements => elements.map(el => ({
                            type: el.type, name: el.name, id: el.id, className: el.className,
                            maxLength: el.maxLength, autocomplete: el.autocomplete,
                            ariaLabel: el.getAttribute('aria-label'),
                            dataInputOtp: el.getAttribute('data-input-otp'),
                            visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                        }))""")
                        print("GMAIL_CODE_INPUTS", json.dumps(metadata), flush=True)
                    except Exception:
                        continue
            return False
        target, controls, split_controls = candidates[0]
        single_control = controls.count() == 1
        split_count = split_controls.count()
        if not single_control and split_count not in range(4, 9):
            return False
        message = self.wait_for_verification(after_epoch, company, timeout_seconds)
        if not message or not message.code:
            return False
        if single_control:
            controls.first.fill(message.code)
        elif split_count == len(message.code):
            for index, character in enumerate(message.code):
                split_controls.nth(index).fill(character)
        else:
            return False
        buttons = target.get_by_role("button", name=re.compile(r"verify|confirm|continue", re.I)).all()
        button = next((item for item in buttons if item.is_visible() and item.is_enabled()), None)
        if button is None:
            # Some ATS widgets validate immediately when the final character is entered.
            page.wait_for_timeout(2500)
            return True
        button.click()
        page.wait_for_timeout(2000)
        return True
