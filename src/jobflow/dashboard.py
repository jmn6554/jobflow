"""Local-only FastAPI dashboard for review, tailoring, and guarded submission."""

import html
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .applications import ApplicationService, provider_for_url
from .discovery import DiscoveryService, configured_result_limit
from .gmail import GmailConfigurationError, GmailVerificationClient
from .linkedin_alerts import LinkedInAlertImporter
from .providers import aggregator_url, configured_providers, prohibited_automation_url, public_http_url
from .repository import Repository, STATUSES
from .resume import FakeTailoringProvider, ResumeService, configured_tailoring_provider, configured_base_resume
from .verification import VerificationService, prepare_verified_jobs
from .daily import DailyService
from .policy import skill_threshold_met


CSS = """
:root{--ink:#17212b;--muted:#637083;--line:#d8e0e8;--paper:#fff;--wash:#f3f6f8;
--navy:#16324f;--teal:#087e8b;--amber:#b76808;--red:#a23838;--green:#237a57}
*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:16px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
a{color:#075f83}header{background:var(--navy);color:#fff;padding:18px 0}header a{color:#fff;text-decoration:none}
.shell{width:min(1180px,calc(100% - 32px));margin:auto}.brand{display:flex;justify-content:space-between;align-items:center;gap:24px}.brand strong{font-size:22px}.brand nav{display:flex;gap:18px}
main{padding:30px 0 60px}.toolbar,.grid,.metrics{display:grid;gap:16px}.toolbar{grid-template-columns:1fr auto;align-items:end}.metrics{grid-template-columns:repeat(4,1fr);margin:18px 0}.metric,.panel,.job,.tracker-card{background:var(--paper);border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 8px #18334b10}.metric{padding:18px}.metric b{display:block;font-size:28px}.metric span,.muted{color:var(--muted)}
.grid{grid-template-columns:2fr 1fr}.panel{padding:22px;margin-bottom:18px}.job{padding:18px;margin:12px 0;display:grid;grid-template-columns:1fr auto;gap:16px}.job h3{margin:0 0 5px}.company{font-weight:650;margin-bottom:7px}.meta,.keyterms{display:flex;flex-wrap:wrap;gap:8px;color:var(--muted)}.keyterms{align-items:center;margin-top:10px}.keyterms b{color:var(--ink);font-size:14px}
.pill{border-radius:999px;background:#e7eef5;padding:3px 9px;font-size:14px}.qualified{background:#dff3e8;color:#155a3d}.review{background:#fff0d7;color:#7d4700}.rejected,.missing{background:#f8dddd;color:#772b2b}.score{font-size:25px;font-weight:700;color:var(--navy);text-align:right}.match{font-size:14px;font-weight:700;color:var(--green);margin-top:5px}.tracker{display:grid;gap:14px}.tracker-card{padding:18px}.tracker-card h3{margin:0}.tracker-form{display:grid;grid-template-columns:180px 1fr 130px auto;gap:10px;align-items:end;margin-top:14px}.tracker-form label{margin:0}.tracker-form button{white-space:nowrap}.small-button{padding:7px 10px;font-size:14px}
button,.button{display:inline-block;border:0;border-radius:8px;background:var(--teal);color:#fff;padding:10px 14px;font:inherit;font-weight:650;text-decoration:none;cursor:pointer}.secondary{background:#e7eef5;color:var(--navy)}.danger{background:var(--red)}input,textarea,select{width:100%;border:1px solid #b9c5d0;border-radius:8px;padding:10px;font:inherit;background:#fff}textarea{min-height:130px}label{display:block;margin:12px 0 5px;font-weight:650}.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}.actions form{margin:0}
pre.jd{white-space:pre-wrap;font:16px/1.55 ui-sans-serif,system-ui;margin:0}.notice{padding:12px 15px;border-left:4px solid var(--teal);background:#eaf5f6;margin:14px 0}.error{border-color:var(--red);background:#fae8e8}.diff{display:grid;grid-template-columns:1fr 1fr;gap:14px}.before,.after{padding:14px;border-radius:8px;white-space:pre-wrap}.before{background:#fff1f1}.after{background:#e9f6ef}iframe{width:100%;height:780px;border:1px solid var(--line);border-radius:8px;background:#fff}details{margin:10px 0}code{font-size:14px}@media(max-width:800px){.metrics{grid-template-columns:1fr 1fr}.grid,.toolbar,.job,.diff,.tracker-form{grid-template-columns:1fr}.score{text-align:left}.brand{align-items:flex-start;flex-direction:column}}
.brand nav{flex-wrap:wrap}.panel,.notice{overflow-wrap:anywhere}
"""


def e(value) -> str:
    return html.escape(str(value or ""))


def page(title: str, body: str) -> HTMLResponse:
    body = '<p><a href="/strategy">Focused search</a> · <a href="/daily">Daily flow and application queue</a> · <a href="/outreach">Outreach queue</a></p>' + body
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · JobFlow</title><style>{css}</style></head><body><header><div class="shell brand"><strong>JobFlow Local</strong><nav><a href="/">Jobs</a><a href="/linkedin">LinkedIn links</a><a href="/applications">Applications</a><a href="/exceptions">Exceptions</a><a href="/settings">Settings</a></nav></div></header><main class="shell">{body}</main></body></html>""".format(title=e(title), css=CSS, body=body))


def money(row) -> str:
    if row["salary_max"] is None:
        return "Salary undisclosed"
    minimum = "${:,.0f}–".format(row["salary_min"]) if row["salary_min"] is not None else "Up to "
    suffix = " estimated" if row["salary_estimated"] else ""
    return "{}${:,.0f} {}{}".format(minimum, row["salary_max"], row["salary_currency"], suffix)


def _json(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _strong_match(row) -> bool:
    components = _json(row["score_components"], {})
    canonical = row["canonical_url"]
    return (
        bool(canonical)
        and public_http_url(canonical)
        and not prohibited_automation_url(canonical)
        and not aggregator_url(canonical)
        and len(row["description"].strip()) >= 500
        and skill_threshold_met(components.get("skill_match_percent", 0))
    )


def create_app(db_path="data/jobflow.db", base_resume=None, artifact_root="artifacts") -> FastAPI:
    base_resume = base_resume or configured_base_resume()
    app = FastAPI(title="JobFlow Local", docs_url=None, redoc_url=None)
    from .key_settings import install_key_settings
    install_key_settings(app, Path.cwd() / '.env')
    repo = Repository(db_path)
    repo.init()
    from .outreach_dashboard import install_outreach
    install_outreach(app, repo, page, e, Path('private-data/application-profile.json'))
    from .strategy_dashboard import install_strategy
    install_strategy(app, repo, page, e)

    @app.get('/daily', response_class=HTMLResponse)
    def daily_page():
        status = DailyService(repo).status()
        latest = status['latest']
        report = json.loads(latest['report']) if latest else {}
        title = 'Daily flow — 8 a.m. Eastern'
        body = '<h1>' + title + '</h1><p>Collect → screen → queue → apply → verify → report.</p>'
        body += '<p>Greenhouse and Lever: automatic applications in regular Chrome. Ashby and LinkedIn: links to open yourself.</p>'
        body += '<p>Resume: <b>' + e(Path(status['policy']['resume']).name) + '</b>. OpenAI calls disabled.</p>'
        body += '<div class="actions"><form method="post" action="/daily/run"><button>Run today’s flow</button></form>'
        body += '<a class="button secondary" href="' + e(status['policy']['remote_access_url']) + '">Open remote Mac for CAPTCHA</a></div>'
        body += '<p>Latest run: ' + e(latest['day'] + ' — ' + latest['state'] if latest else 'Not yet run') + '</p>'
        for error in report.get('errors', []):
            body += '<div class="notice error">' + e(error) + '</div>'
        with repo.connect() as db:
            rows = db.execute('''SELECT q.*,j.title,j.company,s.state AS submission_state,s.detail AS submission_detail
                FROM daily_queue q JOIN jobs j ON j.id=q.job_id
                LEFT JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE job_id=j.id)
                ORDER BY q.updated_at DESC LIMIT 200''').fetchall()
        for row in rows:
            state = row['submission_state'] or row['state']
            body += '<article class="panel"><b>' + e(row['company']) + ' — ' + e(row['title']) + '</b><p>' + e(state) + '</p><p>' + e(row['submission_detail'] or row['detail']) + '</p></article>'
        for link in report.get('links', []):
            body += '<p><a href="' + e(link['url']) + '">' + e(link['company'] + ' — ' + link['title']) + '</a></p>'
        return page('Daily flow', body)

    @app.post('/daily/run')
    def start_daily():
        log_path = Path(db_path).resolve().parent / 'daily.log'
        with log_path.open('a') as log:
            subprocess.Popen([sys.executable, '-u', '-m', 'jobflow.cli', '--db', str(Path(db_path).resolve()), 'daily-run'],
                             cwd=str(Path.cwd()), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        return RedirectResponse('/daily', status_code=303)

    def resume_service():
        if os.environ.get("JOBFLOW_FAKE_TAILORING", "").lower() in {"1", "true", "yes"}:
            provider = FakeTailoringProvider()
        else:
            provider = configured_tailoring_provider()
        return ResumeService(repo, Path(base_resume), Path(artifact_root), provider)

    @app.get("/linkedin", response_class=HTMLResponse)
    def linkedin_links(message: str = "", error: str = ""):
        rows = repo.linkedin_links(100)
        cards = "".join(
            '<article class="job"><div><h3>{title}</h3><div class="company">{company}</div>'
            '<div class="meta"><span>{location}</span><span class="pill review">Link only · details unverified</span>'
            '<span class="pill">{salary}</span></div><div class="actions">'
            '<a class="button" href="{url}" target="_blank" rel="noopener noreferrer">Open LinkedIn posting</a>'
            '<a class="button secondary" href="/jobs/{id}">View in JobFlow</a></div></div>'
            '<div class="score">{score}<small>/100</small></div></article>'.format(
                title=e(row["title"]), company=e(row["company"]), location=e(row["location"]),
                salary=e(money(row)), url=e(row["url"]), id=row["id"], score=row["fit_score"] or 0,
            ) for row in rows
        )
        notice = '<div class="notice">{}</div>'.format(e(message)) if message else ""
        notice += '<div class="notice error">{}</div>'.format(e(error)) if error else ""
        body = ('<h1>LinkedIn application links</h1>' + notice +
                '<p class="muted">Collected from your read-only Gmail job alerts or the public search index. '
                'Open each posting to confirm it is active, the work location, salary and requirements. '
                'JobFlow does not open or submit LinkedIn forms automatically.</p>'
                '<form method="post" action="/linkedin/import"><button type="submit">Import alert emails now</button></form>'
                + (cards or '<div class="panel">No matching LinkedIn links gathered yet.</div>'))
        return page("LinkedIn links", body)

    @app.post("/linkedin/import")
    def import_linkedin_alerts():
        try:
            report = LinkedInAlertImporter(repo).run()
            message = "Checked {} alert emails; imported {} links; {} already known; {} filtered.".format(
                report.emails_checked, report.imported, report.existing, report.filtered,
            )
            return RedirectResponse("/linkedin?message=" + quote(message), status_code=303)
        except (OSError, ValueError, GmailConfigurationError) as exc:
            return RedirectResponse("/linkedin?error=" + quote(str(exc)), status_code=303)
        except Exception:
            return RedirectResponse(
                "/linkedin?error=" + quote("Gmail import failed; check the connected account and retry."),
                status_code=303,
            )

    @app.get("/", response_class=HTMLResponse)
    def home(message: str = "", error: str = "", strong: int = 0):
        pool = repo.surfaced_jobs(configured_result_limit())
        strong_count = sum(
            1 for row in pool if _strong_match(row)
        )
        surfaced = [
            row for row in pool
            if not strong or _strong_match(row)
        ][:30]
        qualified = [row for row in surfaced if row["eligibility_queue"] == "qualified"]
        review = [row for row in surfaced if row["eligibility_queue"] == "estimated_pay_review"]
        runs = repo.discovery_runs(5)
        cards = []
        for row in surfaced:
            queue_class = "qualified" if row["eligibility_queue"] == "qualified" else "review"
            components = _json(row["score_components"], {})
            terms = components.get("matched_skills", components.get("matched_terms", []))[:8]
            missing = components.get("missing_required", [])[:4]
            term_html = "".join('<span class="pill">{}</span>'.format(e(term)) for term in terms)
            missing_html = "".join('<span class="pill missing">{}</span>'.format(e(term)) for term in missing)
            match_percent = components.get("skill_match_percent", 0)
            skill_count = len(components.get("jd_skills", []))
            confidence = components.get("skill_confidence", "insufficient")
            direct_url = row["canonical_url"] or ""
            original_url = row["url"] or ""
            if public_http_url(direct_url) and not aggregator_url(direct_url) and not prohibited_automation_url(direct_url):
                route = "Direct · {}".format(provider_for_url(direct_url).replace("_", " ").title())
                route_class = "qualified"
            elif prohibited_automation_url(original_url):
                route = "User-assisted board"
                route_class = "review"
            else:
                route = "Employer URL needed"
                route_class = "review"
            track_action = (
                '<form method="post" action="/jobs/{}/track"><button class="secondary small-button" type="submit">Track job</button></form>'.format(row["id"])
                if row["status"] == "new" else
                '<a class="button secondary small-button" href="/applications">Tracked: {}</a>'.format(e(row["status"].title()))
            )
            cards.append("""<article class="job"><div><h3><a href="/jobs/{id}">{title}</a></h3><div class="company">Company: {company}</div><div class="meta"><span>{location}</span><span class="pill {queue_class}">{queue}</span><span class="pill">{workplace}</span><span class="pill">{salary}</span><span class="pill {route_class}">{route}</span></div><div class="keyterms"><b>Matched skills:</b>{terms}</div>{missing_block}<div class="actions">{track_action}</div></div><div><div class="score">{score}<small>/100</small></div><div class="match">{match_percent}% skill match</div><div class="muted">{confidence}</div></div></article>""".format(
                id=row["id"], title=e(row["title"]), company=e(row["company"]), location=e(row["location"]),
                queue_class=queue_class, queue=e(row["eligibility_queue"].replace("_", " ")), workplace=e(row["workplace_type"]),
                salary=e(money(row)), terms=term_html or '<span class="muted">None extracted</span>', score=row["fit_score"] or 0,
                match_percent=match_percent, confidence=e("{} · {} detected skill{}".format(
                    confidence, skill_count, "" if skill_count == 1 else "s"
                )),
                missing_block='<div class="keyterms"><b>Missing required:</b>{}</div>'.format(missing_html) if missing_html else "",
                track_action=track_action, route=e(route), route_class=route_class,
            ))
        last = runs[0] if runs else None
        board_summaries = repo.discovery_board_summary(last["id"]) if last else []
        board_audit = "".join(
            '<p><b>{provider} boards:</b> {boards} checked, {failed} failed, {partial} incomplete, '
            '{index_count} title candidates, {emitted_count} postings returned for screening. '
            '<a href="/discovery/boards/{run_id}">View failures</a></p>'.format(
                provider=e(row["provider"].title()), boards=row["boards"], failed=row["failed"],
                partial=row['partial'],
                index_count=row["index_count"], emitted_count=row["emitted_count"],
                run_id=last["id"],
            ) for row in board_summaries
        )
        notice = '<div class="notice">{}</div>'.format(e(message)) if message else ""
        notice += '<div class="notice error">{}</div>'.format(e(error)) if error else ""
        providers = configured_providers(known_jobs=repo.jobs(limit=100000))
        provider_count = len(providers)
        for provider in providers:
            provider.client.close()
        filter_action = '<a class="button secondary" href="/">Show all</a>' if strong else '<a class="button secondary" href="/?strong=1">Verified postings ({})</a>'.format(strong_count)
        body = """<div class="toolbar"><div><h1>Application workspace</h1><p class="muted">One daily flow collects, filters, and queues jobs. Greenhouse/Lever applications use your unchanged resume; Ashby/LinkedIn links appear in the daily report.</p></div><div class="actions">{filter_action}<a href="/daily">Daily queue and results</a><form method="post" action="/daily/run"><button type="submit">Run daily flow</button></form></div></div>{notice}
        <section class="metrics"><div class="metric"><b>{qualified}</b><span>qualified</span></div><div class="metric"><b>{review}</b><span>needs evidence</span></div><div class="metric"><b>{providers}</b><span>configured sources</span></div><div class="metric"><b>{new_jobs}</b><span>new jobs last run</span></div></section>
        <div class="grid"><section><h2>Ranked jobs</h2>{cards}</section><aside><div class="panel"><h2>Application routes</h2><p><b>Direct</b> means the original employer or ATS page was verified and can enter guarded automation.</p><p><b>User-assisted board</b> means LinkedIn, Indeed, or Glassdoor: open the link and complete it yourself. JobFlow does not automate those sites.</p><p><b>Employer URL needed</b> remains blocked until resolution succeeds.</p></div><div class="panel"><h2>Recent runs</h2>{runs}{board_audit}</div></aside></div>""".format(
            qualified=len(qualified), review=len(review), providers=provider_count,
            new_jobs=repo.new_jobs_in_discovery_run(last["id"]) if last else 0, notice=notice,
            cards="".join(cards) or '<div class="panel"><p>No surfaced jobs yet. Configure a source in Settings, or import the example CSV.</p></div>',
            runs="".join("<p><b>{}</b><br><span class=\"muted\">{} · {} matched · {} new</span></p>".format(
                e(run["state"]), e(run["started_at"][:16]), run["discovered_count"],
                repo.new_jobs_in_discovery_run(run["id"])
            ) for run in runs) or "<p class=\"muted\">No runs yet.</p>",
            board_audit=board_audit,
            filter_action=filter_action,
        )
        return page("Jobs", body)

    @app.get("/discovery/boards/{run_id}", response_class=HTMLResponse)
    def discovery_boards(run_id: int):
        run = repo.discovery_run(run_id)
        summary = repo.discovery_board_summary(run_id)
        failures = repo.failed_discovery_boards(run_id)
        summary_html = "".join(
            "<p>{provider}: {boards} boards checked, {failed} failed, {partial} incomplete; {raw} raw postings, "
            "{index} title candidates, {emitted} postings returned for screening.</p>".format(
                provider=e(row["provider"].title()), boards=row["boards"], failed=row["failed"],
                partial=row['partial'],
                raw=row["raw_count"], index=row["index_count"], emitted=row["emitted_count"],
            ) for row in summary
        )
        failure_html = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                e(row["company"]), e(row["board_token"]), e(row["error"])
            ) for row in failures
        )
        body = ('<p><a href="/">← Back to jobs</a></p><div class="panel"><h1>Discovery board audit</h1>'
                '<p>Run {} · {}</p>{}<h2>Failed or incomplete boards</h2>{}</div>').format(
                    run_id, e(run["started_at"][:16]), summary_html or "<p>No board details recorded.</p>",
                    '<table><tr><th>Company</th><th>Board token</th><th>Error</th></tr>{}</table>'.format(failure_html)
                    if failures else '<p>No failed or incomplete boards recorded.</p>',
                )
        return page("Discovery board audit", body)

    @app.post("/discover")
    def discover():
        providers = configured_providers(known_jobs=repo.jobs(limit=100000))
        if not providers:
            return RedirectResponse("/?error=" + quote("No discovery source is configured. See Settings."), status_code=303)
        run_id = DiscoveryService(repo, providers).run()
        report = VerificationService(repo).run()
        return RedirectResponse("/?message=" + quote(
            "Pipeline run {} completed; checked {}, resolved {}, and ranked all eligible results. No resumes were created.".format(
                run_id, report.checked, report.resolved
            )
        ), status_code=303)

    @app.post("/verify")
    def verify_jobs():
        report = VerificationService(repo).run()
        return RedirectResponse("/?message=" + quote(
            "Checked {} unresolved jobs; resolved {} and reranked them. No resumes were created.".format(
                report.checked, report.resolved
            )
        ), status_code=303)

    @app.post("/jobs/{job_id}/track")
    def track_job(job_id: int):
        try:
            row = repo.job(job_id)
            if row["status"] == "new":
                repo.transition(job_id, "reviewing", "Added to application tracker")
            return RedirectResponse("/applications?message=" + quote("Job added to tracker."), status_code=303)
        except ValueError as exc:
            return RedirectResponse("/?error=" + quote(str(exc)), status_code=303)

    @app.get("/exceptions", response_class=HTMLResponse)
    def exceptions(message: str = "", error: str = ""):
        latest = {}
        for submission in repo.submissions():
            latest.setdefault(submission["job_id"], submission)
        cards = []
        counts = {"confirmation_unknown": 0, "blocked": 0, "manual_action_required": 0, "failed": 0}
        for row in repo.jobs(queue="qualified", limit=2000):
            submission = latest.get(row["id"])
            state = submission["state"] if submission else "manual_action_required"
            if state == "submitted":
                continue
            if state in counts:
                counts[state] += 1
            detail = submission["detail"] if submission else "Not attempted yet"
            url = row["canonical_url"] or row["url"]
            action = '<a class="button secondary" href="{}" rel="noreferrer">Open application ↗</a>'.format(e(url)) if public_http_url(url) else ""
            if (
                state == "blocked" and "captcha" in (detail or "").lower()
                and public_http_url(row["canonical_url"] or "")
                and not prohibited_automation_url(row["canonical_url"]) and not aggregator_url(row["canonical_url"])
            ):
                action += '<form method="post" action="/exceptions/{}/assist"><button type="submit">Solve CAPTCHA</button></form>'.format(row["id"])
            cards.append("""<article class="tracker-card"><div class="toolbar"><div><h3><a href="/jobs/{id}">{title}</a></h3><div class="company">{company}</div><div class="meta"><span>{location}</span><span class="pill review">{state}</span></div></div><div class="score">{score}<small>/100</small></div></div><p>{detail}</p><div class="actions">{action}</div></article>""".format(
                id=row["id"], title=e(row["title"]), company=e(row["company"]), location=e(row["location"]),
                state=e(state.replace("_", " ")), score=row["fit_score"] or 0, detail=e(detail), action=action,
            ))
        notice = '<div class="notice">{}</div>'.format(e(message)) if message else ""
        notice += '<div class="notice error">{}</div>'.format(e(error)) if error else ""
        body = """<div class="toolbar"><div><h1>Automation exceptions</h1><p class="muted">CAPTCHAs are the normal user-action handoff. Login, account creation, saved sessions, and email verification run automatically.</p></div><a class="button secondary" href="/">Back to jobs</a></div>{notice}<section class="metrics"><div class="metric"><b>{unknown}</b><span>confirmation unknown</span></div><div class="metric"><b>{blocked}</b><span>CAPTCHA</span></div><div class="metric"><b>{manual}</b><span>unsupported identity/form issue</span></div><div class="metric"><b>{failed}</b><span>automatic retry</span></div></section><section class="tracker">{cards}</section>""".format(
            unknown=counts["confirmation_unknown"], blocked=counts["blocked"],
            manual=counts["manual_action_required"], failed=counts["failed"],
            cards="".join(cards) or '<div class="panel"><p>No qualified-job exceptions.</p></div>',
            notice=notice,
        )
        return page("Exceptions", body)

    @app.post("/exceptions/{job_id}/assist")
    def start_assisted_session(job_id: int):
        try:
            row = repo.job(job_id)
            if row["eligibility_queue"] != "qualified":
                raise ValueError("Assisted mode is limited to qualified jobs")
            latest = repo.submissions(job_id)
            if latest and latest[0]["state"] in {"submitted", "confirmation_unknown"}:
                raise ValueError("This application is already submitted or awaiting confirmation; it will not be reopened")
            url = row["canonical_url"] or ""
            if not public_http_url(url) or prohibited_automation_url(url) or aggregator_url(url):
                raise ValueError("A verified direct employer/ATS URL is required")
            project_root = Path.cwd().resolve()
            log_path = project_root / "data" / "assisted-browser.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable, "-m", "jobflow.cli", "--db", str(Path(db_path).resolve()),
                "assist", str(job_id),
                "--resume", os.environ.get("JOBFLOW_APPLICATION_RESUME", "private-data/resume.docx"),
                "--profile", os.environ.get("JOBFLOW_APPLICATION_PROFILE", "private-data/application-profile.json"),
                "--browser-profile", os.environ.get("JOBFLOW_NATIVE_BROWSER_PROFILE", "private-data/native-chrome-profile"),
                "--native-chrome",
            ]
            with log_path.open("a", encoding="utf-8") as log:
                subprocess.Popen(
                    command, cwd=str(project_root), env=os.environ.copy(), stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            return RedirectResponse(
                "/exceptions?message=" + quote(
                    "Regular Chrome opened and was prefilled. Automation detaches before CAPTCHA; solve it and submit manually."
                ),
                status_code=303,
            )
        except ValueError as exc:
            return RedirectResponse("/exceptions?error=" + quote(str(exc)), status_code=303)

    @app.get("/applications", response_class=HTMLResponse)
    def applications(message: str = "", error: str = ""):
        tracked = [row for row in repo.jobs(limit=100000) if row["status"] != "new"]
        tracked.sort(key=lambda row: row["updated_at"], reverse=True)
        counts = {status: sum(1 for row in tracked if row["status"] == status) for status in STATUSES}
        notice = '<div class="notice">{}</div>'.format(e(message)) if message else ""
        notice += '<div class="notice error">{}</div>'.format(e(error)) if error else ""
        cards = []
        editable_statuses = ("reviewing", "preparing", "submitted", "interviewing", "offer", "rejected", "withdrawn", "new")
        for row in tracked:
            options = "".join(
                '<option value="{}"{}>{}</option>'.format(
                    e(status), " selected" if status == row["status"] else "",
                    "Remove from tracker" if status == "new" else e(status.title()),
                ) for status in editable_statuses
            )
            applied_form = "" if row["status"] == "submitted" else """<form class="tracker-form" method="post" action="/applications/{id}/applied"><label>Applied externally<input name="note" placeholder="Optional note"></label><label>Confirmation<input value="I submitted this application" disabled></label><label>Follow up (days)<input type="number" name="follow_up_days" min="0" max="365" value="7"></label><button type="submit">Mark applied</button></form>""".format(id=row["id"])
            cards.append("""<article class="tracker-card"><div class="toolbar"><div><h3><a href="/jobs/{id}">{title}</a></h3><div class="company">{company}</div><div class="meta"><span>{location}</span><span class="pill">{status}</span><span>{updated}</span></div></div><div class="score">{score}<small>/100</small></div></div><form class="tracker-form" method="post" action="/applications/{id}/status"><label>Status<select name="status">{options}</select></label><label>Note<input name="note" placeholder="Interview date, contact, outcome…"></label><label>Follow up (days)<input type="number" name="follow_up_days" min="0" max="365"></label><button class="secondary" type="submit">Update</button></form>{applied_form}</article>""".format(
                id=row["id"], title=e(row["title"]), company=e(row["company"]), location=e(row["location"]),
                status=e(row["status"].title()), updated=e(row["updated_at"][:10]), score=row["fit_score"] or 0,
                options=options, applied_form=applied_form + '<p><a href="/jobs/{}#application-resumes">Application résumé history</a></p>'.format(row["id"]),
            ))
        body = """<div class="toolbar"><div><h1>Application tracker</h1><p class="muted">Track every application and update its status, notes, and follow-up timing.</p></div><a class="button secondary" href="/">Find jobs</a></div>{notice}<section class="metrics"><div class="metric"><b>{total}</b><span>tracked</span></div><div class="metric"><b>{submitted}</b><span>applied</span></div><div class="metric"><b>{interviewing}</b><span>interviewing</span></div><div class="metric"><b>{offers}</b><span>offers</span></div></section><section class="tracker">{cards}</section>""".format(
            total=len(tracked), submitted=counts["submitted"], interviewing=counts["interviewing"],
            offers=counts["offer"], notice=notice,
            cards="".join(cards) or '<div class="panel"><h2>No tracked jobs yet</h2><p>Add a job from the ranked-jobs dashboard.</p></div>',
        )
        return page("Applications", body)

    @app.post("/applications/{job_id}/status")
    def application_status(job_id: int, status: str = Form(...), note: str = Form(""), follow_up_days: str = Form("")):
        try:
            if status == "submitted" and repo.job(job_id)["status"] != "submitted":
                raise ValueError("Use Mark applied to confirm a submitted application")
            days = int(follow_up_days) if follow_up_days.strip() else None
            if days is not None and not 0 <= days <= 365:
                raise ValueError("Follow-up days must be between 0 and 365")
            repo.transition(job_id, status, note, days)
            return RedirectResponse("/applications?message=" + quote("Application status updated."), status_code=303)
        except ValueError as exc:
            return RedirectResponse("/applications?error=" + quote(str(exc)), status_code=303)

    @app.post("/applications/{job_id}/applied")
    def mark_applied(job_id: int, note: str = Form(""), follow_up_days: int = Form(7)):
        try:
            if not 0 <= follow_up_days <= 365:
                raise ValueError("Follow-up days must be between 0 and 365")
            repo.transition(job_id, "submitted", note or "Manually confirmed external application", follow_up_days)
            return RedirectResponse("/applications?message=" + quote("Application marked as submitted."), status_code=303)
        except ValueError as exc:
            return RedirectResponse("/applications?error=" + quote(str(exc)), status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(job_id: int, message: str = "", error: str = ""):
        try:
            row = repo.job(job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        artifacts = repo.artifacts(job_id)
        warnings = _json(row["eligibility_warnings"], [])
        reasons = _json(row["eligibility_reasons"], [])
        components = _json(row["score_components"], {})
        notices = '<p><a class="button secondary" href="/outreach?job_id={}">Prepare LinkedIn outreach</a></p>'.format(job_id)
        notices += '<div class="notice">{}</div>'.format(e(message)) if message else ""
        notices += '<div class="notice error">{}</div>'.format(e(error)) if error else ""
        artifact_html = []
        attempts = []
        for submission in repo.submissions(job_id):
            attachment = repo.submission_resume(submission["id"])
            link = ('<a class="button secondary" href="/submissions/{}/resume">Download résumé for this attempt — {}</a>'.format(
                submission["id"], e(attachment["original_filename"])) if attachment else
                '<span class="muted">No exact résumé was recorded for this attempt.</span>')
            attempts.append('<li><p>{} · {} · Attempt #{}</p>{}</li>'.format(
                e(submission["attempted_at"][:19]), e(submission["state"].replace("_", " ")),
                submission["id"], link))
        artifact_html.append('<div class="panel" id="application-resumes"><h2>Application résumé history</h2>'
                             '<p>Saved copies are kept separately from editable drafts. An attempt may be blocked '
                             'before upload; only a confirmed submission means the employer accepted the application.</p>'
                             + ('<ul>' + ''.join(attempts) + '</ul>' if attempts else '<p>No application attempts yet.</p>')
                             + '</div>')
        for artifact in artifacts:
            report = _json(artifact["change_report"], {})
            changes = report.get("changes", [])
            diff = "".join("""<details><summary>{id} · {reason}</summary><div class="diff"><div class="before"><b>Before</b><br>{before}</div><div class="after"><b>After</b><br>{after}</div></div></details>""".format(
                id=e(change.get("evidence_id")), reason=e(change.get("reason")), before=e(change.get("before")), after=e(change.get("after"))
            ) for change in changes) or '<p class="muted">No wording changes in this version.</p>'
            if report.get("proposed_numerical_claims"):
                diff += '<h4>Proposed numerical claims — not included in the résumé</h4><p>Awaiting your specific confirmation.</p>'
                diff += "".join("<p><b>{id}</b>: {wording}<br>{question}</p>".format(
                    id=e(item.get("evidence_id", "")), wording=e(item.get("proposed_wording", "")),
                    question=e(item.get("confirmation_question", "")))
                    for item in report["proposed_numerical_claims"])
            artifact_html.append("""<div class="panel"><h3>Resume v{version} <span class="pill qualified">ready</span></h3><div class="actions"><a class="button" href="/artifacts/{id}/file/docx">Download DOCX</a><a class="button secondary" href="/artifacts/{id}/file/pdf">Download PDF</a></div><details><summary>ATS terms and gaps</summary><p><b>Terms:</b> {terms}</p><p><b>Gaps:</b> {gaps}</p></details><h4>Change report</h4>{diff}<h4>PDF preview</h4><iframe title="Resume PDF preview" src="/artifacts/{id}/file/pdf"></iframe>
            <h4>Application approval</h4><p class="muted">Paste the reviewed field-to-answer mapping as JSON. Approval is bound to this resume, these answers, the canonical URL, and the reviewed field names. Sensitive demographic or identity questions remain manual-only.</p><form method="post" action="/jobs/{job_id}/approve"><input type="hidden" name="artifact_id" value="{id}"><label>Reviewed fields (JSON object)</label><textarea name="fields_json" placeholder='{{"Full name":"Your reviewed answer"}}' required></textarea><label>Type SUBMIT to approve this exact package</label><input name="typed_confirmation" autocomplete="off" required><button type="submit">Approve package</button></form></div>""".format(
                version=artifact["version"], id=artifact["id"], terms=e(", ".join(_json(artifact["ats_terms"], [])) or "None"),
                gaps=e("Not assessed by local wording editor" if report.get("gaps_assessed") is False else (", ".join(_json(artifact["gaps"], [])) or "None")),
                diff=diff + "".join("<p>Original retained for {id}: {reason}</p>".format(
                    id=e(item.get("evidence_id", "")), reason=e(item.get("reason", "")))
                    for item in report.get("skipped_edits", [])), job_id=job_id,
            ))
        preparation_blocked = (
            not row["canonical_url"]
            or not public_http_url(row["canonical_url"])
            or aggregator_url(row["canonical_url"])
            or prohibited_automation_url(row["canonical_url"])
            or row["eligibility_queue"] not in {"qualified", "estimated_pay_review"}
        )
        prepare = '<div class="notice error">A verified employer/ATS posting and an eligible decision are required before preparing a resume.</div>' if preparation_blocked else '<form method="post" action="/jobs/{}/prepare"><button type="submit">Prepare application</button></form>'.format(job_id)
        status_options = "".join(
            '<option value="{}"{}>{}</option>'.format(
                e(status), " selected" if status == row["status"] else "", e(status.title())
            )
            for status in STATUSES if status != "submitted"
        )
        salary_evidence = "{}{}".format(
            row["salary_estimate_source"] or "posting/provider field",
            " · confidence " + row["salary_estimate_confidence"] if row["salary_estimate_confidence"] else "",
        )
        posting_url = (row["canonical_url"] or row["url"]) if public_http_url(row["canonical_url"] or row["url"]) else "#"
        assisted = prohibited_automation_url(row["url"] or "") and not row["canonical_url"]
        posting_label = "Open board manually ↗" if assisted else "Open verified employer/ATS posting ↗"
        route_notice = '<div class="notice">This restricted-board application is user-assisted; automation will not control or submit on this site.</div>' if assisted else ""
        body = """<p><a href="/">← Back to jobs</a></p>{notices}{route_notice}<div class="toolbar"><div><h1>{title}</h1><p>{company} · {location}</p></div><div class="score">{score}<small>/100</small></div></div><div class="meta"><span class="pill">{salary}</span><span class="pill">{workplace}</span><span class="pill">{employment}</span><a class="button secondary" href="{url}" rel="noreferrer">{posting_label}</a></div>
        <div class="grid" style="margin-top:22px"><section><div class="panel"><h2>Job description</h2><pre class="jd">{jd}</pre></div>{artifacts}</section><aside><div class="panel"><h2>Decision</h2><p><b>{queue}</b></p><p>{explanation}</p><p><b>Warnings</b><br>{warnings}</p><p><b>Exclusions</b><br>{reasons}</p>{prepare}</div><div class="panel"><h2>Eligibility evidence</h2><p><b>Salary:</b> {salary}</p><p><b>Salary source:</b> {salary_evidence}</p><p><b>Language:</b> {language}</p><p><b>Sponsorship:</b> {sponsorship}</p><p><b>Clearance:</b> {clearance}</p></div><div class="panel"><h2>Application status</h2><form method="post" action="/jobs/{job_id}/status"><select name="status">{status_options}</select><label>Note</label><input name="note"><button type="submit">Update status</button></form><p class="muted">Submitted status is set only after a confirmed ATS receipt, or from the CLI after human confirmation.</p></div><div class="panel"><h2>Score components</h2>{components}</div></aside></div>""".format(
            notices=notices, title=e(row["title"]), company=e(row["company"]), location=e(row["location"]), score=row["fit_score"] or 0,
            salary=e(money(row)), workplace=e(row["workplace_type"]), employment=e(row["employment_type"]),
            url=e(posting_url), posting_label=e(posting_label), route_notice=route_notice,
            jd=e(row["description"]), artifacts="".join(artifact_html), queue=e(row["eligibility_queue"].replace("_", " ")),
            explanation=e(row["score_explanation"]), warnings=e("; ".join(warnings) or "None"), reasons=e("; ".join(reasons) or "None"), prepare=prepare,
            components="".join("<p><b>{}</b>: {}</p>".format(e(key.replace("_", " ").title()), e(value)) for key, value in components.items() if key not in {"matched_terms", "missing_terms"}) or "<p class=\"muted\">Not scored yet.</p>",
            salary_evidence=e(salary_evidence), language=e(row["language_requirements"] or "Not stated"),
            sponsorship=e(row["sponsorship"] or "Unknown"), clearance=e(row["security_clearance"] or "Not stated"),
            job_id=job_id, status_options=status_options,
        )
        return page(row["title"], body)

    @app.post("/jobs/{job_id}/status")
    def update_status(job_id: int, status: str = Form(...), note: str = Form("")):
        if status == "submitted":
            return RedirectResponse("/jobs/{}?error={}".format(job_id, quote("Submitted status requires confirmed submission evidence.")), status_code=303)
        try:
            repo.transition(job_id, status, note)
            return RedirectResponse("/jobs/{}?message={}".format(job_id, quote("Status updated to {}.".format(status))), status_code=303)
        except ValueError as exc:
            return RedirectResponse("/jobs/{}?error={}".format(job_id, quote(str(exc))), status_code=303)

    @app.post("/jobs/{job_id}/prepare")
    def prepare(job_id: int):
        try:
            artifact_id = resume_service().prepare(job_id)
            message = "Resume artifact {} is ready for review.".format(artifact_id)
            return RedirectResponse("/jobs/{}?message={}".format(job_id, quote(message)), status_code=303)
        except Exception as exc:
            return RedirectResponse("/jobs/{}?error={}".format(job_id, quote(str(exc))), status_code=303)

    @app.get("/submissions/{submission_id}/resume")
    def submission_resume_file(submission_id: int):
        try:
            path = repo.submission_resume_file(submission_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, str(exc))
        return FileResponse(str(path), filename=path.name,
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    @app.get("/artifacts/{artifact_id}/file/{kind}")
    def artifact_file(artifact_id: int, kind: str):
        if kind not in {"docx", "pdf"}:
            raise HTTPException(404, "Unknown artifact kind")
        artifact = repo.artifact(artifact_id)
        path = Path(artifact[kind + "_path"]).resolve()
        root = Path(artifact_root).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404, "Artifact file is unavailable")
        media = "application/pdf" if kind == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return FileResponse(str(path), media_type=media, filename=path.name)

    @app.post("/jobs/{job_id}/approve")
    def approve(job_id: int, artifact_id: int = Form(...), fields_json: str = Form(...), typed_confirmation: str = Form(...)):
        try:
            fields = json.loads(fields_json)
            if not isinstance(fields, dict):
                raise ValueError("Reviewed fields must be a JSON object")
            binding = ApplicationService(repo, dry_run=True).approve(job_id, artifact_id, fields, typed_confirmation)
            return RedirectResponse("/jobs/{}/approval/{}".format(job_id, binding), status_code=303)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return RedirectResponse("/jobs/{}?error={}".format(job_id, quote(str(exc))), status_code=303)

    @app.get("/jobs/{job_id}/approval/{binding}", response_class=HTMLResponse)
    def approval_review(job_id: int, binding: str):
        approval = repo.approval(binding)
        if not approval or approval["job_id"] != job_id:
            raise HTTPException(404, "Approval is invalid or expired")
        fields = _json(approval["answers_json"], {})
        field_names = ", ".join(fields) or "None"
        body = """<p><a href="/jobs/{job_id}">← Back to job</a></p><div class="panel"><h1>Package approved</h1><div class="notice">The resume, canonical URL, reviewed field names, and answer set are cryptographically bound. Any change blocks submission.</div><p><b>Approved fields:</b> {field_names}</p><p>Answers are stored only in the local database and are not printed on this page. Live ATS submission is disabled by default. This action performs the guarded dry run and records that manual action is required.</p><form method="post" action="/jobs/{job_id}/submit"><input type="hidden" name="binding" value="{binding}"><button type="submit">Run guarded submission</button></form></div>""".format(job_id=job_id, binding=e(binding), field_names=e(field_names))
        return page("Approved package", body)

    @app.post("/jobs/{job_id}/submit", response_class=HTMLResponse)
    def submit(job_id: int, binding: str = Form(...)):
        try:
            approval = repo.approval(binding)
            if not approval or approval["job_id"] != job_id:
                raise ValueError("Approval is invalid or expired")
            fields = _json(approval["answers_json"], None)
            if not isinstance(fields, dict):
                raise ValueError("Approved answers are unavailable")
            flags = {name: os.environ.get("JOBFLOW_ENABLE_" + name.upper(), "").lower() in {"1", "true", "yes"} for name in ("greenhouse", "lever", "ashby")}
            live = os.environ.get("JOBFLOW_LIVE_SUBMISSION", "").lower() in {"1", "true", "yes"}
            decision = ApplicationService(repo, dry_run=not live, feature_flags=flags).submit(binding, fields)
            return page("Submission result", '<p><a href="/jobs/{}">← Back to job</a></p><div class="panel"><h1>{}</h1><p>{}</p><p>{}</p></div>'.format(job_id, e(decision.state.replace("_", " ").title()), e(decision.reason), e(decision.receipt)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return page("Submission blocked", '<div class="notice error">{}</div>'.format(e(exc)))

    @app.get("/settings", response_class=HTMLResponse)
    def settings(message: str = "", error: str = ""):
        gmail = GmailVerificationClient.from_environment()
        gmail_configured = gmail.configured()
        gmail_saved = gmail.connected()
        gmail_connected = False
        if gmail_configured and gmail_saved:
            try:
                gmail.profile()
                gmail_connected = True
            except Exception:
                # A token file can remain after Google revokes its refresh
                # token; the user must be offered a fresh OAuth connection.
                pass
        source_rows = [
            ("Adzuna", bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))),
            ("Brave Search", bool(os.environ.get("BRAVE_SEARCH_API_KEY"))),
            ("Greenhouse boards", bool(os.environ.get("JOBFLOW_GREENHOUSE_BOARDS"))),
            ("Lever sites", bool(os.environ.get("JOBFLOW_LEVER_SITES"))),
            ("Ashby boards", bool(
                os.environ.get("JOBFLOW_ASHBY_BOARDS") or os.environ.get("JOBFLOW_ASHBY_REGISTRY")
            )),
            ("OpenAI resume tailoring", bool(os.environ.get("OPENAI_API_KEY"))),
            ("Twice-daily qualified-job auto-apply", os.environ.get("JOBFLOW_AUTO_APPLY", "").lower() in {"1", "true", "yes"}),
            ("Gmail verification reader", gmail_connected),
            ("Persistent ATS login/account automation", bool(os.environ.get("JOBFLOW_BROWSER_PROFILE", "private-data/browser-profile"))),
        ]
        rows = "".join('<p><span class="pill {}">{}</span> {}</p>'.format("qualified" if active else "review", "configured" if active else "not configured", e(name)) for name, active in source_rows)
        notices = '<div class="notice">{}</div>'.format(e(message)) if message else ""
        notices += '<div class="notice error">{}</div>'.format(e(error)) if error else ""
        if gmail_configured:
            status = (
                '<span class="pill qualified">connected</span> Gmail read-only access is ready. '
                if gmail_connected else
                '<span class="pill review">not connected</span> Gmail authorization needs to be renewed. '
                if gmail_saved else ''
            )
            label = "Reconnect Gmail" if gmail_saved else "Connect Gmail"
            gmail_action = status + '<form method="post" action="/settings/gmail/connect"><button type="submit">{}</button></form>'.format(label)
        else:
            gmail_action = (
                '<span class="pill review">setup required</span> Download a Google OAuth Desktop client JSON, '
                'save it as <code>private-data/google-oauth-client.json</code>, then refresh this page.'
            )
        body = """<h1>Settings</h1>{notices}<div class="grid"><section class="panel"><h2>Integration status</h2>{rows}</section><aside class="panel"><h2>Local privacy</h2><p>OAuth tokens and generated ATS credentials stay in owner-only files under <code>private-data</code> and are never written to the database or logs.</p></aside></div><div class="panel"><h2>Gmail verification</h2><p>JobFlow requests read-only Gmail access. It searches only recent application-verification messages and automatically follows one unambiguous verification link or fills an email code. It does not process password resets, account recovery, CAPTCHA, SMS, passkeys, or authenticator challenges.</p><div class="actions">{gmail_action}</div></div><div class="panel"><h2>Automation policy</h2><p><code>JOBFLOW_AUTO_APPLY=1</code> lets scheduled runs process qualified jobs with verified employer/ATS URLs. Login and account creation use a persistent browser profile and generated local credentials. CAPTCHA pauses for you; transient login failures retry automatically. Confirmed and confirmation-unknown submissions are never duplicated.</p></div>""".format(rows=rows, notices=notices, gmail_action=gmail_action)
        body += '<section class="panel"><h2>Tavily free search</h2><p><a class="button" href="/settings/tavily">Paste API key securely</a></p></section>'
        return page("Settings", body)

    @app.post("/settings/gmail/connect")
    def connect_gmail():
        try:
            return RedirectResponse(GmailVerificationClient.from_environment().authorization_url(), status_code=303)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return RedirectResponse("/settings?error=" + quote(str(exc)), status_code=303)

    @app.get("/oauth/gmail/callback")
    def gmail_callback(
        code: str = Query(""), state: str = Query(""), oauth_error: str = Query("", alias="error")
    ):
        if oauth_error:
            return RedirectResponse(
                "/settings?error=" + quote("Google authorization was not completed: " + oauth_error),
                status_code=303,
            )
        try:
            if not code or not state:
                raise GmailConfigurationError("Google did not return an authorization code")
            gmail = GmailVerificationClient.from_environment()
            gmail.complete_authorization(code, state)
            profile = gmail.profile()
            address = profile.get("emailAddress") or "the selected account"
            return RedirectResponse(
                "/settings?message=" + quote("Gmail connected read-only for {}.".format(address)),
                status_code=303,
            )
        except Exception as exc:
            return RedirectResponse("/settings?error=" + quote("Gmail connection failed: " + str(exc)), status_code=303)

    return app
