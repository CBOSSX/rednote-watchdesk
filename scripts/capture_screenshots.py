"""Render README screenshots from deterministic, synthetic state only.

No backend imports, existing browser profiles, local services or external requests.
Run: .venv/bin/python -m scripts.capture_screenshots
"""
import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

PROJECT = Path(__file__).resolve().parent.parent
WEB = PROJECT/'web'
DOCS = PROJECT/'docs'
ORIGIN = 'http://watchdesk-demo.test'
ASSETS = {
    '/': ('index.html', 'text/html'),
    '/js/app.js': ('js/app.js', 'text/javascript'),
    '/js/accounts.js': ('js/accounts.js', 'text/javascript'),
    '/js/motion.js': ('js/motion.js', 'text/javascript'),
    '/css/style.css': ('css/style.css', 'text/css'),
    '/css/motion.css': ('css/motion.css', 'text/css'),
}


def demo_state():
    actors = [
        {'id': 'demo_daily', 'label': '日常账号', 'name': '晴天', 'red_id': 'demo_sunny', 'ready': True},
        {'id': 'demo_work', 'label': '工作账号', 'name': '山间来信', 'red_id': 'demo_letter', 'ready': True},
    ]
    people = [
        {'id': 'studio', 'name': '山间工作室', 'candidates': ['demo_studio', 'demo_studio2'],
         'auto_follow': True, 'follow_account_ids': ['demo_daily', 'demo_work']},
        {'id': 'cafe', 'name': '街角咖啡', 'candidates': ['demo_cafe'],
         'auto_follow': True, 'follow_account_ids': ['demo_daily', 'demo_work']},
        {'id': 'books', 'name': '海边书店', 'candidates': ['demo_books'],
         'auto_follow': True, 'follow_account_ids': ['demo_work']},
    ]
    rows = [
        {'id': 'studio', 'matched_target': None, 'completed': False, 'follow_status': None,
         'last_checked_at': '2026-11-03T09:58:00+00:00', 'follow_jobs': []},
        {'id': 'cafe', 'matched_target': 'demo_cafe', 'completed': False, 'follow_status': 'pending',
         'last_checked_at': '2026-11-03T09:56:00+00:00',
         'follow_jobs': [{'account_id': 'demo_daily', 'status': 'succeeded'}, {'account_id': 'demo_work', 'status': 'pending'}]},
        {'id': 'books', 'matched_target': 'demo_books', 'completed': True, 'follow_status': 'succeeded',
         'last_checked_at': '2026-11-03T09:55:00+00:00',
         'follow_jobs': [{'account_id': 'demo_work', 'status': 'succeeded'}]},
    ]
    return {
        'config': {'interval_seconds': 1800, 'search_account_id': 'demo_daily', 'people': people},
        'version': 'synthetic-demo-version', 'pending_apply': False,
        'state': {'people': rows, 'paused': False, 'completed': False, 'follow_paused': False,
                  'heartbeat_at': datetime.datetime.fromisoformat('2026-11-03T10:00:00+00:00').timestamp(),
                  'pending_notifications': False},
        'accounts': actors, 'jobs': [],
        'notifications': {'configured': False, 'signing_configured': False, 'mention_users': []},
    }


def main():
    state, errors = demo_state(), []

    def respond(route):
        request = route.request
        url = urlsplit(request.url)
        if request.method != 'GET' or url.netloc != 'watchdesk-demo.test':
            errors.append('Blocked unexpected request: ' + request.method + ' ' + request.url)
            route.abort()
        elif url.path == '/api/state':
            route.fulfill(json=state)
        elif url.path == '/favicon.ico':
            route.fulfill(status=204)
        elif url.path in ASSETS:
            filename, mime = ASSETS[url.path]
            route.fulfill(content_type=mime, body=(WEB/filename).read_text().replace('__CSRF_TOKEN__', 'demo-only'))
        else:
            errors.append('Blocked unknown asset: ' + url.path)
            route.abort()

    DOCS.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': 1360, 'height': 1000}, device_scale_factor=2,
                                          reduced_motion='reduce', service_workers='block')
            context.route('**/*', respond)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(ORIGIN)
            page.evaluate('document.fonts.ready')
            expect(page.locator('.person-card')).to_have_count(3)
            expect(page.locator('#last-run')).to_have_text('18:00:00')
            expect(page.locator('#global-status')).to_have_text('监测已开启')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.mouse.move(0, 0)
            page.screenshot(path=str(DOCS/'screenshot.png'), full_page=True)

            heights = page.locator('.person-card').evaluate_all('nodes => nodes.map(n => n.getBoundingClientRect().height)')
            page.locator('.follow-picker').first.click()
            expect(page.locator('#follow-dialog')).to_be_visible()
            expect(page.locator('#follow-dialog input:checked')).to_have_count(2)
            assert heights == page.locator('.person-card').evaluate_all('nodes => nodes.map(n => n.getBoundingClientRect().height)')
            page.mouse.move(0, 0)
            # Capture the dialog itself so the README does not shrink it into a full-screen thumbnail.
            page.locator('#follow-dialog').screenshot(path=str(DOCS/'follower-selection.png'))
            page.locator('#cancel-follow-dialog').click()

            page.locator('[data-tab="accounts"]').click()
            expect(page.locator('.account-card')).to_have_count(2)
            expect(page.locator('.rename-account').first).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.mouse.move(0, 0)
            page.screenshot(path=str(DOCS/'accounts.png'), full_page=True)
            assert not errors, errors
        finally:
            browser.close()
    print('Updated three README screenshots using intercepted GET requests and synthetic data only.')


if __name__ == '__main__':
    main()
