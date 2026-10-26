"""Read-only, mobile-friendly strategy view using the existing JobFlow shell."""
from .providers import public_http_url


def install_strategy(app, repo, render, e):
    @app.get('/strategy')
    def strategy():
        from .strategy import build_report
        r = build_report(repo)
        m = r['metrics']
        body = '<h1>Focused job search</h1><p>' + e(r['note']) + '</p>'
        body += '<p>Two-week operating targets: 40–60 confirmed applications and 10–15 targeted outreach messages, if enough suitable openings exist. No automatic messaging.</p>'
        body += '<div class="metrics">'
        for label, value in [('New eligible candidates', r['new_candidates']), ('Strong work-fit candidates', r['strong_candidates']), ('Distinct jobs attempted (14 days)', m['distinct_attempted_jobs']), ('Recorded confirmations (14 days)', m['app_recorded_confirmations'])]:
            body += f'<div class="metric"><b>{value}</b><span>{e(label)}</span></div>'
        body += '</div><p>' + e(m['confirmation_caveat']) + '</p>'
        body += '<h2>Results by work family (14 days)</h2>'
        for family, values in m['by_role_family'].items():
            body += f'<p>{e(family.replace("_", " "))}: {values["attempted_jobs"]} attempted jobs; {values["app_recorded_confirmations"]} recorded confirmations; {values["known_interview_or_offer_jobs"]} tracked interviews/offers.</p>'
        body += '<p>Update interview status in <a href="/applications">Applications</a>. Drafts and verified contacts are in <a href="/outreach">Outreach</a>.</p>'
        body += '<h2>Results by résumé track</h2><p>Historical files without a track stay untracked. Counts can overlap if the same job used multiple versions.</p>'
        for track, values in m['by_resume_track'].items():
            body += f'<p>{e(track)}: {values["attempted"]} attempted jobs; {values["confirmed"]} recorded confirmations; {values["interview"]} tracked interviews/offers.</p>'
        for title, items, note in [
            ('Prioritised candidates', r['ranked'], 'Up to 60. Canada-compatible first. Adjacent roles remain visible; no new score cutoff.'),
            ('Manual rescue', r['manual_rescue'], 'Up to 10 strong-fit failures. Verify the existing attempt before manually submitting.'),
            ('Reconcile first — do not resubmit', r['reconciliation'], 'Up to 10 strong-fit uncertain/CAPTCHA/interrupted applications; these remain protected.')]:
            body += '<h2>' + title + '</h2><p>' + note + '</p>'
            if not items:
                body += '<p>No items in this section.</p>'
            for row in items:
                a = row['fit']
                body += f'<article class="panel"><h3><a href="/jobs/{row["id"]}">{e(row["company"])} — {e(row["title"])}</a></h3><p>{e(row["location"])} · {e(a["band"])} · résumé track: {e(a["resume_track"])}</p>'
                body += '<p>Listed pay: ' + e(f"{row['salary_min']}–{row['salary_max']} {row['salary_currency']}" if row.get('salary_max') else 'Undisclosed; not excluded') + '</p>'
                body += '<p>Related work: ' + e(', '.join(x['work'] for x in a['evidence']) or 'Not established from saved description') + '</p>'
                for evidence in a['evidence']:
                    body += '<details><summary>' + e(evidence['work']) + '</summary>' + e(evidence['excerpt']) + '</details>'
                if a['concerns']:
                    body += '<p>Concerns: ' + e('; '.join(a['concerns'])) + '</p>'
                if row.get('blocker'):
                    body += '<p>Blocker: ' + e(row['blocker']) + '</p><p>' + e(row['action']) + '</p>'
                if public_http_url(row.get('canonical_url') or ''):
                    body += f'<a href="{e(row["canonical_url"])}" target="_blank" rel="noopener noreferrer">Employer posting</a> · '
                body += f'<a href="/jobs/{row["id"]}#application-resumes">Résumé history</a>'
                if row.get('resume_url'):
                    body += f' · <a href="{e(row["resume_url"])}">Exact attempt résumé</a>'
                body += '</article>'
        return render('Focused search', body)
