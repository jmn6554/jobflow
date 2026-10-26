# Security

This is a local, single-user application. Do not expose the dashboard to the
internet, a shared LAN, or a reverse proxy. It has no multi-user authentication.

Do not include applicant data, résumés, API keys, cookies, OAuth clients, email
tokens, screenshots or local account-store files in issue reports. Use synthetic
data and redact logs. Revoke any credential accidentally published before
attempting to clean Git history.

Saved browser profiles and account data can grant access to external accounts.
Filesystem permissions are not encryption. Keep the computer protected and do not
share its runtime directories. Review each employer's terms and required consents.

Automated submission is best-effort. Preserve uncertain-submission and CAPTCHA
holds rather than blindly retrying; a timeout can occur after an employer has
already received an application.
