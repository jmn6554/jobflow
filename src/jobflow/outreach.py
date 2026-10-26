"""Local, template-only outreach preparation. Never sends LinkedIn messages."""
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .dedupe import normalize_text


STATES = ('draft', 'ready', 'contacted', 'replied', 'closed')


def profile_url(value):
    """Accept only normal public LinkedIn person URLs, not redirect/share URLs."""
    value = value.strip()
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not re.fullmatch(r'(?:www\.|[a-z]{2}\.)?linkedin\.com', parsed.hostname or '')
            or parsed.username or parsed.password or parsed.port
            or not re.fullmatch(r'/in/[A-Za-z0-9_%.-]+/?', parsed.path)):
        raise ValueError('Use a LinkedIn profile URL: https://www.linkedin.com/in/name/')
    slug = unquote(parsed.path.strip('/').split('/')[1]).casefold()
    if not re.fullmatch(r'[\w.-]+', slug):
        raise ValueError('Invalid LinkedIn profile name')
    return 'https://www.linkedin.com/in/' + quote(slug) + '/'


def contact_candidates(description):
    """Extract evidence, not identities inferred from a job title. All are unverified."""
    found = {}
    for match in re.finditer(r'https://(?:www\.|[a-z]{2}\.)?linkedin\.com/in/[A-Za-z0-9_%.-]+/?', html.unescape(description or '')):
        try:
            url = profile_url(match.group())
        except ValueError:
            continue
        context = html.unescape(re.sub('<[^>]+>', ' ', description[max(0, match.start()-160):match.end()+180]))
        found[url] = {'url': url, 'evidence': ' '.join(context.split())[:500], 'confidence': 'unverified'}
    return list(found.values())[:10]


def search_url(company, title):
    from .outreach_search import search_query
    query = search_query(company, title)
    return 'https://www.google.com/search?q=' + quote(query)


def draft_message(job, profile, contact_name=''):
    # Only list skills explicitly saved as professional experience AND present
    # in this posting. No API calls, inferred achievements, or invented tenure.
    description = html.unescape(re.sub('<[^>]+>', ' ', job['description'] or ''))
    skills = [s for s in profile.get('experience', {}).get('professional_skills', [])
              if re.search(r'(?<!\w)' + re.escape(s) + r'(?!\w)', description, re.I)][:3]
    first = contact_name.strip().split()[0] if contact_name.strip() else 'there'
    sender = profile.get('identity', {}).get('full_name', '').strip().split()
    experience = (' My professional experience includes ' + ', '.join(skills) + '.') if skills else ''
    # Intentionally valid even for failed/uncertain/unattempted applications.
    return (f"Hi {first}, I’m interested in the {job['title']} role at {job['company']}."
            + experience + ' Are you the right person to speak with about this opening, '
            'or could you point me to the team? Thanks' + (', ' + sender[0] if sender else '') + '.')


class OutreachQueue:
    def __init__(self, repo, profile_path=Path('private-data/application-profile.json')):
        self.repo = repo
        self.profile_path = Path(profile_path)
        with repo.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS outreach_items (
              id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
              job_key TEXT NOT NULL UNIQUE, company TEXT NOT NULL, title TEXT NOT NULL,
              job_url TEXT NOT NULL, draft TEXT NOT NULL, candidates TEXT NOT NULL DEFAULT '[]',
              contact_name TEXT NOT NULL DEFAULT '', contact_url TEXT NOT NULL DEFAULT '',
              contact_role TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
              contact_verified INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'draft', notes TEXT NOT NULL DEFAULT '',
              first_contacted_at TEXT, followup_due TEXT NOT NULL DEFAULT '',
              followup_sent_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outreach_events (
              id INTEGER PRIMARY KEY, outreach_id INTEGER NOT NULL REFERENCES outreach_items(id),
              action TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outreach_contact ON outreach_items(contact_url);
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(outreach_items)')}
            if 'research' not in columns:
                db.execute("ALTER TABLE outreach_items ADD COLUMN research TEXT NOT NULL DEFAULT '{}'")

    def research(self, item_id, finder=None, posting=None):
        from .outreach_search import OutreachSearch
        from .posting_status import PostingStatus
        row = self.get(item_id)
        if row['first_contacted_at'] or row['state'] in {'closed', 'replied'} or row['contact_verified']:
            return 'Existing verified/contacted/closed conversation protected; no search requested.'
        if posting is None:
            checker = PostingStatus()
            try:
                posting = (checker.check(dict(self.repo.job(row['job_id']))) if row['job_id'] else
                           checker.result('unknown', 'Saved job no longer available', row['job_url']))
            finally:
                checker.client.close()
        if posting['state'] == 'open':
            leads, note = (finder or OutreachSearch()).find(row['company'], row['title'])
        else:
            leads, note = [], f'Posting {posting["state"]}: {posting["detail"]}. No contact search requested.'
        previous = json.loads(row['research'])
        # A temporary failure must not erase previously discovered evidence.
        results = leads or previous.get('results', [])
        research = dict(results=results, note=note, posting=posting, checked_at=datetime.now(timezone.utc).isoformat())
        with self.repo.connect() as db:
            db.execute('UPDATE outreach_items SET research=? WHERE id=?', (json.dumps(research), item_id))
        return note

    def profile(self):
        return json.loads(self.profile_path.read_text()) if self.profile_path.exists() else {}

    def add(self, job_id):
        job = dict(self.repo.job(job_id))
        key = normalize_text(job['company']) + '|' + normalize_text(job['title'])
        stamp = datetime.now(timezone.utc).isoformat()
        with self.repo.connect() as db:
            db.execute('''INSERT OR IGNORE INTO outreach_items
              (job_id,job_key,company,title,job_url,draft,candidates,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?)''', (job_id, key, job['company'], job['title'],
                job['canonical_url'] or job['url'], draft_message(job, self.profile()),
                json.dumps(contact_candidates(job['description'])), stamp, stamp))
            return db.execute('SELECT id FROM outreach_items WHERE job_key=?', (key,)).fetchone()[0]

    def get(self, item_id):
        with self.repo.connect() as db:
            row = db.execute('SELECT * FROM outreach_items WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise ValueError('Outreach item not found')
        return dict(row)

    def suggest_contact(self, item_id, name, url, role, evidence):
        """Persist public research as an unverified suggestion, never an approval."""
        row = self.get(item_id)
        url = profile_url(url)
        candidates = json.loads(row['candidates'])
        if any(c['url'] == url for c in candidates):
            return
        candidates.append({'name': name, 'url': url, 'role': role, 'evidence': evidence,
                           'confidence': 'unverified'})
        with self.repo.connect() as db:
            db.execute('UPDATE outreach_items SET candidates=? WHERE id=?', (json.dumps(candidates[:10]), item_id))

    def items(self, state='', limit=100):
        if state and state not in STATES:
            raise ValueError('Unknown outreach status')
        with self.repo.connect() as db:
            return [dict(r) for r in db.execute('''SELECT * FROM outreach_items
              WHERE (?='' OR state=?) ORDER BY
              CASE WHEN state='contacted' AND followup_due!='' AND followup_due<=? THEN 0 ELSE 1 END,
              updated_at DESC,id DESC LIMIT ?''', (state, state, date.today().isoformat(), limit))]

    def jobs(self, query='', limit=20):
        with self.repo.connect() as db:
            if query:
                return db.execute('''SELECT id,title,company,location,eligibility_queue FROM jobs
                  WHERE instr(lower(company || ' ' || title),lower(?))>0
                  ORDER BY updated_at DESC,id DESC LIMIT ?''', (query[:150], limit)).fetchall()
            return db.execute('''SELECT id,title,company,location,eligibility_queue FROM jobs
              WHERE eligibility_queue IN ('qualified','estimated_pay_review')
              AND status NOT IN ('rejected','withdrawn')
              ORDER BY updated_at DESC,fit_score DESC,id DESC LIMIT ?''', (limit,)).fetchall()

    def save(self, item_id, contact_name, contact_url, contact_role, evidence, verified, draft, notes):
        current = self.get(item_id)
        url = profile_url(contact_url) if contact_url.strip() else ''
        if verified and (not url or not contact_name.strip() or not evidence.strip()):
            raise ValueError('Verification needs a contact name, profile URL, and evidence of their connection to the employer/team.')
        if not draft.strip() or len(draft) > 4000:
            raise ValueError('Enter a draft of 1–4000 characters')
        if any(len(s) > 2000 for s in (contact_name, contact_role, evidence, notes)):
            raise ValueError('Contact fields and notes must be under 2000 characters')
        if current['first_contacted_at'] and url != current['contact_url']:
            raise ValueError('Cannot replace a contacted person; preserve this outreach history.')
        state = current['state']
        if state in {'draft', 'ready'}:
            state = 'ready' if verified else 'draft'
        with self.repo.connect() as db:
            db.execute('''UPDATE outreach_items SET contact_name=?,contact_url=?,contact_role=?,evidence=?,
              contact_verified=?,draft=?,notes=?,state=?,updated_at=? WHERE id=?''',
              (contact_name.strip(), url, contact_role.strip(), evidence.strip(), int(verified),
               draft.strip(), notes.strip(), state, datetime.now(timezone.utc).isoformat(), item_id))

    def duplicate_contacts(self, item):
        if not item['contact_url']:
            return []
        with self.repo.connect() as db:
            return db.execute('''SELECT id,company,title,state FROM outreach_items
              WHERE contact_url=? AND id!=?''', (item['contact_url'], item['id'])).fetchall()

    def record(self, item_id, action):
        if action not in {'contacted', 'replied', 'closed', 'followup'}:
            raise ValueError('Unknown outreach action')
        stamp = datetime.now(timezone.utc).isoformat()
        with self.repo.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM outreach_items WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise ValueError('Outreach item not found')
            if action == 'contacted':
                if not row['contact_verified'] or not row['contact_url']:
                    raise ValueError('Verify the recipient before marking a message sent')
                if row['first_contacted_at']:
                    raise ValueError('Already marked contacted; no duplicate outreach recorded')
                if row['state'] != 'ready':
                    raise ValueError('Only a ready outreach draft can be marked sent')
                duplicate = db.execute('''SELECT id FROM outreach_items WHERE contact_url=?
                  AND first_contacted_at IS NOT NULL AND id!=?''', (row['contact_url'], item_id)).fetchone()
                if duplicate:
                    raise ValueError(f"This person was already contacted in outreach #{duplicate[0]}; use that conversation.")
                db.execute('''UPDATE outreach_items SET state='contacted',first_contacted_at=?,
                  followup_due=?,updated_at=? WHERE id=?''',
                  (stamp, (date.today()+timedelta(days=7)).isoformat(), stamp, item_id))
            elif action == 'followup':
                if row['state'] != 'contacted' or row['followup_sent_at'] or not row['first_contacted_at']:
                    raise ValueError('Only one follow-up is tracked for an unanswered contacted item')
                db.execute("UPDATE outreach_items SET followup_sent_at=?,followup_due='',updated_at=? WHERE id=?", (stamp, stamp, item_id))
            else:
                if action == 'replied' and not row['first_contacted_at']:
                    raise ValueError('Record your initial message before a reply')
                db.execute("UPDATE outreach_items SET state=?,followup_due='',updated_at=? WHERE id=?", (action, stamp, item_id))
            db.execute('INSERT INTO outreach_events(outreach_id,action,created_at) VALUES (?,?,?)', (item_id, action, stamp))
