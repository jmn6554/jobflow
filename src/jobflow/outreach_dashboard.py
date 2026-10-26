"""Phone-friendly manual-send outreach UI, isolated from application workers."""
import json
import secrets
from urllib.parse import quote

from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse

from .outreach import OutreachQueue, STATES, search_url
from .outreach_search import LABELS, linkedin_evidence_url
from .outreach_safety import REVIEW_CHECKLIST, search_safety
from .providers import public_http_url


def install_outreach(app, repo, page, e, profile_path):
    queue = OutreachQueue(repo, profile_path)
    token = secrets.token_urlsafe(32)

    def csrf():
        return '<input type="hidden" name="csrf" value="' + token + '">'

    def check(value):
        if not secrets.compare_digest(token, value):
            raise HTTPException(403, 'Reload this page before saving')

    def render(title, body):
        response = page(title, body)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    def redirect(item_id, message):
        return RedirectResponse(f'/outreach/{item_id}?message=' + quote(message), status_code=303)

    def safety_warnings(text):
        # Recompute for old cached evidence too; never trust a result's claimed
        # verification or allow a search snippet to approve itself.
        return ''.join('<p class="notice error">' + e(warning) + '</p>'
                       for warning in search_safety(text)['warnings'])

    @app.get('/outreach')
    def home(q: str = '', state: str = '', job_id: int = 0):
        try:
            items = queue.items(state)
            jobs = [repo.job(job_id)] if job_id else queue.jobs(q)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        body = '''<h1>Outreach queue</h1><p>Prepare here. Review and send yourself on LinkedIn.</p>
          <div class="notice">No automatic messages, invitations, LinkedIn scraping, or OpenAI calls.
          Optional hiring-post searches use the existing capped search allowance and cached results.
          Copying a message does not mark it sent. Suggested contacts are unverified until you check them.</div>'''
        body += '<form method="get"><label for="state">Outreach status</label><select id="state" name="state"><option value="">All</option>'
        body += ''.join(f'<option value="{s}" {"selected" if state == s else ""}>{s.title()}</option>' for s in STATES)
        body += '</select><button class="small-button">Filter queue</button></form>'
        body += '<h2>Your queue</h2><p class="muted">Up to 100 most recently updated items; due follow-ups first.</p>'
        for row in items:
            body += (f'<article class="panel"><h3><a href="/outreach/{row["id"]}">{e(row["company"])} — {e(row["title"])}</a></h3>'
                     f'<span class="pill">{e(row["state"])}</span> <span>{e(row["contact_name"] or "Contact needed")}</span>'
                     + (f'<p>Follow-up due: {e(row["followup_due"])}</p>' if row['followup_due'] else '') + '</article>')
        if not items:
            body += '<p>No outreach queued yet. Choose a saved job below.</p>'
        body += '<h2>Add from saved jobs</h2><p>Suggestions use your existing qualified/review pool. Search all saved jobs by title or company; no new discovery runs.</p>'
        body += '<form method="get"><label for="q">Search saved jobs</label><input id="q" name="q" maxlength="150" value="' + e(q) + '"><button>Search</button></form>'
        for job in jobs:
            body += (f'<article class="panel"><b>{e(job["company"])} — {e(job["title"])}</b><p>{e(job["location"])} · {e(job["eligibility_queue"])}</p>'
                     f'<form method="post" action="/outreach/add">{csrf()}<input type="hidden" name="job_id" value="{job["id"]}">'
                     '<button>Add to outreach</button></form></article>')
        return render('Outreach', body)

    @app.post('/outreach/add')
    def add(job_id: int = Form(...), csrf: str = Form(...)):
        check(csrf)
        try:
            item_id = queue.add(job_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return redirect(item_id, 'Draft ready. Verify a contact before sending. Existing outreach is reused, not duplicated.')

    @app.get('/outreach/{item_id}')
    def detail(item_id: int, message: str = ''):
        try:
            row = queue.get(item_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        body = f'<p><a href="/outreach">← Outreach queue</a></p><h1>{e(row["company"])}</h1><h2>{e(row["title"])}</h2>'
        body += f'<p class="pill">{e(row["state"])}</p>'
        if message:
            body += '<p class="notice" role="status">' + e(message) + '</p>'
        if public_http_url(row['job_url']):
            body += f'<p><a href="{e(row["job_url"])}" target="_blank" rel="noopener noreferrer">Open job posting</a></p>'
        body += '<h3>Find the person hiring for this opening</h3>'
        body += '<section class="panel"><h3>Contact safety review — before sharing your resume</h3><p>Job relevance is not identity verification. Profile history, follower/connection counts and original post dates are not collected by the public-search tool. Check them on the actual profile and save your observations and corroborating links in Evidence below.</p><ul>'
        body += ''.join('<li>' + e(check_item) + '</li>' for check_item in REVIEW_CHECKLIST)
        body += '</ul><p>If evidence is missing or concerns remain unresolved, leave the contact unverified and do not send your resume.</p></section>'
        query_url = search_url(row['company'], row['title'])
        body += f'<p><a href="{e(query_url)}" target="_blank" rel="noopener noreferrer">Search company + exact title on LinkedIn</a> · '
        body += f'<a href="{e(query_url + quote(" (site:linkedin.com/posts/ OR site:linkedin.com/feed/update/)"))}" target="_blank" rel="noopener noreferrer">Focus on hiring posts</a></p>'
        body += f'<form method="post" action="/outreach/{item_id}/research">{csrf()}<button class="secondary">Find hiring posts</button></form>'
        body += '<p class="muted">Checks the employer posting first; closed or unconfirmed postings do not use search credits. Reuses matching cached posts from this company before searching. One search at most per click, shared monthly cap, seven-day cache. Verified or already-contacted conversations are protected. Only public search results are retrieved—not your LinkedIn inbox. A shared post does not prove hiring authority; check the author, date and linked vacancy.</p>'
        research = json.loads(row['research'])
        if research:
            body += f'<p>{e(research.get("note"))} Checked: {e(research.get("checked_at"))}</p>'
            if research.get('posting'):
                posting = research['posting']
                body += f'<p>Posting status: {e(posting.get("state"))} — {e(posting.get("detail"))}</p>'
            for lead in research.get('results', []):
                url = linkedin_evidence_url(lead.get('url'))
                if url:
                    body += f'<article class="panel"><span class="pill">{e(LABELS.get(lead.get("kind"), LABELS["related"]))}</span><p><a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(lead.get("title") or "Search evidence")}</a></p><p>{e(lead.get("evidence"))}</p></article>'
                    body += safety_warnings(str(lead.get('title') or '') + ' ' + str(lead.get('evidence') or ''))
        candidates = json.loads(row['candidates'])
        if candidates:
            body += '<h3>Possible contacts from posting/public research — unverified</h3>'
            for index, candidate in enumerate(candidates):
                body += f'<p><a href="{e(candidate["url"])}" target="_blank" rel="noopener noreferrer">{e(candidate.get("name") or "Possible contact")}</a>: {e(candidate["evidence"])}</p>'
                body += safety_warnings(candidate.get('evidence'))
                if not row['first_contacted_at']:
                    body += f'<form method="post" action="/outreach/{item_id}/candidate">{csrf()}<input type="hidden" name="index" value="{index}"><button class="small-button secondary">Use suggestion (still unverified)</button></form>'
        else:
            body += '<p class="muted">No person’s LinkedIn link was found in the saved posting. Use public search; do not assume a recruiter owns this opening.</p>'
        duplicates = queue.duplicate_contacts(row)
        for duplicate in duplicates:
            body += f'<p class="notice error">Same person already appears in <a href="/outreach/{duplicate["id"]}">{e(duplicate["company"])} — {e(duplicate["title"])}</a> ({e(duplicate["state"])}). Use one conversation.</p>'
        body += f'<form method="post" action="/outreach/{item_id}/save">{csrf()}'
        for name, label in [('contact_name','Contact name'), ('contact_url','LinkedIn person profile URL'), ('contact_role','Their role/team')]:
            body += f'<label for="{name}">{label}</label><input id="{name}" name="{name}" value="{e(row[name])}" maxlength="2000">'
        body += f'<label for="evidence">Evidence: how is this person connected to this employer/team?</label><textarea id="evidence" name="evidence" maxlength="2000">{e(row["evidence"])}</textarea>'
        body += safety_warnings(row['evidence'])
        body += '<label><input style="width:auto" type="checkbox" name="verified" value="true" ' + ('checked' if row['contact_verified'] else '') + '> I completed the safety review above, independently corroborated the person and opening, and resolved any warning signs. This is my review, not automatic verification.</label>'
        body += f'<label for="draft">Message — edit before sending</label><textarea id="draft" name="draft" maxlength="4000" required>{e(row["draft"])}</textarea>'
        body += '<p class="muted">Template uses only saved professional skills mentioned in the posting. It never claims an application was submitted. Replace “Hi there” with the person’s name if appropriate.</p>'
        body += '<button type="button" class="secondary" data-copy="draft">Copy message</button><span id="copy-status" role="status" aria-live="polite"></span>'
        body += f'<label for="notes">Notes / reply summary</label><textarea id="notes" name="notes" maxlength="2000">{e(row["notes"])}</textarea><button>Save draft and contact</button></form>'
        if row['contact_url']:
            body += f'<p><a class="button secondary" href="{e(row["contact_url"])}" target="_blank" rel="noopener noreferrer">Open contact on LinkedIn</a></p>'
        body += '<h3>Manual tracking</h3><p>Only mark sent after you actually send it on LinkedIn. Replies are recorded here manually—not read from your inbox.</p><div class="actions">'
        for action, label in [('contacted','I sent the message'), ('replied','They replied'), ('closed','Close outreach')]:
            if action == 'contacted' and (row['first_contacted_at'] or not row['contact_verified']):
                continue
            if action == 'replied' and not row['first_contacted_at']:
                continue
            body += f'<form method="post" action="/outreach/{item_id}/record">{csrf()}<input type="hidden" name="action" value="{action}"><button class="secondary">{label}</button></form>'
        body += '</div>'
        if row['state'] == 'contacted' and not row['followup_sent_at']:
            followup = f'Hi, following up on my interest in the {row["title"]} role at {row["company"]}. I’d appreciate any guidance on the right person to contact. Thank you.'
            body += f'<h3>One optional follow-up</h3><p>Due {e(row["followup_due"])}. Review the conversation first; do not follow up if they declined.</p><textarea id="followup">{e(followup)}</textarea><button type="button" data-copy="followup">Copy follow-up</button>'
            body += f'<form method="post" action="/outreach/{item_id}/record">{csrf()}<input type="hidden" name="action" value="followup"><button class="secondary">I sent this follow-up</button></form>'
        body += '''<script>
          document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{
            const area=document.getElementById(button.dataset.copy), status=document.getElementById('copy-status');
            try { if(!navigator.clipboard) throw Error('fallback'); await navigator.clipboard.writeText(area.value); status.textContent=' Copied. Not sent.'; }
            catch { area.focus(); area.select(); area.setSelectionRange(0,area.value.length);
              status.textContent=document.execCommand('copy')?' Copied. Not sent.':' Text selected—use your phone’s Copy command.'; }
          }));</script>'''
        return render('Outreach draft', body)

    @app.post('/outreach/{item_id}/research')
    def research(item_id: int, csrf: str = Form(...)):
        check(csrf)
        try:
            note = queue.research(item_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return redirect(item_id, note)

    @app.post('/outreach/{item_id}/candidate')
    def candidate(item_id: int, index: int = Form(...), csrf: str = Form(...)):
        check(csrf)
        try:
            row = queue.get(item_id)
            candidates = json.loads(row['candidates'])
            if index < 0 or index >= len(candidates):
                raise ValueError('Unknown contact suggestion')
            contact = candidates[index]
            draft = row['draft']
            if draft.startswith('Hi there,') and contact.get('name'):
                draft = 'Hi ' + contact['name'].split()[0] + ',' + draft[len('Hi there,'):]
            queue.save(item_id, contact.get('name',''), contact['url'], contact.get('role',''),
                       contact['evidence'], False, draft, row['notes'])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return redirect(item_id, 'Suggestion loaded, not verified. Check the current employer/team and save before sending.')

    @app.post('/outreach/{item_id}/save')
    def save(item_id: int, csrf: str = Form(...), contact_name: str = Form(''), contact_url: str = Form(''),
             contact_role: str = Form(''), evidence: str = Form(''), verified: bool = Form(False),
             draft: str = Form(...), notes: str = Form('')):
        check(csrf)
        try:
            queue.save(item_id, contact_name, contact_url, contact_role, evidence, verified, draft, notes)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return redirect(item_id, 'Saved locally. Nothing sent to LinkedIn.')

    @app.post('/outreach/{item_id}/record')
    def record(item_id: int, csrf: str = Form(...), action: str = Form(...)):
        check(csrf)
        try:
            queue.record(item_id, action)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return redirect(item_id, 'Your manual outreach update was recorded. Job application status is unchanged.')
