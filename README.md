# JobFlow

A local-first job-search and application workbench: collect employer postings,
deduplicate them by posting identity, review matches, assist with application forms,
and keep an audit trail of submissions and the exact résumé uploaded.

This is an **experimental, single-user desktop tool**, extracted from a personal
workflow—not a hosted service or a promise of fully autonomous applications.
Employers change their forms; unanswered questions and site restrictions need
human attention. An attempt is not a confirmed submission.

## Included

- Greenhouse, Lever and Ashby discovery adapters, plus additional experimental sources.
- SQLite job history, duplicate protection and a local review dashboard.
- Saved-answer form assistance, browser sessions and optional Gmail verification.
- Unchanged résumé uploads with retained file snapshots.
- Separate states for confirmed, unconfirmed, interrupted and CAPTCHA-held attempts.

No personal résumé, applicant answers, application history, cookies, credentials,
or local-model integration is included. Model-assisted application answers and
optional cloud résumé editing are disabled by default. Using the optional cloud
features sends applicant information to that provider and may incur charges.

## Quick start (macOS/Linux, Python 3.11+)

Run from the repository directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[browser,dev]'
python -m playwright install chromium
cp .env.example .env
mkdir -p private-data
cp examples/application-profile.example.json private-data/application-profile.json
jobflow init
```

1. Edit `.env` with the employer boards you want to search. A Greenhouse URL such
   as `https://job-boards.greenhouse.io/example` uses board token `example`.
   Set entries as `Company:token`, or use a CSV registry with the columns shown
   in `examples/boards.example.csv`. No paid API key is needed for these ATS feeds.
2. Edit `config/daily.json` for your search. The existing role taxonomy targets
   data, analytics, systems, business-analysis and integration roles in Canada/US.
   It is **not a general worldwide job-search engine**. Office locations, salary
   thresholds, languages and supported skills are configurable. The original
   North-American eligibility heuristics remain conservative and should be reviewed
   before use outside that scope; scores are not a hiring probability.
3. Put your own DOCX résumé at `private-data/resume.docx`. Complete the private
   application profile using truthful information. Empty fields mean unknown,
   never an assumed “No.” No sample employment history is submitted for you.
4. Discover and review before approving applications:

```sh
jobflow discover
jobflow list
jobflow serve
```

The dashboard is at `http://127.0.0.1:8741`. Stop it with Ctrl+C. Keep it on
localhost; it is not designed for public hosting or multiple users.

## Application assistance

Start with one reviewed job, using the ID shown in `jobflow list`:

```sh
jobflow assist 123 --no-auto-submit --no-native-chrome
```

This opens a visible dedicated browser. Review the answers and submit yourself.
Use `--auto-submit` only when you explicitly want the tool to submit completed
forms. Batch commands have confirmation arguments; read `jobflow --help` and
the relevant command's `--help` first. Do not waive eligibility just to increase
application volume. Do not retry an unconfirmed submission without checking it.

The portable saved-answer engine deliberately does **not** include the original
user's personal question templates. It handles basic identity and narrow
work-authorization labels, plus exact saved question/answer mappings. For example,
in your private profile:

```json
{
  "additional_answers": {"How did you hear about us?": "Company careers site"},
  "employer_answers": {
    "Example Employer": {"What is your notice period?": "Your truthful answer"}
  }
}
```

Explicit employer answers take precedence. Save consent or sensitive disclosure
choices only after reading the actual question. Unknown questions stop that
application for review. CAPTCHA and anti-abuse restrictions are not bypassed.
Use each site's permitted access methods and respect rate limits.

## Privacy and security

Keep `.env`, `private-data/`, `data/`, `artifacts/`, `outputs/`, browser profiles and
résumés out of Git. These are ignored, but always review `git diff --cached`
before committing. Browser sessions, email tokens and saved account credentials
are sensitive local files, **not an encrypted password vault**. Protect the machine
with disk encryption and a strong login. Never publish screenshots or debug logs
without reviewing them for applicant information. Do not reuse one password
across employers.

Gmail is optional. Supply your own OAuth desktop client; never share a token or
client file. No email access or paid search is required for basic discovery.
No schedule is installed automatically. `daily-run` is an optional explicit
workflow, not permission to repeatedly submit applications.

## Development

```sh
pytest
```

Tests use synthetic applicant data and mocked providers; they do not submit real
applications. The public export does not include personal one-off repair scripts,
downloaded third-party company datasets, or historical reports. Add board tokens
yourself and review any dataset's license before redistribution.

The first release is a source checkout workflow. Use it from this directory;
configuration files are not currently bundled as wheel package data.
