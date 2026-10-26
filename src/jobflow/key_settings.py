"""Local-only secret entry; never renders or logs the submitted value."""
import os
import re
import secrets
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


def install_key_settings(app, env_path):
    token = secrets.token_urlsafe(32)
    headers = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
               'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}

    def local(request):
        if (not request.client or request.client.host not in {'127.0.0.1', '::1', 'testclient'}
                or request.url.hostname not in {'127.0.0.1', 'localhost', '::1', 'testserver'}):
            raise HTTPException(403, 'Use the local dashboard on your Mac.')

    @app.get('/settings/tavily')
    def key_form(request: Request):
        local(request)
        saved = '<p role="status">Key saved locally. You can close this page.</p>' if request.query_params.get('saved') == '1' else ''
        return HTMLResponse('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Tavily · JobFlow</title>
<style>body{font:17px system-ui;background:#f3f6f8;color:#17212b;padding:24px}main{max-width:440px;margin:40px auto;background:white;padding:28px;border-radius:16px}input,button{box-sizing:border-box;width:100%;padding:14px;margin-top:14px;font:inherit;border:1px solid #aaa;border-radius:8px}button{background:#087e8b;color:white}p{line-height:1.5}</style></head>
<body><main><h1>Connect Tavily</h1><p>Paste your free-account API key. It stays on this Mac and is never displayed back to you.</p>'''
            + saved + '<form method="post" action="/settings/tavily" autocomplete="off">'
            + '<input type="hidden" name="csrf" value="' + token + '">'
            + '''<label for="api-key">Tavily API key</label><input id="api-key" name="api_key" type="password" required maxlength="256" autocomplete="new-password" autocapitalize="none" spellcheck="false" placeholder="tvly-…">
<button type="submit">Save key securely</button></form><p>900 search attempts/month maximum. Use a free account with no billing enabled. No paid fallback.</p></main></body></html>''', headers=headers)

    @app.post('/settings/tavily')
    async def save_key(request: Request):
        local(request)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Invalid request origin')
        # Bound the body before parsing; no validation errors echo submitted secrets.
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4096:
                raise HTTPException(413, 'Form too large')
            chunks.append(chunk)
        # Parse only URL-encoded data, not multipart uploads.
        from urllib.parse import parse_qs
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(400, 'Invalid form')
        form = parse_qs(b''.join(chunks).decode('utf-8', errors='replace'))
        if not secrets.compare_digest(form.get('csrf', [''])[0], token):
            raise HTTPException(403, 'Reload the form and try again')
        key = form.get('api_key', [''])[0].strip()
        if not re.fullmatch(r'tvly-[A-Za-z0-9_-]{10,251}', key):
            raise HTTPException(400, 'Enter a valid Tavily key beginning with tvly-')
        try:
            save_env_key(env_path, key)
        except (OSError, ValueError):
            raise HTTPException(500, 'Could not save the key locally') from None
        os.environ['TAVILY_API_KEY'] = key
        return RedirectResponse('/settings/tavily?saved=1', status_code=303, headers=headers)


def save_env_key(path, key):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing symlink')
    previous = path.read_text() if path.exists() else ''
    lines = [line for line in previous.splitlines()
             if not re.match(r'^\s*(?:export\s+)?TAVILY_API_KEY\s*=', line)]
    lines.append('TAVILY_API_KEY=' + key)
    fd, temporary = tempfile.mkstemp(prefix='.env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(lines) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
