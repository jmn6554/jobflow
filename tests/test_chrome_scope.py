import json
import socket
from urllib.request import urlopen

import pytest
from playwright.sync_api import sync_playwright

from jobflow.chrome_scope import ScopedChromeConnection


def test_scoped_connection_leaves_unrelated_tabs_and_handles_new_tabs():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    endpoint = f'http://127.0.0.1:{port}'
    with sync_playwright() as p:
        owner = p.chromium.launch(headless=True, args=[f'--remote-debugging-port={port}'])
        try:
            context = owner.new_context()
            unrelated = context.new_page()
            unrelated.goto('data:text/html,unrelated')
            chosen = context.new_page()
            chosen.goto('data:text/html,<input id="answer"><iframe src="about:blank"></iframe>')
            with urlopen(endpoint + '/json/list') as response:
                targets = json.load(response)
            target_id = next(t['id'] for t in targets if 'answer' in t['url'])
            with ScopedChromeConnection(endpoint, target_id) as scoped_url:
                client = p.chromium.connect_over_cdp(scoped_url, timeout=10000)
                pages = [page for c in client.contexts for page in c.pages]
                assert len(pages) == 1
                pages[0].locator('#answer').fill('saved answer')
                assert len(pages[0].frames) == 2
                assert chosen.locator('#answer').input_value() == 'saved answer'
                pages[0].set_content('<button onclick="window.open(\'about:blank\')">Open</button>')
                with client.contexts[0].expect_page() as popup_event:
                    pages[0].get_by_text('Open', exact=True).click()
                popup = popup_event.value
                popup.set_content('<input id="popup-answer">')
                popup.locator('#popup-answer').fill('saved popup answer')
                popup.close()
                new_page = client.contexts[0].new_page()
                new_page.goto('data:text/html,owned new tab')
                assert len(client.contexts[0].pages) == 2
                new_page.close()
                client.close()  # CDP disconnect, not Browser.close.
            assert unrelated.title() == ''
            assert not chosen.is_closed()
            assert len(context.pages) == 2
        finally:
            owner.close()


@pytest.mark.parametrize('url', ['http://example.com:9222', 'https://127.0.0.1:9222', 'http://127.0.0.1'])
def test_only_loopback_chrome_allowed(url):
    with pytest.raises(ValueError):
        ScopedChromeConnection(url, 'target')
