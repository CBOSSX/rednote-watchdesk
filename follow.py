"""Follow an allowlisted exact Red ID using a separately enrolled Chrome profile."""
import contextlib
import fcntl
from pathlib import Path
import re
import time
import urllib.parse


class FollowProblem(Exception):
    def __init__(self, reason, *, blocked=False, retryable=False):
        super().__init__(reason)
        self.blocked, self.retryable = blocked, retryable


def profile_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != 'www.xiaohongshu.com' or not re.fullmatch(r'/user/profile/[a-fA-F0-9]{24}', parts.path):
        raise FollowProblem('follow_invalid_profile')
    return 'https://www.xiaohongshu.com' + parts.path


@contextlib.contextmanager
def follow_browser(data, headed=False):
    from playwright.sync_api import sync_playwright
    with (Path(data)/'follow-browser.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(Path(data)/'follow-browser-profile'), channel='chrome', headless=not headed,
                chromium_sandbox=True, viewport={'width': 1200, 'height': 950},
                locale='zh-CN', timezone_id='Asia/Shanghai', accept_downloads=False)
            context.set_default_timeout(10000)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield page
            finally:
                context.close()


def login_account(data, account, seconds, gate, fresh=False):
    with follow_browser(data, headed=True) as page:
        if fresh:
            page.context.clear_cookies()
        page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(2000)
        button = page.get_by_role('button', name='登录', exact=True)
        if button.count() and button.first.is_visible():
            button.first.click()
        print('BIG_ACCOUNT_LOGIN_WINDOW_READY', flush=True)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not page.is_closed():
            try:
                gate(page)
            except Exception:
                page.wait_for_timeout(2000)
                continue
            urls = set(page.locator('a[href*="/user/profile/"]').evaluate_all(
                "els=>els.filter(e=>e.innerText.trim()==='我').map(e=>e.href.split('?')[0])"))
            if len(urls) == 1:
                url = profile_url(urls.pop())
                if (account.get('profile_url') and url != account['profile_url']) or url == account.get('small_profile_url'):
                    raise FollowProblem('follow_account_mismatch', blocked=True)
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
                label = page.locator('.user-info .user-redId')
                label.wait_for(state='visible', timeout=15000)
                gate(page)
                red_id = re.search(r'小红书号\s*[：:]\s*([A-Za-z0-9_-]+)', label.inner_text())
                if not red_id:
                    raise FollowProblem('follow_account_not_ready', blocked=True)
                return dict(account, profile_url=url, red_id=red_id.group(1),
                            name=page.locator('.user-info .user-name').inner_text())
            page.wait_for_timeout(2000)
    raise FollowProblem('follow_account_not_ready', blocked=True)


def inspect_profile(page, account, found, target, gate):
    gate(page)
    expected = profile_url(account['profile_url'])
    identities = page.locator('a[href*="/user/profile/"]').evaluate_all(
        "els=>els.filter(e=>e.innerText.trim()==='我').map(e=>e.href.split('?')[0])")
    if not identities or set(identities) != {expected}:
        raise FollowProblem('follow_account_mismatch', blocked=True)
    if profile_url(page.url) != profile_url(found['url']):
        raise FollowProblem('follow_target_mismatch')
    label = page.locator('.user-info .user-redId')
    label.wait_for(state='visible', timeout=15000)
    red_id = re.search(r'小红书号\s*[：:]\s*([A-Za-z0-9_-]+)', label.inner_text())
    if not red_id or red_id.group(1).casefold() != target.casefold():
        raise FollowProblem('follow_target_mismatch')
    button = page.locator('.user-info button.follow-button')
    if button.count() != 1 or not button.is_visible():
        raise FollowProblem('follow_button_unavailable', retryable=True)
    label = re.sub(r'\s+', '', button.inner_text())
    if label in ('已关注', '互相关注', '相互关注'):
        return 'followed', button
    if label in ('关注', '+关注', '＋关注'):
        return 'not_followed', button
    raise FollowProblem('follow_button_unavailable', retryable=True)


def follow_once(data, account, found, target, allowed_targets, checkpoint, gate, *, verify_only=False):
    if target not in allowed_targets or found.get('red_id', '').casefold() != target.casefold():
        raise FollowProblem('follow_not_allowlisted')
    url = profile_url(found['url'])
    if url == profile_url(account['profile_url']):
        raise FollowProblem('follow_self_refused')
    with follow_browser(data) as page:
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
            if response and response.status in (401, 403, 429):
                raise FollowProblem('login_required' if response.status == 401 else 'http_' + str(response.status), blocked=True)
            page.wait_for_timeout(1500)
            state, button = inspect_profile(page, account, found, target, gate)
            if state == 'followed':
                return {'status': 'already_followed', 'actor_name': account['name']}
            if verify_only:
                raise FollowProblem('follow_not_confirmed')
            # Persist intent BEFORE the write. A crash/timeout resumes in read-only verification mode.
            checkpoint()
            button.click(timeout=10000)
            page.wait_for_timeout(1500)
            gate(page)
            page.reload(wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(1500)
            state, _ = inspect_profile(page, account, found, target, gate)
            if state != 'followed':
                raise FollowProblem('follow_not_confirmed')
            return {'status': 'followed', 'actor_name': account['name']}
        except Exception:
            # A challenge may appear while a click or navigation is timing out.
            try:
                gate(page)
            except FollowProblem:
                raise
            except Exception as gate_error:
                if type(gate_error).__name__ == 'SitePaused':
                    raise
            raise
