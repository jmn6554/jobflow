"""Command-line entry point for local job discovery and review."""

import argparse
import os
import json
import shlex
from datetime import datetime
from pathlib import Path

from .discovery import DiscoveryService
from .drafting import create_drafts
from .eligibility import evaluate, score as fit_score
from .linkedin_alerts import LinkedInAlertImporter
from .notifications import send_discovery_email
from .providers import LinkedInLinkProvider, configured_providers
from .repository import Repository, STATUSES
from .resume import FakeTailoringProvider, ResumeService, configured_tailoring_provider, configured_base_resume
from .scheduler import launchd_plist, should_run
from .scoring import load_profile, score_job
from .sources import CSVJobSource
from .verification import VerificationService, prepare_verified_jobs
from .autopilot import ApplicationProfile, AssistedApplicationService, BulkApplicationService


def parser():
    root = argparse.ArgumentParser(prog="jobflow", description="Local-first job application assistant")
    root.add_argument("--db", default=os.environ.get("JOBFLOW_DB", "data/jobflow.db"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the local database")
    strategy = commands.add_parser('strategy-report', help='rank saved jobs and report conversions/rescue candidates; no applications or discovery')
    strategy.add_argument('--output', default='data/strategy-latest.json')
    strategy.add_argument('--prepare-outreach', action='store_true', help='create up to ten draft-only outreach items; never searches or sends')
    targets = commands.add_parser('audit-target-companies', help='read-only integration audit for all approved Canada remote company targets')
    targets.add_argument('--manifest', default='config/canada-remote-targets.json')
    targets.add_argument('--output', default='data/canada-remote-coverage.json')
    target_pull = commands.add_parser('discover-target-companies', help='discover the approved target companies through supported feeds; never applies')
    target_pull.add_argument('--manifest', default='config/canada-remote-targets.json')
    target_apply = commands.add_parser('apply-target-companies', help='preview or apply only existing target-company jobs; no discovery or retries')
    target_apply.add_argument('--manifest', default='config/canada-remote-targets.json')
    target_apply.add_argument('--limit', type=int, default=50)
    target_apply.add_argument('--confirm', choices=['APPLY TO TARGET COMPANIES'])
    remaining_apply = commands.add_parser('apply-remaining', help='preview/apply untouched Ashby, Greenhouse, and Canada-remote jobs; no discovery or retries')
    remaining_apply.add_argument('--limit', type=int, default=500)
    remaining_apply.add_argument('--approved-job-id', type=int, action='append', default=[], help='explicit user-approved preference exception for this exact job')
    remaining_apply.add_argument('--approved-batch', help='JSON snapshot of exact user-approved job IDs; no selection filters or unrelated jobs')
    remaining_apply.add_argument('--continue-on-holds', action='store_true', help='preserve per-job CAPTCHA/uncertain holds and continue to distinct jobs')
    remaining_apply.add_argument('--confirm', choices=['APPLY TO REMAINING'])

    import_csv = commands.add_parser("import-csv", help="import, deduplicate, gate, and rank a CSV")
    import_csv.add_argument("path")
    listing = commands.add_parser("list", help="list jobs")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--queue", choices=("qualified", "estimated_pay_review", "rejected", "pending"))

    discover = commands.add_parser("discover", help="run all configured compliant discovery sources")
    discover.add_argument("--email", action="store_true", help="send the configured summary email")
    recovery = commands.add_parser('recover-discovery', help='live-revalidate and import missing audit candidates; never applies')
    recovery.add_argument('report', help='completed policy-comparison JSON')
    recovery.add_argument('--output', required=True, help='local JSON recovery report')
    commands.add_parser('priority-search', help='collect the priority tech/energy companies and write a shortlist; never applies')
    linkedin = commands.add_parser("gather-linkedin", help="gather LinkedIn posting URLs for manual review only")
    linkedin.add_argument("--pages", type=int, default=int(os.environ.get("JOBFLOW_LINKEDIN_PAGES", "1")))
    alerts = commands.add_parser("import-linkedin-alerts", help="import links from read-only Gmail job-alert emails")
    alerts.add_argument("--days", type=int, default=14)
    browser_links = commands.add_parser('collect-linkedin-browser', help='collect rendered LinkedIn cards in signed-in Chrome; never applies')
    browser_links.add_argument('--url', help='LinkedIn search URL; default uses the current search tab')
    browser_links.add_argument('--latest-alert', action='store_true', help='read the newest Gmail alert and open its search-results link')
    browser_links.add_argument('--endpoint', help='local Chrome connection; default reads the application profile port')
    browser_links.add_argument('--pages', type=int, default=1)
    browser_links.add_argument('--details', type=int, default=5)
    browser_links.add_argument('--delay', type=float, default=5)
    preferred = commands.add_parser('search-linkedin', help='run saved preferred searches in signed-in Chrome; never applies')
    preferred.add_argument('--endpoint', help='local Chrome connection')
    preferred.add_argument('--preview', action='store_true', help='print saved search URLs without opening the browser')
    preferred.add_argument('--search-limit', type=int, help='bound this run; default all saved searches')
    resolver = commands.add_parser('resolve-linkedin', help='match stored LinkedIn alerts to direct ATS records; never submits')
    resolver.add_argument('--limit', type=int, default=1000)
    resolver.add_argument('--web-limit', type=int, default=20, help='maximum alerts to search publicly; 0 is inventory-only')
    pipeline = commands.add_parser("pipeline-run", help="discover, verify eligibility, and rank without creating resumes")
    pipeline.add_argument("--email", action="store_true", help="send the configured summary email")
    commands.add_parser("scheduled-run", help="compatibility alias for daily-run")
    daily = commands.add_parser("daily-run", help="run the consolidated daily workflow once per Toronto calendar day")
    daily.add_argument("--preview", action="store_true", help="show existing candidates without fetching or applying")
    commands.add_parser("daily-status", help="show the latest daily report and application queue")
    retry = commands.add_parser('daily-requeue', help='requeue a proven pre-submit failure without erasing history')
    retry.add_argument('job_id', type=int)

    apply_all = commands.add_parser("apply-all", help="attempt every non-rejected job using one shared resume")
    apply_all.add_argument("--resume", default=os.environ.get("JOBFLOW_APPLICATION_RESUME", "private-data/resume.docx"))
    apply_all.add_argument("--profile", default=os.environ.get("JOBFLOW_APPLICATION_PROFILE", "private-data/application-profile.json"))
    apply_all.add_argument("--limit", type=int, default=2000)
    apply_all.add_argument("--confirm", required=True, help="must be APPLY TO ALL")
    apply_all.add_argument("--retry-manual", action="store_true", help="retry jobs previously stopped for manual action")
    apply_all.add_argument(
        "--strong-only", action="store_true",
        help="apply only to verified direct postings with a strong resume-skill match",
    )

    assist = commands.add_parser("assist", help="open one qualified job in a persistent user-assisted browser")
    assist.add_argument("job_id", type=int)
    assist.add_argument("--resume", default=os.environ.get("JOBFLOW_APPLICATION_RESUME", "private-data/resume.docx"))
    assist.add_argument("--profile", default=os.environ.get("JOBFLOW_APPLICATION_PROFILE", "private-data/application-profile.json"))
    assist.add_argument("--browser-profile", default=os.environ.get("JOBFLOW_BROWSER_PROFILE", "private-data/browser-profile"))
    assist.add_argument(
        "--native-chrome", action=argparse.BooleanOptionalAction,
        default=os.environ.get("JOBFLOW_ASSIST_NATIVE_CHROME", "true").lower() in {"1", "true", "yes", "on"},
        help="prefill in regular Chrome, then detach automation before CAPTCHA and submission",
    )
    assist.add_argument("--timeout", type=int, default=1800)
    assist.add_argument("--auto-submit", action=argparse.BooleanOptionalAction, default=False,
                        help="submit a completed native Chrome form once, leaving CAPTCHA to the user")
    assist.add_argument(
        "--override-eligibility", action="store_true",
        help="allow a user-directed one-time assisted attempt for a non-qualified job",
    )

    verify = commands.add_parser("verify-jobs", help="resolve and validate ranked jobs from search indexes")
    verify.add_argument("--limit", type=int, default=int(os.environ.get("JOBFLOW_VERIFICATION_LIMIT", "2000")))
    verify.add_argument("--workers", type=int, default=int(os.environ.get("JOBFLOW_VERIFICATION_WORKERS", "8")))
    verify.add_argument("--prepare", action="store_true", help="also generate job-specific resumes for verified matches")

    prepare = commands.add_parser("prepare-resume", help="generate a one-page DOCX/PDF only for a chosen job")
    prepare.add_argument("job_id", type=int)
    prepare.add_argument("--base-resume", default=configured_base_resume())
    prepare.add_argument("--artifacts", default=os.environ.get("JOBFLOW_ARTIFACTS", "artifacts"))
    prepare.add_argument("--fake", action="store_true", help="test the formatting pipeline without calling a model")

    serve = commands.add_parser("serve", help="start the local review dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8741)
    serve.add_argument("--base-resume", default=configured_base_resume())
    serve.add_argument("--artifacts", default=os.environ.get("JOBFLOW_ARTIFACTS", "artifacts"))

    plist = commands.add_parser("launchd-config", help="write an optional daily 8:00 a.m. macOS launch-agent file (do not combine with the Codex schedule)")
    plist.add_argument("output")
    plist.add_argument("--minute", type=int, default=0)

    # Compatibility commands retained from the first prototype.
    score = commands.add_parser("score", help="score one or all jobs with a JSON profile")
    score_target = score.add_mutually_exclusive_group(required=True)
    score_target.add_argument("--job-id", type=int)
    score_target.add_argument("--all", action="store_true")
    score.add_argument("--profile", required=True)
    draft = commands.add_parser("draft", help="create local, review-required notes")
    draft.add_argument("job_id", type=int)
    draft.add_argument("--profile", required=True)
    draft.add_argument("--output", required=True)
    track = commands.add_parser("track", help="record an application status")
    track.add_argument("job_id", type=int)
    track.add_argument("--status", required=True, choices=STATUSES)
    track.add_argument("--note", default="")
    track.add_argument("--follow-up-days", type=int)
    track.add_argument("--confirm-human-approved", action="store_true")
    reminders = commands.add_parser("reminders", help="list due reminders")
    reminders.add_argument("--through")
    return root


def _discover(repo: Repository, email: bool) -> int:
    providers = configured_providers(known_jobs=repo.jobs(limit=100000))
    if not providers:
        raise ValueError("No discovery providers configured; see .env.example")
    run_id = DiscoveryService(repo, providers).run()
    run = repo.discovery_run(run_id)
    sent = send_discovery_email(run, repo.surfaced_jobs(30)) if email else False
    print("Discovery {}: {} qualified, {} need evidence{}".format(
        run_id, run["qualified_count"], run["review_count"], "; email sent" if sent else ""
    ))
    return run_id


def _pipeline(repo: Repository, email: bool = False):
    """Run automated stages 1-5 and deliberately stop before resume creation."""
    run_id = _discover(repo, False)
    report = VerificationService(
        repo, int(os.environ.get("JOBFLOW_VERIFICATION_WORKERS", "8"))
    ).run(int(os.environ.get("JOBFLOW_VERIFICATION_LIMIT", "2000")))
    run = repo.discovery_run(run_id)
    sent = send_discovery_email(run, repo.surfaced_jobs(30)) if email else False
    print(
        "Pipeline {}: checked {}; resolved {}; qualified {}; rejected {}; "
        "still need evidence {}; errors {}{}".format(
            run_id, report.checked, report.resolved, report.qualified, report.rejected,
            report.still_review, report.errors, "; email sent" if sent else "",
        )
    )
    return run_id, report


def run(args) -> int:
    repo = Repository(args.db)
    repo.init()
    if args.command == "init":
        print("Initialized {}".format(args.db))
    elif args.command == 'apply-target-companies':
        from .daily import DailyService
        print(json.dumps(DailyService(repo).run_targets(args.manifest, confirm=bool(args.confirm), limit=args.limit), indent=2))
    elif args.command == 'apply-remaining':
        from .daily import DailyService
        approved_ids = tuple(args.approved_job_id)
        if args.approved_batch:
            if approved_ids:
                raise ValueError('Use a batch or individual approvals, not both')
            batch = json.loads(Path(args.approved_batch).read_text())
            approved_ids = tuple(batch['job_ids'])
            if not approved_ids or any(type(i) is not int or i <= 0 for i in approved_ids) or len(set(approved_ids)) != len(approved_ids):
                raise ValueError('Approved batch IDs must be distinct positive integers')
        print(json.dumps(DailyService(repo).run_targets(None, confirm=bool(args.confirm), limit=args.limit,
                         remaining=True, approved_job_ids=approved_ids, approved_only=bool(args.approved_batch),
                         continue_on_holds=args.continue_on_holds), indent=2))
    elif args.command == "import-csv":
        added = duplicate = 0
        for job in CSVJobSource(args.path).fetch():
            job_id, is_new, _ = repo.upsert_job(job)
            repo.save_evaluation(job_id, evaluate(job), fit_score(job))
            added += int(is_new)
            duplicate += int(not is_new)
        print("Imported {}; updated/skipped {} duplicate(s)".format(added, duplicate))
    elif args.command == "list":
        rows = repo.jobs(args.status, args.queue)
        for row in rows:
            print("{id:>3}  {score:>3}  {queue:<21}  {title} — {company}".format(
                id=row["id"], score=row["fit_score"] if row["fit_score"] is not None else "-",
                queue=row["eligibility_queue"], title=row["title"], company=row["company"]
            ))
        print("{} job(s)".format(len(rows)))
    elif args.command == 'recover-discovery':
        from .recovery import recover_candidates
        result = recover_candidates(repo, json.loads(Path(args.report).read_text()))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k not in {'results', 'discovery_runs'}}, indent=2))
        print('Recovery report: ' + str(output.resolve()))
    elif args.command == 'strategy-report':
        from .strategy import build_report, prepare_outreach
        result = build_report(repo)
        if args.prepare_outreach:
            result['outreach_draft_ids'] = prepare_outreach(repo, result)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
        temporary.chmod(0o600)
        temporary.replace(output)
        print(json.dumps({k: result[k] for k in ('new_candidates', 'strong_candidates', 'manual_rescue_count', 'reconciliation_count', 'metrics')}, indent=2))
        print('Report: ' + str(output.resolve()))
        print('No applications, discovery, paid searches or messages sent. Schedules unchanged.')
    elif args.command == "discover":
        _discover(repo, args.email)
    elif args.command == 'discover-target-companies':
        from .company_coverage import load_targets, target_providers, FACTORIES
        rows = load_targets(args.manifest)
        providers = target_providers(rows)
        try:
            run_id = DiscoveryService(repo, providers, reevaluate_stored=False).run()
        finally:
            for provider in providers:
                provider.client.close()
        print(json.dumps(dict(repo.discovery_run(run_id)), indent=2))
        print('Not yet integrated: ' + ', '.join(r['company'] for r in rows if r['provider'] not in FACTORIES))
        print('No applications started; schedules unchanged.')
    elif args.command == 'audit-target-companies':
        from .company_coverage import load_targets, audit_targets, write_report
        report = audit_targets(repo, load_targets(args.manifest))
        write_report(report, args.output)
        print(json.dumps(report['summary'], indent=2))
        print('Read-only coverage report: ' + str(Path(args.output).resolve()))
    elif args.command == 'priority-search':
        from .priority_search import PriorityDiscovery
        report = PriorityDiscovery(repo).run()
        print(json.dumps({'potential_matches': len(report['shortlist']), 'needs_review': len(report['review']),
            'excluded': len(report['excluded_jobs']), 'applications_started': 0,
            'report': str(repo.path.parent / 'priority-shortlist.md')}, indent=2))
    elif args.command == "gather-linkedin":
        key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required to gather LinkedIn links")
        provider = LinkedInLinkProvider(key, pages=args.pages)
        try:
            run_id = DiscoveryService(repo, [provider], reevaluate_stored=False).run()
        finally:
            provider.client.close()
        run = repo.discovery_run(run_id)
        if run["state"] != "completed":
            print("LinkedIn run {} failed: {}".format(run_id, run["error_summary"]))
            return 1
        print("LinkedIn run {}: {} indexed links; {} surfaced for review; open /linkedin in the dashboard".format(
            run_id, run["discovered_count"], run["qualified_count"] + run["review_count"]
        ))
    elif args.command == "import-linkedin-alerts":
        report = LinkedInAlertImporter(repo).run(args.days)
        print("LinkedIn alerts: {} emails; {} links; {} imported; {} already known; {} filtered".format(
            report.emails_checked, report.links_found, report.imported, report.existing, report.filtered,
        ))
        for title, company, url in report.new_links:
            print("{} — {}\n{}".format(title, company, url))
    elif args.command == 'search-linkedin':
        from .linkedin_search import search_plan, run
        if args.preview:
            print(json.dumps(search_plan(), indent=2))
            return 0
        if not args.endpoint:
            raise ValueError('Pass the active application Chrome --endpoint')
        report = run(repo, args.endpoint, args.search_limit)
        print(json.dumps(report, indent=2))
        return 1 if report['state'] == 'stopped' else 0
    elif args.command == 'collect-linkedin-browser':
        from .linkedin_browser import LinkedInBrowserCollector, latest_alert_search_url
        if args.latest_alert and args.url:
            raise ValueError('Choose --latest-alert or --url, not both')
        endpoint = args.endpoint
        if not endpoint:
            port_file = Path('private-data/native-chrome-profile/DevToolsActivePort')
            if not port_file.exists():
                raise ValueError('Application Chrome needs a local debugging connection; see DAILY_FLOW.md')
            endpoint = 'http://127.0.0.1:' + port_file.read_text().splitlines()[0]
        url = latest_alert_search_url() if args.latest_alert else args.url
        result = LinkedInBrowserCollector(repo).run(endpoint, url, args.pages, args.details, args.delay)
        return 1 if result['state'] in {'stopped', 'no_cards_or_layout_changed'} else 0
    elif args.command == 'resolve-linkedin':
        from .linkedin_resolver import LinkedInEmployerResolver
        print(json.dumps(LinkedInEmployerResolver(repo).run(args.limit, args.web_limit), indent=2))
    elif args.command == "pipeline-run":
        _pipeline(repo, args.email)
    elif args.command in {"daily-run", "scheduled-run", "daily-status", "daily-requeue"}:
        from .daily import DailyService
        daily = DailyService(repo)
        result = (daily.requeue(args.job_id) if args.command == 'daily-requeue' else
                  daily.status() if args.command == 'daily-status' else daily.run(getattr(args, 'preview', False)))
        print(json.dumps(result, indent=2))
    elif args.command == "apply-all":
        raise ValueError('Legacy bulk runner retired. Use daily-run or daily-run --preview for the shared queue.')
        if args.confirm != "APPLY TO ALL":
            raise ValueError("Bulk application requires the exact confirmation APPLY TO ALL")
        report = BulkApplicationService(repo).run(
            Path(args.resume), ApplicationProfile.load(Path(args.profile)), args.limit, args.retry_manual,
            args.strong_only,
        )
        print(
            "Considered {considered}; submitted {submitted}; already submitted {skipped_existing}; "
            "manual {manual_action_required}; confirmation unknown {confirmation_unknown}; "
            "blocked {blocked}; failed {failed}".format(**report.__dict__)
        )
    elif args.command == "assist":
        decision = AssistedApplicationService(repo, Path(args.browser_profile)).run(
            args.job_id, Path(args.resume), ApplicationProfile.load(Path(args.profile)), args.timeout,
            args.override_eligibility, args.native_chrome, args.auto_submit,
        )
        print("{}: {}{}".format(
            decision.state, decision.reason, " ({})".format(decision.receipt) if decision.receipt else ""
        ))
    elif args.command == "verify-jobs":
        report = VerificationService(repo, args.workers).run(args.limit)
        print(
            "Checked {checked}; resolved {resolved}; qualified {qualified}; rejected {rejected}; "
            "still review {review}; errors {errors}".format(
                checked=report.checked, resolved=report.resolved, qualified=report.qualified,
                rejected=report.rejected, review=report.still_review, errors=report.errors,
            )
        )
        print("Preparable job IDs: {}".format(
            ",".join(str(job_id) for job_id in report.preparable_job_ids) or "none"
        ))
        if args.prepare:
            prepared, failures = prepare_verified_jobs(
                repo, report.preparable_job_ids,
                Path(configured_base_resume()),
                Path(os.environ.get("JOBFLOW_ARTIFACTS", "artifacts")),
            )
            print("Prepared {} resume(s); {} failed".format(len(prepared), len(failures)))
    elif args.command == "prepare-resume":
        provider = FakeTailoringProvider() if args.fake else configured_tailoring_provider()
        artifact_id = ResumeService(repo, Path(args.base_resume), Path(args.artifacts), provider).prepare(args.job_id)
        artifact = repo.artifact(artifact_id)
        print("Prepared resume {}\nDOCX: {}\nPDF: {}".format(artifact_id, artifact["docx_path"], artifact["pdf_path"]))
    elif args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("The dashboard is local-only; bind to 127.0.0.1, localhost, or ::1")
        from .dashboard import create_app
        import uvicorn

        uvicorn.run(create_app(args.db, args.base_resume, args.artifacts), host=args.host, port=args.port)
    elif args.command == "launchd-config":
        if not (0 <= args.minute <= 59):
            raise ValueError("Invalid minute")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(launchd_plist(Path.cwd(), minute=args.minute), encoding="utf-8")
        print("Wrote {}. Review it, then install with launchctl bootstrap.".format(output))
    elif args.command == "score":
        profile = load_profile(args.profile)
        rows = repo.jobs() if args.all else [repo.job(args.job_id)]
        for row in rows:
            result = score_job(dict(row), profile)
            repo.save_score(row["id"], result)
            print("{}: {}/100 ({})".format(row["id"], result.score, result.explanation))
    elif args.command == "draft":
        profile = load_profile(args.profile)
        resume, cover = create_drafts(dict(repo.job(args.job_id)), profile)
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "resume-notes.md").write_text(resume, encoding="utf-8")
        (output / "cover-letter.md").write_text(cover, encoding="utf-8")
        print("Created review-required drafts in {}".format(output))
    elif args.command == "track":
        if args.follow_up_days is not None and args.follow_up_days < 0:
            raise ValueError("--follow-up-days must be zero or greater")
        if args.status == "submitted" and not args.confirm_human_approved:
            raise ValueError("Recording submitted requires --confirm-human-approved")
        repo.transition(args.job_id, args.status, args.note, args.follow_up_days)
        print("Job {} is now {}".format(args.job_id, args.status))
    elif args.command == "reminders":
        rows = repo.reminders(args.through)
        for row in rows:
            print("{}  job {}  {} — {}  ({})".format(
                row["due_date"], row["job_id"], row["title"], row["company"], row["kind"]
            ))
        print("{} reminder(s)".format(len(rows)))
    return 0


def main():
    # Load literal .env assignments without executing shell code or printing secrets.
    env_path = Path('.env')
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip().removeprefix('export ')
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            parts = shlex.split(value, comments=True)
            os.environ.setdefault(key.strip(), ' '.join(parts))
    try:
        raise SystemExit(run(parser().parse_args()))
    except (OSError, ValueError) as exc:
        raise SystemExit("error: {}".format(exc))


if __name__ == "__main__":
    main()
