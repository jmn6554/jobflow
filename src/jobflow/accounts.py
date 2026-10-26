"""Automatic ATS account creation, sign-in, and email verification."""

import json
import os
import re
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from .consent import application_privacy_answer


@dataclass(frozen=True)
class AccountResolution:
    state: str
    detail: str


class AccountStore:
    """Owner-only local credential store keyed by ATS host."""

    def __init__(self, path=Path("private-data/application-accounts.json")):
        self.path = Path(path).resolve()

    @classmethod
    def from_environment(cls):
        return cls(os.environ.get("JOBFLOW_ACCOUNT_STORE", "private-data/application-accounts.json"))

    @staticmethod
    def key(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if host.endswith(('.successfactors.com', '.successfactors.eu', '.sapsf.com')):
            companies = parse_qs(parsed.query).get('company', [])
            if len(companies) == 1 and re.fullmatch(r'[\w.-]+', companies[0]):
                return host + '/company/' + companies[0].lower()
        # These hosts share authentication surfaces across unrelated employers.
        tenant = re.search(r'/recruiting/([^/]+)', parsed.path) if host.endswith('.myworkdaysite.com') else None
        return host + '/' + tenant.group(1).lower() if tenant else host

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"accounts": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("accounts", {}), dict):
            raise ValueError("ATS account store is invalid")
        value.setdefault("accounts", {})
        return value

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        # Set permissions BEFORE writing secrets, including temporary files.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)

    @staticmethod
    def _password(length=24) -> str:
        if not 12 <= length <= 64:
            raise ValueError('Unsupported password length')
        alphabet = string.ascii_letters + string.digits + "!@#%_-"
        # Guarantee the character classes most ATS password policies require.
        required = [
            secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits), secrets.choice("!@#%_-")
        ]
        return "".join(required + [secrets.choice(alphabet) for _ in range(length - 4)])

    def get(self, url: str):
        accounts = self._read()["accounts"]
        key = self.key(url)
        account = accounts.get(key)
        if account is None and "/company/" in key:
            legacy = accounts.get(key.split("/company/")[0])
            if legacy:
                # Old host-only credentials cannot be assigned to a new tenant
                # merely because both employers use the same SaaS server.
                return dict(legacy, legacy_scope_unverified=True)
        return account

    def enable_shared_new_account_password(self) -> None:
        """Explicit user opt-in; never change an existing employer credential."""
        data = self._read()
        policy = data.get('new_account_password') or {}
        if policy.get('mode') == 'shared':
            self._new_account_password(data, 24)  # Validate, never silently rotate.
            return
        data['new_account_password'] = {
            'mode': 'shared',
            'password': self._password(20),
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)

    def _new_account_password(self, data, password_length):
        policy = data.get('new_account_password') or {}
        if not policy:
            return self._password(password_length)
        password = policy.get('password')
        if (policy.get('mode') != 'shared' or not isinstance(password, str)
                or not 12 <= len(password) <= 64
                or not all((any(c.isupper() for c in password),
                            any(c.islower() for c in password),
                            any(c.isdigit() for c in password),
                            any(c in '!@#%_-' for c in password)))):
            raise ValueError('Invalid shared new-account password configuration')
        if len(password) > password_length:
            raise ValueError('Shared password exceeds requested employer password length; no credential created')
        return password

    def get_or_create(self, url: str, email: str, password_length=24) -> dict:
        data = self._read()
        key = self.key(url)
        if not key:
            raise ValueError("Cannot create an ATS account without a valid host")
        existing = data["accounts"].get(key)
        if existing:
            return existing
        if (self.get(url) or {}).get("legacy_scope_unverified"):
            raise ValueError("Legacy account needs employer-scope reconciliation before signup")
        account = {
            "email": email,
            "password": self._new_account_password(data, password_length),
            "status": "pending",
            "stage": "credentials_generated",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data["accounts"][key] = account
        self._write(data)
        return account

    def mark_confirmed(self, url: str) -> None:
        data = self._read()
        account = data["accounts"].get(self.key(url))
        if account:
            account["status"] = "confirmed"
            account["stage"] = "login_confirmed"
            account["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            self._write(data)

    def mark_verification_pending(self, url: str, requested_at: int):
        data = self._read()
        account = data['accounts'].get(self.key(url))
        if account:
            account.update(status='verification_pending', stage='verification_pending',
                           verification_requested_at=requested_at)
            self._write(data)

    def mark_email_verified(self, url: str):
        data = self._read()
        account = data['accounts'].get(self.key(url))
        if account:
            account.update(status='email_verified', stage='email_verified',
                           email_verified_at=datetime.now(timezone.utc).isoformat())
            self._write(data)

    def registration_event(self, url, stage):
        if stage not in {'registration_submitting', 'registration_submitted', 'registration_rejected'}:
            raise ValueError('Unknown account registration stage')
        data = self._read()
        account = data['accounts'].get(self.key(url))
        if account and account.get('status') not in {'confirmed', 'email_verified', 'password_reset'}:
            account['stage'] = stage
            if stage == 'registration_submitting':
                account['registration_requested_at'] = int(time.time())
            self._write(data)

    def begin_recovery(self, url: str):
        data = self._read()
        account = data['accounts'][self.key(url)]
        # Persist before sending: a crash must not resend recovery requests.
        if not account.get('recovery'):
            account['recovery'] = {'requested_at': int(time.time()), 'password': self._password(), 'state': 'requested'}
            self._write(data)
        return account['recovery']

    def recovery_state(self, url: str, state: str):
        data = self._read()
        account = data['accounts'][self.key(url)]
        account['recovery']['state'] = state
        if state == 'completed':
            account['password'] = account['recovery'].pop('password')
            account['status'] = 'password_reset'
        self._write(data)


class AccountAutomation:
    """Resolve common ATS login/signup gates without user interaction."""

    LOGIN_TEXT = re.compile(r"\b(?:sign in|log in|create (?:an )?account|register|sign up)\b", re.I)
    CAPTCHA_TEXT = re.compile(r"complete (?:the )?captcha|solve (?:the )?captcha|verify you are human|security challenge", re.I)
    MFA_TEXT = re.compile(
        r"authenticator app|security key|passkey|text message|send (?:an? )?(?:sms|text)|"
        r"verification code.{0,40}(?:phone|mobile)", re.I | re.S,
    )
    EMAIL_VERIFICATION_TEXT = re.compile(
        r'check your email|verification (?:code|link|email)|verify your (?:email|account)|'
        r'activate your account|account (?:is |has )?not (?:been )?verified|'
        # Do not span unrelated form labels: "Email Address / Password /
        # Verify New Password" is a signup form, not an email challenge.
        r'(?:email|message)[^\n.!?]{0,100}(?:activate|verify(?!\s+(?:new\s+)?password)|verification)', re.I,
    )
    EMAIL_INPUT_SELECTOR = (
        'input:visible:is([type="email"],[autocomplete="email"],[autocomplete="username"],[name*="email" i])'
        ':not([readonly]):not(:disabled):not([aria-hidden="true"]):not([aria-hidden="true"] *)'
        ':not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'
    )

    def __init__(self, store: AccountStore, gmail_client=None, navigator=None):
        self.store = store
        self.gmail_client = gmail_client
        self.navigator = navigator

    @staticmethod
    def _surfaces(page):
        return page.frames if hasattr(page, "frames") else [page]

    @staticmethod
    def _visible(locator):
        try:
            return next((item for item in locator.all() if item.is_visible() and item.is_enabled()), None)
        except Exception:
            return None

    def _body(self, page) -> str:
        pieces = []
        for surface in self._surfaces(page):
            try:
                if surface.locator("body").count():
                    pieces.append(surface.locator("body").inner_text())
            except Exception:
                continue
        return " ".join(pieces)

    def looks_like_login(self, page) -> bool:
        for surface in self._surfaces(page):
            if surface.get_by_role('heading', name=re.compile(
                    r'^(?:register|registration|choose an option to register|'
                    r'create (?:an? |your )?account|inscription|créer (?:un |mon )?compte)$', re.I)).count():
                return True
            if surface.get_by_role('heading', name=re.compile(r"^(?:let.s transition your account|migrate your account|upgrade your account)$", re.I)).count():
                return True
            email_choice = surface.get_by_role('button', name=re.compile(r'^(?:sign in|log in|continue) with email$', re.I))
            if any(control.is_visible() and control.is_enabled() for control in email_choice.all()):
                return True
            if surface.locator('input:visible[type="password"]').count():
                return True
            # A job page commonly has newsletter/email-sharing inputs plus a
            # Sign In header. Only classify an email-only gate when the same
            # local form has an account prompt and a corresponding action.
            for scope in surface.locator('form:visible, [role="dialog"]:visible').all():
                if not scope.locator(self.EMAIL_INPUT_SELECTOR).count() or scope.locator('input[type="file"]').count():
                    continue
                text = scope.inner_text()
                prompt = re.search(r'create (?:an )?account|sign in|log in|enter your email', text, re.I)
                if not prompt:
                    # Many email-first gates put their heading beside the form,
                    # not inside it. Keep this local: a header's Sign In link
                    # must not turn a newsletter field into a login form.
                    parent = scope.locator('..')
                    headings = parent.locator(':scope > h1, :scope > h2, :scope > [role="heading"]').all_text_contents()
                    prompt = any(re.search(r'^(?:log in|sign in|create (?:an )?account|enter your email)', h.strip(), re.I)
                                 for h in headings)
                action = scope.get_by_role('button', name=re.compile(
                    r'^(?:sign in|log in|create (?:an )?account|continue|next)$', re.I))
                if prompt and any(c.is_visible() and c.is_enabled() for c in action.all()):
                    return True
        return False

    def _click_named(self, page, pattern: str, goal=None) -> bool:
        regex = re.compile(pattern, re.I)
        for surface in self._surfaces(page):
            # Use the accessible click target, not Workday's hidden native
            # submit button beneath its click-filter overlay.
            forms = surface.locator('form:visible:has(input[type="password"]), [role="dialog"]:visible')
            for scope in [*reversed(forms.all()), surface]:
                # Workday's accessible click_filter wrapper can consume a
                # pointer click without submitting. Use the form's normal
                # keyboard submission, never DOM submit() or a hidden API.
                if (urlparse(page.url).hostname or '').endswith(('.myworkdayjobs.com', '.myworkdaysite.com')) and scope is not surface:
                    submits = [c for c in scope.locator('button[type="submit"]').all()
                               if regex.search(c.inner_text().strip()) and c.is_enabled()]
                    controls = scope.locator('[data-automation-id="click_filter"][role="button"]:visible')
                    # Some Workday tenants label the wrapper "Submit", while
                    # its associated native button says "Sign In". Match that
                    # generic wrapper only when this scope has one matching
                    # submit button; never fall back to clicking the hidden one.
                    controls = [c for c in controls.all() if regex.search(c.get_attribute('aria-label') or '')
                                or (len(submits) == 1 and (c.get_attribute('aria-label') or '').strip().casefold() == 'submit')]
                    if len(submits) == 1 and len(controls) == 1:
                        controls[0].press('Enter')
                        page.wait_for_timeout(1500)
                        return True
                for role in ('button', 'link'):
                    controls = scope.get_by_role(role, name=regex)
                    matches = [c for c in controls.all() if c.is_visible() and c.is_enabled()]
                    if len(matches) == 1:
                        matches[0].click(timeout=5000)
                        page.wait_for_timeout(1500)
                        return True
        return bool(goal and self.navigator and self.navigator.click(page, goal))

    @staticmethod
    def _set_checked(surface, control, desired):
        if control.is_checked() == desired:
            return
        control_id = control.get_attribute('id')
        label = surface.locator('label[for=' + json.dumps(control_id) + ']') if control_id else None
        if label is not None and label.count() == 1 and label.is_visible():
            label.click(timeout=3000)
        else:
            control.set_checked(desired, timeout=3000)
        if control.is_checked() != desired:
            raise ValueError('Visible account choice did not change to the approved value')

    @staticmethod
    def _consents(surface, profile=None):
        """Never assume every checkbox on a signup form is permission."""
        unknown = []
        for check in surface.locator('input[type="checkbox"]').all():
            label = check.evaluate("el => [...(el.labels || [])].map(x => x.innerText).join(' ') || el.getAttribute('aria-label') || el.parentElement.innerText || ''")
            if not check.is_visible():
                continue
            answer = profile.answer(label, '') if profile else None
            if answer is None:
                answer = application_privacy_answer(label, getattr(profile, 'values', {}))
            required = (check.get_attribute('required') is not None
                        or check.get_attribute('aria-required') == 'true'
                        or check.get_attribute('aria-invalid') == 'true'
                        or check.get_attribute('data-automation-id') == 'createAccountCheckbox')
            if answer in ('Yes', 'Confirmed'):
                AccountAutomation._set_checked(surface, check, True)
            elif answer == 'No':
                AccountAutomation._set_checked(surface, check, False)
                if required:
                    unknown.append('Required consent is declined by saved answer: ' + (label or 'Unlabelled account consent'))
            elif required:
                unknown.append(label or 'Unlabelled required account consent')
        return unknown

    def _inputs(self, page):
        for surface in self._surfaces(page):
            emails = surface.locator(self.EMAIL_INPUT_SELECTOR)
            if not emails.count():
                emails = surface.get_by_label(re.compile(r"email address|email|^username$|^identifiant$", re.I)).and_(
                    surface.locator('input:visible:not([readonly]):not(:disabled):not([aria-hidden="true"]):not([aria-hidden="true"] *):not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"])')
                )
            passwords = surface.locator('input:visible[type="password"]')
            if emails.count() or passwords.count():
                return surface, emails, passwords
        return None, None, None

    @staticmethod
    def _fill_named(surface, selectors, value: str) -> None:
        for selector in selectors:
            control = surface.locator(selector)
            for index in range(control.count()):
                item = control.nth(index)
                if item.is_visible() and item.is_enabled() and item.is_editable():
                    item.fill(value)
                    return

    def _email_verification(self, page, started: int, company: str, account_url: str = '') -> bool:
        if self.gmail_client is None:
            return False
        account = self.store.get(account_url or page.url) or {}
        try:
            message = self.gmail_client.wait_for_verification(
                started, company, timeout_seconds=45, expected_url=account_url or page.url,
                expected_recipient=account.get('email', ''))
        except Exception as exc:
            self.verification_error = 'Gmail verification connection failed (' + type(exc).__name__ + '); reconnect Gmail if authorization expired'
            return False
        if not message:
            return False
        if message.code:
            candidates = []
            for surface in self._surfaces(page):
                controls = surface.locator(
                    'input:visible[name*="code" i], input:visible[id*="code" i], '
                    'input:visible[autocomplete="one-time-code"]'
                )
                split = surface.locator('input:visible[maxlength="1"], input:visible[data-testid*="code" i]')
                if controls.count() == 1 or split.count() in range(4, 9):
                    candidates.append((surface, controls, split))
            if len(candidates) == 1:
                surface, controls, split = candidates[0]
                if controls.count() == 1:
                    controls.first.fill(message.code)
                elif split.count() == len(message.code):
                    for index, character in enumerate(message.code):
                        split.nth(index).fill(character)
                else:
                    return False
                button = self._visible(surface.get_by_role(
                    "button", name=re.compile(r"verify|confirm|continue", re.I)
                ))
                if button is not None:
                    button.click()
                page.wait_for_timeout(2000)
                body = self._body(page)
                return (not self.EMAIL_VERIFICATION_TEXT.search(body)
                        and not re.search(r'(?:code|verification).{0,40}(?:invalid|incorrect|expired|failed)', body, re.I)
                        and self._verification_evidence(page, body))
        # Keep account activation on the same observed ATS host. Do not open a
        # generic verification link from a different employer or a reset email.
        host = (urlparse(account_url or page.url).hostname or '').lower()
        links = [link for link in message.links if urlparse(link).scheme == 'https'
                 and (urlparse(link).hostname or '').lower() == host
                 and self.store.key(link) == self.store.key(account_url or page.url)]
        if len(links) == 1:
            link = links[0]
            response = page.goto(link, wait_until="domcontentloaded", timeout=30000)
            if response and response.status >= 400:
                return False
            page.wait_for_timeout(2000)
            body = self._body(page)
            if re.search(r'(?:link|token).{0,60}(?:expired|invalid)|verification (?:failed|unsuccessful)', body, re.I):
                return False
            if self.EMAIL_VERIFICATION_TEXT.search(body) and not re.search(r'(?:account|email).{0,40}(?:successfully )?(?:verified|activated)', body, re.I):
                return False
            return self._verification_evidence(page, body)
        return False

    def _verification_evidence(self, page, body):
        return bool(re.search(r'(?:account|email).{0,40}(?:successfully )?(?:verified|activated)|'
                              r'(?:verification|activation).{0,20}(?:complete|successful)', body, re.I)
                    or self.looks_like_login(page)
                    or any(s.locator('input[type=file]:visible').count() for s in self._surfaces(page)))

    def _confirmed_session(self, page, login_submitted):
        if login_submitted and not self.looks_like_login(page):
            return True
        return any(self._visible(s.get_by_role(
            "link", name=re.compile(r"^(?:sign out|log out|logout|déconnexion|se déconnecter)$", re.I)))
            or self._visible(s.get_by_role(
                "button", name=re.compile(r"^(?:sign out|log out|logout|déconnexion|se déconnecter)$", re.I)))
            for s in self._surfaces(page))

    def recover_password(self, page, account_url: str) -> AccountResolution:
        """Supported Workday recovery, once per account; never submits a job."""
        host = (urlparse(page.url).hostname or '').lower()
        account = self.store.get(account_url)
        if (not account or not self.gmail_client or self.store.key(page.url) != self.store.key(account_url)
                or not host.endswith(('.myworkdayjobs.com', '.myworkdaysite.com'))):
            return AccountResolution('retry', 'ACCOUNT_RECOVERY_REQUIRED: recovery unavailable for this account')
        body = self._body(page)
        if re.search(r'too many (?:attempts|requests)|account (?:is |has been )locked|access denied|flagged as (?:possible )?spam|temporarily blocked', body, re.I):
            return AccountResolution('retry', 'ACCOUNT_LOCKED: employer restriction requires supported unlock or waiting')
        recovery = account.get('recovery')
        reset_success = r'password.{0,70}(?:successfully|has been).{0,30}(?:reset|changed)|successfully.{0,30}(?:reset|changed).{0,30}password'
        if recovery and recovery['state'] == 'reset_submitting' and re.search(reset_success, body, re.I):
            self.store.recovery_state(account_url, 'completed')
            return AccountResolution('resolved', 'Interrupted reset reconciled from employer confirmation')
        if recovery and recovery['state'] not in {'requested'}:
            return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: prior reset must be reconciled; not resent')
        if not recovery:
            # The sign-in page itself contains a Forgot Password link. Its
            # presence is not evidence that the reset form is already open.
            if (page.locator('input[type="password"]:visible').count()
                    or not re.search(r'Forgot Password', body, re.I)):
                if not self._click_named(page, r'^Forgot (?:your )?password\??$'):
                    return AccountResolution('retry', 'ACCOUNT_RECOVERY_REQUIRED: forgot-password control unavailable')
            emails = page.locator(self.EMAIL_INPUT_SELECTOR)
            if emails.count() != 1 or page.locator('input[type="password"]:visible').count():
                return AccountResolution('retry', 'ACCOUNT_RECOVERY_REQUIRED: ambiguous recovery form')
            emails.first.fill(account['email'])
            recovery = self.store.begin_recovery(account_url)
            if not self._click_named(page, r'^Reset Password$'):
                return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: reset request not confirmed; not resent')
        message = None
        for _ in range(10):
            if self.CAPTCHA_TEXT.search(self._body(page)):
                return AccountResolution('captcha', 'CAPTCHA_REQUIRED: account recovery challenge')
            message = self.gmail_client.find_password_reset(recovery['requested_at'], account_url, account['email'])
            if message:
                break
            page.wait_for_timeout(5000)
        if not message:
            return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: matching reset email has not arrived; request not resent')
        page.goto(message.links[0], wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(1500)
        if self.store.key(page.url) != self.store.key(account_url):
            return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: unexpected reset destination')
        passwords = page.locator('input[type="password"]:visible')
        try:
            passwords.first.wait_for(state='visible', timeout=15000)
        except Exception:
            return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: reset form did not load; request not resent')
        if passwords.count() != 2:
            return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: reset form unavailable or expired')
        for control in passwords.all():
            control.fill(recovery['password'])
        self.store.recovery_state(account_url, 'reset_submitting')
        if not self._click_named(page, r'^(?:Reset Password|Submit|Change Password)$'):
            return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: reset action unavailable')
        for _ in range(15):
            body = self._body(page)
            if re.search(reset_success, body, re.I):
                self.store.recovery_state(account_url, 'completed')
                return AccountResolution('resolved', 'Password reset confirmed by employer; ready for one login test')
            page.wait_for_timeout(1000)
        return AccountResolution('retry', 'ACCOUNT_RECOVERY_PENDING: reset result unconfirmed; not resent')

    def resolve(
        self, page, target_url: str, identity: dict, company: str, return_url: str = "", profile=None
    ) -> AccountResolution:
        if self.navigator:
            account = self.store.get(target_url) or self.store.get(page.url) or {}
            self.navigator.reset(secrets=tuple(identity.values()) + (
                account.get('password'), (account.get('recovery') or {}).get('password')))
        try:
            return self._resolve(page, target_url, identity, company, return_url, profile)
        finally:
            if self.navigator:
                self.navigator.save_audit(target_url)

    def _resolve(
        self, page, target_url: str, identity: dict, company: str, return_url: str = "", profile=None
    ) -> AccountResolution:
        email = str(identity.get("email") or "").strip()
        if not email:
            return AccountResolution("retry", "LOGIN_RETRY_PENDING: application profile has no email")
        started = int(time.time())
        self.verification_error = ''
        account = self.store.get(target_url)
        if account is None:
            # Prefer the actual ATS tenant after a marketing-site redirect.
            target_url = page.url
            account = self.store.get(target_url)
        if account and str(account.get('email') or '').lower() != email.lower():
            return AccountResolution('retry', 'ACCOUNT_IDENTITY_MISMATCH: saved account differs from application profile')
        if account and account.get('legacy_scope_unverified'):
            return AccountResolution('retry', 'ACCOUNT_SCOPE_REVIEW_REQUIRED: legacy shared-host credential has no verified employer binding; no login or duplicate signup attempted')
        attempted_actions = set()
        login_submitted = False
        verification_attempted = False
        application_return_url = return_url or page.url
        workday = 'myworkday' in (urlparse(page.url).hostname or '')
        # Recovery is a durable account state, not a reason to retry the old
        # password on every job sharing this employer account.
        recovery = (account or {}).get('recovery') or {}
        if workday and recovery and recovery.get('state') != 'completed':
            recovered = self.recover_password(page, target_url)
            if recovered.state != 'resolved':
                return recovered
            account = self.store.get(target_url)
            page.goto(application_return_url, wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(1500)
        # Recover a signup from a previous interrupted attempt. Use the saved
        # creation time, not the time we happened to restart the worker.
        if account and account.get('status') in {'pending', 'verification_pending'} and self.gmail_client:
            created_at = int(datetime.fromisoformat(account['created_at']).timestamp())
            try:
                pending_email = self.gmail_client.find_verification(
                    created_at, company, expected_url=page.url, expected_recipient=email)
            except Exception as exc:
                # Optional reconciliation must not prevent an existing-account
                # login. A real verification gate still stays blocked below.
                pending_email = None
                self.verification_error = 'Gmail verification connection failed (' + type(exc).__name__ + '); reconnect Gmail if authorization expired'
            if pending_email and (pending_email.links or pending_email.code):
                self.store.mark_verification_pending(target_url, created_at)
                account = self.store.get(target_url)
                # Reconcile a saved pending account for ANY ATS, not just Workday.
                # Only use the existing same-employer email verifier; never ask
                # an optional answer provider to read codes/links or initiate password recovery.
                if self._email_verification(page, created_at, company, target_url):
                    verification_attempted = True
                    self.store.mark_email_verified(target_url)
                    account = self.store.get(target_url)
                    page.goto(application_return_url, wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_timeout(1500)

        for _ in range(30):
            body = self._body(page)
            if self.CAPTCHA_TEXT.search(body):
                return AccountResolution('captcha', 'CAPTCHA_REQUIRED: solve the visible challenge')
            if re.search(r"let.s transition your account|account on our old platform", body, re.I):
                return AccountResolution('retry', 'ACCOUNT_MIGRATION_REQUIRED: employer requires a one-time account transition; no password changed')
            if re.search(r'\b(?:your )?session (?:has )?(?:expired|timed out)\b', body, re.I):
                if 'refresh-expired-session' in attempted_actions:
                    return AccountResolution('retry', 'SESSION_EXPIRED: employer still reports an expired session after one refresh; no login repeated')
                attempted_actions.add('refresh-expired-session')
                # Ordinary navigation obtains a fresh login page. Never dismiss
                # a security gate by removing overlays or replaying form POSTs.
                page.goto(application_return_url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(1500)
                continue
            if workday and re.search(r'^Loading\s*$', body, re.M):
                page.wait_for_timeout(1000)
                continue
            if not any(surface.locator('input[type="password"]:visible').count() for surface in self._surfaces(page)):
                choice_key = (page.url, 'email-authentication-choice')
                if choice_key not in attempted_actions and self._click_named(page, r'^(?:sign in|log in|continue) with email$', goal='email_choice'):
                    attempted_actions.add(choice_key)
                    continue
            if re.search(r'too many (?:attempts|requests)|access denied|flagged as (?:possible )?spam', body, re.I):
                return AccountResolution('retry', 'ACCOUNT_ACCESS_RESTRICTED: employer access restriction; no login or reset retry')
            if re.search(r'(?:incorrect|wrong|invalid) (?:email(?: address)? (?:or|and) password|password|credentials)|account (?:is |might be )?locked', body, re.I):
                # Stale errors/help text on arrival must not initiate recovery.
                # Require an actual sign-in in this invocation, and an account
                # whose creation/verification was confirmed, not just a locally
                # generated password from an unfinished signup.
                has_login_form = any(surface.locator('input[type="password"]:visible').count()
                                     for surface in self._surfaces(page))
                if login_submitted and workday and account and account.get('status') in {'confirmed', 'email_verified', 'password_reset'} and 'password-recovery' not in attempted_actions:
                    attempted_actions.add('password-recovery')
                    recovered = self.recover_password(page, target_url)
                    if recovered.state != 'resolved':
                        return recovered
                    account = self.store.get(target_url)
                    page.goto(application_return_url, wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_timeout(1500)
                    attempted_actions = {item for item in attempted_actions if not (isinstance(item, tuple) and 'sign-in' in item)}
                    login_submitted = False
                    continue
                if login_submitted or not has_login_form:
                    detail = ('saved account creation/verification is unconfirmed; no password reset initiated'
                              if account and account.get('status') in {'pending', 'verification_pending'}
                              else 'employer rejected authentication; no repeated credential attempts')
                    return AccountResolution('retry', 'LOGIN_RETRY_PENDING: ' + detail)
            if self.CAPTCHA_TEXT.search(body):
                return AccountResolution('captcha', 'CAPTCHA_REQUIRED: solve the visible challenge')
            # The visible employer response, not cached account status, decides
            # whether an email challenge is active. Some ATS sites return to
            # Sign In after signup; attempt that login once and let the site
            # tell us whether activation is actually required.
            if self.EMAIL_VERIFICATION_TEXT.search(body):
                if verification_attempted:
                    return AccountResolution('retry', 'EMAIL_VERIFICATION_REQUIRED: employer still requests verification; account not marked confirmed')
                verification_attempted = True
                requested_at = int((account or {}).get('verification_requested_at')
                                   or (account or {}).get('registration_requested_at') or started)
                if not self._email_verification(page, requested_at, company, page.url):
                    return AccountResolution('retry', 'EMAIL_VERIFICATION_REQUIRED: ' +
                        (self.verification_error or 'no valid matching account email/link available; account remains pending'))
                self.store.mark_email_verified(target_url)
                account = self.store.get(target_url)
                # Verification may sign in automatically, or return to login.
                # Resume the original job, never regard visiting the link as an
                # application submission or a confirmed usable session.
                page.goto(application_return_url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(1500)
                attempted_actions.discard((page.url, 'sign-in'))
                continue
            if (any(surface.locator('input[type="file"]').count() for surface in self._surfaces(page))
                    and not any(surface.locator('input[type="password"]:visible').count()
                                for surface in self._surfaces(page))):
                confirmed = self._confirmed_session(page, login_submitted)
                if confirmed:
                    self.store.mark_confirmed(target_url)
                return AccountResolution("resolved", "Authenticated application form is available" if confirmed
                                         else "Application form available; no account login confirmation inferred")
            if re.search(
                r"current step [2-9]|current step 1 of \d+\s+my information|"
                r"my information.{0,80}current step",
                body, re.I | re.S,
            ):
                if self._confirmed_session(page, login_submitted):
                    self.store.mark_confirmed(target_url)
                return AccountResolution("resolved", "Multi-step application available; account status preserved unless authenticated")
            if self.CAPTCHA_TEXT.search(body):
                return AccountResolution("captcha", "CAPTCHA_REQUIRED: solve the visible challenge")
            if self.MFA_TEXT.search(body) and not re.search(r"email", body, re.I):
                return AccountResolution("mfa", "EXTERNAL_MFA_REQUIRED: phone, passkey, or authenticator proof is required")

            # Workday can keep both create-account and sign-in dialogs mounted
            # during transitions. Target its current-password form explicitly.
            sign_in_forms = page.locator(
                'form[data-automation-id="signInFormo"]:visible:has(input[autocomplete="current-password"]), '
                'form[data-automation-id]:visible:has(input[autocomplete="current-password"])'
            )
            if sign_in_forms.count():
                form = sign_in_forms.last
                if account is None:
                    create_link = page.locator('button[data-automation-id="createAccountLink"]:visible')
                    if create_link.count():
                        create_link.last.click()
                        page.wait_for_timeout(1000)
                        continue
                else:
                    form.locator('input[data-automation-id="email"]').fill(email)
                    form.locator('input[autocomplete="current-password"]').fill(account["password"])
                    action_key = (page.url, 'sign-in')
                    if action_key in attempted_actions:
                        return AccountResolution('retry', 'LOGIN_RETRY_PENDING: sign-in did not advance; credentials not resent')
                    attempted_actions.add(action_key)
                    if not self._click_named(page, r'^Sign In$', goal='submit_login'):
                        return AccountResolution('retry', 'LOGIN_RETRY_PENDING: accessible sign-in control unavailable')
                    login_submitted = True
                    page.wait_for_timeout(3500)
                    continue

            create_forms = page.locator('form:visible:has(input[data-automation-id="email"]):has(input[autocomplete="new-password"])')
            if create_forms.count():
                form = create_forms.last
                if account and account.get("status") in {'confirmed', 'email_verified', 'password_reset'}:
                    sign_in_link = page.locator('button[data-automation-id="signInLink"]:visible')
                    if sign_in_link.count():
                        sign_in_link.last.click()
                        page.wait_for_timeout(1000)
                        continue
                account = account or self.store.get_or_create(target_url, email)
                if account.get('stage') in {'registration_submitting', 'registration_submitted'}:
                    return AccountResolution('retry', 'ACCOUNT_REGISTRATION_PENDING: prior signup needs verification; not resubmitted')
                if self.navigator:
                    self.navigator.secrets += (account['password'],)
                form.locator('input[data-automation-id="email"]').fill(email)
                new_passwords = form.locator('input[autocomplete="new-password"]')
                for index in range(new_passwords.count()):
                    new_passwords.nth(index).fill(account["password"])
                unknown = self._consents(form, profile)
                if unknown:
                    return AccountResolution('retry', 'Required account choices need answers: ' + '; '.join(unknown))
                action_key = (page.url, 'create-account')
                if action_key in attempted_actions:
                    return AccountResolution('retry', 'LOGIN_RETRY_PENDING: account creation did not advance; not resent')
                attempted_actions.add(action_key)
                requested_at = int(time.time())
                self.store.registration_event(target_url, 'registration_submitting')
                if not self._click_named(page, r'^Create (?:an )?Account$', goal='submit_registration'):
                    return AccountResolution('retry', 'LOGIN_RETRY_PENDING: accessible create-account control unavailable')
                self.store.registration_event(target_url, 'registration_submitted')
                page.wait_for_timeout(3500)
                after = self._body(page)
                if self.EMAIL_VERIFICATION_TEXT.search(after):
                    self.store.mark_verification_pending(target_url, requested_at)
                    account = self.store.get(target_url)
                elif form.count() and form.is_visible():
                    errors = form.locator('[role="alert"], [data-automation-id="errorMessage"]').all_text_contents()
                    invalid = form.locator('[aria-invalid="true"]').count()
                    if errors or invalid:
                        self.store.registration_event(target_url, 'registration_rejected')
                        return AccountResolution('retry', 'ACCOUNT_VALIDATION_REQUIRED: signup rejected; no verification email assumed. '
                                                 + '; '.join(errors or ['Required signup field or consent is invalid']))
                continue

            surface, emails, passwords = self._inputs(page)
            password_count = passwords.count() if passwords is not None else 0
            if surface is not None and (emails.count() or password_count):
                if password_count >= 2 and account and account.get("status") in {'confirmed', 'email_verified'}:
                    if self._click_named(page, r"^(?:sign in|log in)$", goal='open_login'):
                        continue
                if emails.count():
                    for index in range(emails.count()):
                        item = emails.nth(index)
                        if item.is_visible() and item.is_enabled() and item.is_editable():
                            item.fill(email)

                # Two password fields reliably indicate account creation. With one,
                # use a stored account or first select the provider's signup path.
                creating = password_count >= 2
                if password_count == 1 and account is None:
                    if self._click_named(page, r"^(?:create (?:an )?account|register|sign up)(?: now)?$", goal='open_registration'):
                        continue
                    creating = passwords.first.get_attribute('autocomplete') == 'new-password'
                    if not creating:
                        return AccountResolution('retry', 'LOGIN_RETRY_PENDING: no saved credentials or recognized signup form; no password guessed')
                if password_count:
                    account = account or self.store.get_or_create(target_url, email)
                    if creating and account.get('stage') in {'registration_submitting', 'registration_submitted'}:
                        return AccountResolution('retry', 'ACCOUNT_REGISTRATION_PENDING: prior signup needs verification; not resubmitted')
                    if self.navigator:
                        self.navigator.secrets += (account['password'],)
                    for index in range(password_count):
                        passwords.nth(index).fill(account["password"])

                self._fill_named(surface, (
                    'input:visible[autocomplete="given-name"]', 'input:visible[name*="first" i]'
                ), str(identity.get("first_name") or ""))
                self._fill_named(surface, (
                    'input:visible[autocomplete="family-name"]', 'input:visible[name*="last" i]'
                ), str(identity.get("last_name") or ""))

                unknown = self._consents(surface, profile)
                if unknown:
                    return AccountResolution('retry', 'Required account choices need answers: ' + '; '.join(unknown))
                if creating:
                    action = r"^(?:create (?:an )?account|register|sign up|continue|next)$"
                else:
                    action = r"^(?:sign in|log in|continue|next)$"
                action_key = (page.url, action, password_count)
                if action_key in attempted_actions:
                    return AccountResolution('retry', 'LOGIN_RETRY_PENDING: account step did not advance; not resent')
                attempted_actions.add(action_key)
                if creating:
                    self.store.registration_event(target_url, 'registration_submitting')
                if not self._click_named(page, action, goal=(
                        'submit_registration' if creating else
                        'submit_login' if password_count else 'continue_email')):
                    return AccountResolution("retry", "LOGIN_RETRY_PENDING: account action control was not recognized")
                if creating:
                    self.store.registration_event(target_url, 'registration_submitted')
                if password_count == 1 and not creating:
                    login_submitted = True
                page.wait_for_timeout(1500)
                continue

            if not self.looks_like_login(page):
                # Do not click a site's persistent Sign In header after a
                # successful login. Resume the job if activation landed home.
                if page.url != application_return_url and 'resume-job' not in attempted_actions:
                    attempted_actions.add('resume-job')
                    page.goto(application_return_url, wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_timeout(1500)
                    continue
                if workday:
                    # Workday first paints an empty shell after navigation.
                    # A missing login form is not evidence of either success
                    # or failure; wait for the authenticated step to mount.
                    page.wait_for_timeout(1000)
                    continue
                return AccountResolution('resolved', 'Authentication gate cleared; returning to application navigation')
            if self._click_named(page, r"^(?:create (?:an )?account|register|sign up)(?: now)?$", goal='open_registration'):
                continue
            if self._click_named(page, r"^(?:sign in|log in)$", goal='open_login'):
                continue
            return AccountResolution("retry", "LOGIN_RETRY_PENDING: login form variant was not recognized")

        return AccountResolution("retry", "LOGIN_RETRY_PENDING: account flow did not settle")
