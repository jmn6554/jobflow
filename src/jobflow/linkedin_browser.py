"""Read rendered LinkedIn results in the user's signed-in Chrome; never apply.

No private endpoints, hidden application state, credentials, or anti-bot workarounds.
Collected links are discovery evidence, not approved application-queue entries.
"""
import fcntl
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .eligibility import evaluate, application_guard, score
from .models import JobInput
from .policy import local_office, settings, us_office
from .titles import title_review_reason, relevant_title, excluded_title, software_role_exclusion, project_role_exclusion, product_role_exclusion
from .dedupe import normalize_text

CARDS = r'''() => {
  const out = new Map();
  for (const e of document.querySelectorAll('[componentkey^="job-card-component-ref-"],li[data-occludable-job-id]')) {
    const id = (e.getAttribute('componentkey') || e.getAttribute('data-occludable-job-id') || '').match(/(\d+)$/)?.[1];
    const ps = [...e.querySelectorAll('p')].map(p=>p.innerText.trim()).filter(Boolean);
    const title = ps[0]?.split('\n')[0].split('(Verified job)')[0].replace(/^Selected,\s*/i,'').trim()
      || e.querySelector('a[href*="/jobs/view/"]')?.innerText.trim();
    const company = ps[1] || e.querySelector('.artdeco-entity-lockup__subtitle')?.innerText.trim();
    const location = ps[2] || e.querySelector('.job-card-container__metadata-item')?.innerText.trim();
    if (id && title && company && location) out.set(id, {id,title,company,location});
  }
  return [...out.values()];
}'''

DETAIL = r'''() => {
  const heading = [...document.querySelectorAll('h2')].find(h=>h.innerText.trim()==='About the job');
  let node=heading, description='';
  for(let i=0;node && i<6;i++,node=node.parentElement) {
    if(node.innerText.length>150) {description=node.innerText;break;}
  }
  if(!description) description=document.querySelector('.jobs-description')?.innerText || '';
  const links=[...document.querySelectorAll('a')].filter(a=>
    /apply on company website/i.test(a.getAttribute('aria-label')||'') || a.innerText.trim()==='Apply');
  const selected=[...document.querySelectorAll('a[href*="/jobs/view/"]')].find(a=>!a.closest('[componentkey^="job-card-component-ref-"],li[data-occludable-job-id]'));
  let header=selected, headerText='';
  for(let i=0;header && i<8;i++,header=header.parentElement) {
    if(header.innerText.length>3000) break;
    headerText=header.innerText;
    if(/(^|\n)(Apply|Easy Apply)(\n|$)/.test(headerText)) break;
  }
  const workplace=headerText.split('\n').map(x=>x.trim()).find(x=>['Remote','Hybrid','On-site'].includes(x)) || '';
  return {description, header_text:headerText, selected_id:selected?.href.match(/\/jobs\/view\/(\d+)/)?.[1] || '', workplace,
    apply_urls:links.map(a=>a.href), easy_apply:[...document.querySelectorAll('button,a')].some(e=>e.innerText.trim()==='Easy Apply')};
}'''


def clean_search_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in {'www.linkedin.com', 'linkedin.com'} or p.path.rstrip('/') not in {'/jobs/search', '/jobs/search-results', '/comm/jobs/search', '/comm/jobs/search-results'}:
        raise ValueError('Use a LinkedIn jobs search/results URL')
    allowed = {'keywords', 'location', 'geoId', 'f_WT', 'f_TPR', 'f_E', 'f_JT', 'distance', 'sortBy', 'start'}
    query = [(k, v) for k, values in parse_qs(p.query).items() if k in allowed for v in values]
    return urlunsplit(('https', 'www.linkedin.com', p.path.replace('/comm/', '/'), urlencode(query), ''))


def validate_search_filters(requested_url, actual_url, remote_selected=False):
    """AI search replaces f_WT with an opaque UI filter; verify its visible state.

    Even an active Remote chip is not posting-level eligibility evidence.
    """
    wanted, actual = parse_qs(urlsplit(requested_url).query), parse_qs(urlsplit(actual_url).query)
    if wanted.get('geoId') and actual.get('geoId') != wanted['geoId']:
        raise ValueError('FILTER_MISMATCH: LinkedIn did not retain requested geography')
    remote_requested = wanted.get('f_WT') == ['2']
    remote_verified = actual.get('f_WT') == ['2'] or remote_selected
    if remote_requested and not remote_verified:
        raise ValueError('FILTER_MISMATCH: Remote filter not verified in URL or visible controls')
    return {'geography_verified': bool(wanted.get('geoId')), 'remote_requested': remote_requested,
            'remote_filter_verified': remote_verified, 'ai_search': '/search-results' in urlsplit(actual_url).path,
            'posting_remote_verification_required': True}


def remote_filter_selected(page):
    return page.evaluate('''() => [...document.querySelectorAll('[role="checkbox"], [role="radio"], input[type="checkbox"], input[type="radio"]')].some(e => {
      const label = e.getAttribute('aria-label') || e.labels?.[0]?.innerText || e.innerText || '';
      return /^(?:Filter by )?Remote$/i.test(label.trim()) &&
        (e.checked === true || e.getAttribute('aria-checked') === 'true') && e.getClientRects().length > 0;
    })''')


def latest_alert_search_url():
    """Read the newest alert only; return search filters, never tracking tokens."""
    from .gmail import GmailVerificationClient, GMAIL_API, _message_raw
    client = GmailVerificationClient.from_environment()
    headers = {'Authorization': 'Bearer ' + client._access_token()}
    listing = client._get(GMAIL_API+'/messages', headers=headers,
                          params={'q':'from:jobalerts-noreply@linkedin.com newer_than:14d','maxResults':1})
    listing.raise_for_status()
    for message in listing.json().get('messages', []):
        response = client._get(GMAIL_API+'/messages/'+message['id'], headers=headers, params={'format':'full'})
        response.raise_for_status()
        raw = html.unescape(_message_raw(response.json()['payload']))
        for url in re.findall(r'https?://[^\s<>"\']+', raw):
            try:
                return clean_search_url(url)
            except ValueError:
                continue
    raise ValueError('Latest LinkedIn alert has no supported search-results link')


def employer_url(url):
    p = urlsplit(url)
    if p.hostname in {'linkedin.com', 'www.linkedin.com'} and p.path == '/safety/go/':
        url = parse_qs(p.query).get('url', [''])[0]
        p = urlsplit(url)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
        return ''
    host = p.hostname.lower()
    if host == 'linkedin.com' or host.endswith('.linkedin.com'):
        return ''
    from .providers import public_http_url, aggregator_url
    if not public_http_url(url) or aggregator_url(url):
        return ''
    query = [(k, v) for k, values in parse_qs(p.query).items()
             if not k.lower().startswith(('utm_', 'tracking')) for v in values]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ''))


def country(location):
    if re.search(r'\bCanada\b|,\s*(?:QC|ON|BC|AB|MB|SK|NB|NS|NL|PE)(?:\W|$)', location, re.I) or local_office(location):
        return 'CA'
    if re.search(r'\bUnited States\b|,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)(?:\W|$)', location, re.I):
        return 'US'
    return ''


def search_constraint_evidence(card, detail, constraints):
    """Use visible job-header evidence, never the JD or an AI-search promise."""
    text = detail.get('header_text', '')
    result = dict(header_text=text, checked_at=datetime.now(timezone.utc).isoformat(), reason='')
    if constraints.get('canada_remote_only') and (country(card['location']) != 'CA' or
            not (detail.get('workplace') == 'Remote' or re.search(r'\bremote\b', card['location'], re.I))):
        result['reason'] = 'Canada remote not verified on posting'
    age = re.search(r'\b(\d+)\s+(minute|hour|day|week|month)s?\s+ago\b', text, re.I)
    if age:
        unit = dict(minute=1/60, hour=1, day=24, week=168, month=744)[age[2].lower()]
        result['age_hours_lower_bound'] = int(age[1]) * unit
        result['age_hours_upper_bound'] = (int(age[1]) + 1) * unit
    elif re.search(r'\bjust now\b', text, re.I):
        result.update(age_hours_lower_bound=0, age_hours_upper_bound=1/60)
    if constraints.get('posted_within_hours') and result.get('age_hours_upper_bound', float('inf')) > constraints['posted_within_hours']:
        result['reason'] = result['reason'] or 'Posting age not verified below requested limit'
    count = re.search(r'(?<![\d,])(\d[\d,]*)\s+(applicants?|(?:people|person) clicked apply)\b', text, re.I)
    early = re.search(r'\b(?:be among the|among the) first (\d+) applicants\b', text, re.I)
    if early:
        result.update(applicant_count_upper_bound=int(early[1])-1, count_kind='early_applicant_label')
    elif count and not re.search(r'(?:over|more than|at least)\s*$', text[:count.start()], re.I):
        result.update(applicant_count_upper_bound=int(count[1].replace(',', '')),
                      count_kind='clicks' if 'clicked' in count[2].lower() else 'applicants')
    if constraints.get('applicants_below') and result.get('applicant_count_upper_bound', float('inf')) >= constraints['applicants_below']:
        result['reason'] = result['reason'] or 'Applicant/click count not verified below requested limit'
    return result


def card_reason(card):
    product_reason = product_role_exclusion(card['title'])
    if product_reason:
        return product_reason
    project_reason = project_role_exclusion(card['title'])
    if project_reason:
        return project_reason
    software_reason = software_role_exclusion(card['title'])
    if software_reason:
        return software_reason
    filter_title = settings().get('linkedin_title_filter_enabled', True)
    if filter_title and (excluded_title(card['title']) or re.search(r'\b(?:architect|scientist|settlements?|cybersecurity)\b', card['title'], re.I)):
        return 'Excluded role'
    if filter_title and not relevant_title(card['title']):
        return 'Title outside requested role families; retained for review'
    reason = title_review_reason(card['title'], '') if filter_title else ''
    if reason:
        return reason
    loc = card['location']
    if re.search(r'\b(?:Hybrid|On-site|Onsite)\b', loc, re.I) and not (local_office(loc) or us_office(loc)):
        return 'Office location outside approved geography'
    return ''


class LinkedInBrowserCollector:
    def __init__(self, repo):
        self.repo = repo
        with repo.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS linkedin_browser_jobs (
                external_id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,
                location TEXT NOT NULL, linkedin_url TEXT NOT NULL, description TEXT NOT NULL,
                employer_url TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
                search_url TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)''')
        self._last_action = 0.0

    def pace(self, interval):
        """Space UI actions, without adding a full sleep after a slow page load."""
        remaining = interval - (time.monotonic() - self._last_action)
        if remaining > 0:
            time.sleep(remaining)
        self._last_action = time.monotonic()

    @staticmethod
    def scroll_card(page, job_id):
        # Resolve and scroll in one DOM operation: LinkedIn replaces card nodes
        # during rendering, so a separately resolved element can detach.
        return page.evaluate('''id => {
            const card = document.querySelector('[componentkey="job-card-component-ref-'+id+'"],li[data-occludable-job-id="'+id+'"]');
            if (!card) return false;
            card.scrollIntoView({block: 'nearest'});
            return true;
        }''', job_id)

    def wait_ready(self, page, predicate, denied, timeout=15):
        """Poll rendered content only; every poll still honors access restrictions."""
        deadline = time.monotonic() + timeout
        while True:
            if denied:
                raise ValueError('ACCESS_RESTRICTION: HTTP '+str(denied[0])+'; stopped without retries')
            self.check_access(page)
            value = predicate()
            if value:
                return value
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(200)

    def cached_skips(self, cards):
        """One database snapshot per page, before any detail-panel clicks."""
        with self.repo.connect() as db:
            known = {r['external_id'] for r in db.execute(
                "SELECT external_id FROM linkedin_browser_jobs WHERE description != ''")}
            protected = {(normalize_text(r['company']), normalize_text(r['title']))
                         for r in db.execute("SELECT j.company,j.title FROM jobs j JOIN submissions s ON j.id=s.job_id WHERE s.state IN ('submitted','confirmation_unknown','captcha_waiting')")}
        return {c['id']: 'excluded' if card_reason(c) else 'cached' if c['id'] in known
                else 'already_applied_or_held' for c in cards
                if card_reason(c) or c['id'] in known or
                (normalize_text(c['company']), normalize_text(c['title'])) in protected}

    def wait_detail(self, page, job_id, previous, denied):
        last, stable = None, 0

        def ready():
            nonlocal last, stable
            detail = page.evaluate(DETAIL)
            current = parse_qs(urlsplit(page.url).query).get('currentJobId', [''])[0]
            if (current != job_id or detail['selected_id'] != job_id or not detail['description']
                    or (previous.get('selected_id') != job_id and
                        ' '.join(previous.get('description', '').split()) == ' '.join(detail['description'].split()))):
                last, stable = None, 0
                return None
            stable = stable + 1 if detail == last else 0
            last = detail
            return detail if stable >= 2 else None

        detail = self.wait_ready(page, ready, denied)
        if not detail:
            return None  # Caller records this card as unresolved, never reuses stale detail.
        return detail

    def save(self, card, search_url, detail=None):
        detail = detail or {}
        desc = detail.get('description', '')
        urls = list(dict.fromkeys(filter(None, (employer_url(u) for u in detail.get('apply_urls', [])))))
        target = urls[0] if len(urls) == 1 else ''
        reason = card_reason(card)
        state = 'excluded' if reason else 'needs_details'
        job = None
        if desc and not reason:
            loc = card['location']
            workplace = {'Remote':'remote','Hybrid':'hybrid','On-site':'onsite'}.get(detail.get('workplace'))
            workplace = workplace or ('remote' if re.search(r'\bremote\b', loc, re.I) else 'hybrid' if re.search(r'\bhybrid\b', loc, re.I) else 'onsite' if re.search(r'\bon.?site\b', loc, re.I) else 'unspecified')
            job = JobInput(title=card['title'], company=card['company'], location=loc,
                           url='https://www.linkedin.com/jobs/view/'+card['id']+'/', description=desc,
                           source='linkedin', external_id=card['id'], country=country(loc), workplace_type=workplace,
                           raw_payload=json.dumps({'employer_link_candidates':urls,'collector':'rendered_browser'}))
            decision = evaluate(job, ignore_salary=settings()['ignore_salary'])
            reason = application_guard(job) or (title_review_reason(job.title, desc) if settings().get('linkedin_title_filter_enabled', True) else '') or '; '.join(decision.reasons)
            if reason:
                state = 'needs_review' if re.search(r'confirmation|evidence|review|unknown',reason,re.I) else 'excluded'
            elif detail.get('easy_apply') and not target:
                state, reason = 'easy_apply_only', 'No employer application link exposed'
            elif target:
                state, reason = 'employer_link_found', 'Employer destination extracted; opening and eligibility still need employer verification'
            else:
                state, reason = 'needs_employer_link', 'No unambiguous employer href; no Apply button clicked'
            if state == 'employer_link_found' and decision.warnings:
                reason += '; ' + '; '.join(decision.warnings)
        stamp = datetime.now(timezone.utc).isoformat()
        with self.repo.connect() as db:
            previous = db.execute('SELECT description FROM linkedin_browser_jobs WHERE external_id=?', (card['id'],)).fetchone()
            # A card-only refresh must not erase previously extracted evidence.
            if previous and previous['description'] and not desc:
                db.execute('UPDATE linkedin_browser_jobs SET last_seen=? WHERE external_id=?', (stamp, card['id']))
                return False
            db.execute('''INSERT INTO linkedin_browser_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(external_id) DO UPDATE SET title=excluded.title,company=excluded.company,
                location=excluded.location,description=excluded.description,employer_url=excluded.employer_url,
                state=excluded.state,reason=excluded.reason,search_url=excluded.search_url,last_seen=excluded.last_seen''',
                (card['id'],card['title'],card['company'],card['location'],
                 'https://www.linkedin.com/jobs/view/'+card['id']+'/',desc,target,state,reason,search_url,stamp,stamp))
        if job and state in {'employer_link_found', 'needs_employer_link', 'easy_apply_only'}:
            # LinkedIn remains discovery-only in daily.py. A destination must still
            # be verified by the existing employer resolver before application use.
            job_id, _, _ = self.repo.upsert_job(job)
            self.repo.save_evaluation(job_id, decision, score(job))
        return previous is None

    @staticmethod
    def check_access(page):
        path = urlsplit(page.url).path
        if re.search(r'/(?:login|uas/login|checkpoint|authwall)', path):
            raise ValueError('LOGIN_OR_CHECKPOINT_REQUIRED: complete it in the application Chrome window')
        if page.locator('iframe[src*="captcha"],iframe[title*="CAPTCHA"],#captcha-internal').count():
            raise ValueError('CAPTCHA_REQUIRED: complete it in Chrome; collection stopped')
        if re.search(r'unusual activity|temporarily restricted|too many requests|verify you are human', page.locator('body').inner_text()[:4000], re.I):
            raise ValueError('ACCESS_RESTRICTION: collection stopped without retries')

    def run(self, endpoint, url=None, pages=1, detail_limit=5, delay=5, constraints=None):
        if not re.fullmatch(r'http://127\.0\.0\.1:\d+', endpoint):
            raise ValueError('Chrome endpoint must be local')
        if not 1 <= pages <= 20 or not 0 <= detail_limit <= 100 or delay < 1:
            raise ValueError('Use 1–20 pages, 0–100 details, and action interval >= 1 second')
        from playwright.sync_api import sync_playwright
        started = time.monotonic()
        report = {'pages':0,'cards':0,'new':0,'details':0,'state':'running','applications_submitted':0,'job_ids':[],
                  'skipped_cached':0,'skipped_excluded':0,'skipped_already_applied_or_held':0,'detail_failures':[]}
        if constraints:
            report['screening'] = {}
        with (self.repo.path.parent / 'daily.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Application/daily worker is active; do not share its browser concurrently')
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(endpoint)
                tabs = [pg for ctx in browser.contexts for pg in ctx.pages if urlsplit(pg.url).hostname in {'linkedin.com','www.linkedin.com'}]
                if not tabs and url:
                    tabs = [browser.contexts[0].new_page()]
                if not tabs:
                    raise ValueError('Open the signed-in LinkedIn search in the application browser first')
                page = next((pg for pg in tabs if '/jobs/search' in pg.url), tabs[0])
                denied = []
                page.on('response', lambda r: denied.append(r.status) if r.status == 429 or (r.status in {401,403} and r.request.resource_type == 'document') else None)
                search_url = clean_search_url(url or page.url)
                seen = set()
                try:
                    if url:
                        self.pace(delay)
                        page.goto(search_url, wait_until='domcontentloaded')
                    ready = self.wait_ready(page, lambda: page.evaluate(CARDS) or
                        re.search(r'No results found', page.locator('body').inner_text(), re.I), denied)
                    if not ready:
                        raise ValueError('Search results did not become ready; stopped without guessing')
                    report['filter_evidence'] = validate_search_filters(search_url, page.url, remote_filter_selected(page))
                    for _ in range(pages):
                        if denied:
                            raise ValueError('ACCESS_RESTRICTION: HTTP '+str(denied[0])+'; stopped without retries')
                        self.check_access(page)
                        report['filter_evidence'] = validate_search_filters(search_url, page.url, remote_filter_selected(page))
                        cards = {}
                        stable = 0
                        for _scroll in range(12):
                            self.check_access(page)
                            if denied:
                                raise ValueError('ACCESS_RESTRICTION: HTTP '+str(denied[0])+'; stopped without retries')
                            batch = page.evaluate(CARDS)
                            before = len(cards)
                            cards.update((c['id'],c) for c in batch)
                            stable = stable + 1 if len(cards)==before else 0
                            if stable >= 2 or not batch:
                                break
                            self.pace(delay)
                            self.scroll_card(page, batch[-1]['id'])
                            loaded_ids = set(cards)
                            self.wait_ready(page, lambda: any(c['id'] not in loaded_ids for c in page.evaluate(CARDS)), denied, timeout=2)
                        if not cards:
                            report['state'] = 'no_cards_or_layout_changed'
                            break
                        if set(cards).issubset(seen):
                            report['state'] = 'repeated_page_stopped'
                            break
                        report['pages'] += 1
                        skips = self.cached_skips(cards.values())
                        if constraints:
                            # Applicant counts and posting age require fresh rendered evidence.
                            skips = {key:value for key,value in skips.items() if value != 'cached'}
                        for card in cards.values():
                            if card['id'] in seen:
                                continue
                            seen.add(card['id'])
                            report['job_ids'].append(card['id'])
                            report['cards'] += 1
                            report['new'] += int(self.save(card, search_url))
                            if card['id'] in skips:
                                report['skipped_'+skips[card['id']]] += 1
                                continue
                            if report['details'] >= detail_limit:
                                continue
                            self.check_access(page)
                            previous = page.evaluate(DETAIL)
                            self.pace(delay)
                            page.locator('[componentkey="job-card-component-ref-'+card['id']+'"],li[data-occludable-job-id="'+card['id']+'"]').first.click()
                            detail = self.wait_detail(page, card['id'], previous, denied)
                            self.check_access(page)
                            if detail is None:
                                report['detail_failures'].append({'job_id':card['id'],
                                    'reason':'Detail panel did not become ready; no employer link inferred'})
                                print('Unresolved detail: '+card['id']+'; continuing to next card', flush=True)
                                continue
                            # Never associate a stale previous detail panel with a new card.
                            current = parse_qs(urlsplit(page.url).query).get('currentJobId',[''])[0]
                            if current != card['id']:
                                raise ValueError('Detail selection did not match card; stopping to avoid mismatched evidence')
                            if detail.get('selected_id') != card['id']:
                                raise ValueError('Detail panel identity not verified; stopping without guessing')
                            if not detail['description']:
                                raise ValueError('Description layout changed; stopping without guessing')
                            self.save(card, search_url, detail)
                            if constraints:
                                report['screening'][card['id']] = search_constraint_evidence(card, detail, constraints)
                            report['details'] += 1
                            print('Collected: '+card['title']+' — '+card['company'], flush=True)
                        if report['pages'] >= pages:
                            report['state'] = 'page_limit_reached'
                            break
                        nxt = page.get_by_role('button',name='Next',exact=True)
                        if not nxt.count() or not nxt.first.is_enabled():
                            report['state'] = 'end_of_results'
                            break
                        self.check_access(page)
                        self.pace(delay)
                        nxt.first.click()
                        if not self.wait_ready(page, lambda: any(c['id'] not in seen for c in page.evaluate(CARDS)), denied):
                            raise ValueError('Next results page did not become ready; stopped without guessing')
                except Exception as exc:
                    report['state'] = 'stopped'
                    report['error'] = str(exc).split('Call log:')[0][:400]
                # Disconnect Playwright only; leave the user's browser and login intact.
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        print(json.dumps(report, indent=2), flush=True)
        return report
