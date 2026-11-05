#!/usr/bin/env python3
"""Multi-account exact Red ID search with a persistent browser and Feishu webhook."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from rednote_watchdesk.retry_policy import query_due, retry_delay
from rednote_watchdesk.follow_jobs import run_follow_jobs
from rednote_watchdesk.cards import notification_payload
from rednote_watchdesk.paths import RUNTIME
from rednote_watchdesk.watch_config import targets as plan_targets, normalize, signature
from rednote_watchdesk.group_state import apply_people, sync_people, complete_people
from rednote_watchdesk.query_diagnostics import describe, safe_detail, safe_url
from rednote_watchdesk import pause_control, query_progress

ROOT = RUNTIME
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
STATE = DATA / "state.json"
RED_ID = re.compile(r"小红书号\s*[：:]\s*([A-Za-z0-9_-]+)")
IP_RISK = ("IP存在风险", "网络环境存在风险")
RISK = ("安全限制", "访问过于频繁", "操作过于频繁", "操作频繁", "访问频繁", "安全验证", "请完成验证", "验证后继续", "账号异常", "访问异常", "异常流量", "滑动验证", "拖动滑块", "请依次点击")
EMPTY = ("没有找到相关用户", "暂无相关用户", "暂无搜索结果", "未找到相关用户", "没有找到你想要的", "没有搜到相关")
LOG = logging.getLogger("xhs-watch")
SEARCH_ACCOUNT_BLOCKS = {'login_required', 'search_account_not_ready', 'search_account_mismatch'}
_progress = None


def report_progress(**fields):
    if _progress is not None:
        _progress.update(**fields)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    value = json.loads(path.read_text())
    return pause_control.apply(DATA, value) if path == STATE else value


def write_json(path, obj):
    if path == STATE:
        pause_control.apply(DATA, obj)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp." + str(os.getpid()))
    with tmp.open("w") as f:
        os.chmod(tmp, 0o600)
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)
    health = os.environ.get("REDNOTE_WATCHDESK_HEALTH_DIR")
    if path == STATE and health:
        fields = ("paused", "completed", "query_complete", "follow_pending", "configured_targets", "last_attempt_epoch", "last_status", "last_checked_at", "resumed_at")
        with contextlib.suppress(OSError):
            write_json(Path(health) / "state.json", {k: obj[k] for k in fields if k in obj})


def parse_result(text, href):
    match = RED_ID.search(text)
    if not match:
        return None
    url = urllib.parse.urlsplit(urllib.parse.urljoin("https://www.xiaohongshu.com", href))
    if url.scheme != "https" or url.hostname not in ("www.xiaohongshu.com", "xiaohongshu.com"):
        return None
    if not re.fullmatch(r"/user/profile/[a-fA-F0-9]{24}/?", url.path):
        return None
    return {"red_id": match.group(1), "name": text.splitlines()[0].strip(),
            "url": "https://www.xiaohongshu.com" + url.path.rstrip("/")}


def exact_result(rows, target):
    for row in rows:
        item = parse_result(row["text"], row["href"])
        if item and item["red_id"].casefold() == target.casefold():
            return item
    return None


class SitePaused(Exception):
    pass


def visible_body(page, retry=True):
    timeouts = (5000, 10000) if retry else (5000,)
    for attempt, timeout in enumerate(timeouts, 1):
        previous = getattr(page, '_xhs_body_attempts', 0)
        page._xhs_body_attempts = max(previous if isinstance(previous, int) else 0, attempt)
        try:
            return page.locator("body").inner_text(timeout=timeout)
        except Exception as error:
            if 'Timeout' not in type(error).__name__ or attempt == len(timeouts):
                raise
            LOG.info('body_read_retry stage=%s attempt=%s', getattr(page, '_xhs_stage', 'unknown'), attempt)
            report_progress(body_retry=True)


def set_stage(page, stage):
    page._xhs_stage = stage
    page._xhs_stage_started = time.monotonic()
    page._xhs_body_attempts = 0
    report_progress(stage=stage, body_retry=False)


def navigate(page, url, stage):
    set_stage(page, stage)
    page._xhs_http_status = None
    response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
    page._xhs_http_status = response.status if response else None
    check_response(response)
    return response


def check_gate(page, retry_body=True):
    text = visible_body(page, retry=retry_body)
    if any(word in text for word in IP_RISK):
        raise SitePaused("ip_risk")
    if any(word in text for word in RISK):
        raise SitePaused("risk_control")
    if "登录后查看搜索结果" in text or "扫码登录" in text or "手机号登录" in text:
        raise SitePaused("login_required")
    if page.get_by_role("button", name="登录", exact=True).count():
        if page.get_by_role("button", name="登录", exact=True).first.is_visible():
            raise SitePaused("login_required")
    if urllib.parse.urlsplit(page.url).hostname not in ("www.xiaohongshu.com", "xiaohongshu.com"):
        # Includes about:blank and Chrome's error page after network failures.
        # A redirect without a visible login/risk gate is retryable.
        raise RuntimeError("unexpected_redirect")
    return text


@contextlib.contextmanager
def browser_page(headed=False, account_id=None):
    if account_id is not None:
        from rednote_watchdesk.accounts import browser, require_account
        account = require_account(DATA, account_id)
        if not account.get("profile_url"):
            raise SitePaused("search_account_not_ready")
        with browser(DATA, account_id, headed) as page:
            try:
                navigate(page, 'https://www.xiaohongshu.com/explore', 'navigate_home')
                page.wait_for_timeout(1500)
                set_stage(page, 'homepage_access')
                check_gate(page)
                set_stage(page, 'verify_identity')
                identities = page.locator('a[href*="/user/profile/"]').evaluate_all(
                    "els=>els.filter(e=>e.innerText.trim()==='我').map(e=>e.href.split('?')[0])")
                if set(identities) != {account["profile_url"]}:
                    raise SitePaused("search_account_mismatch")
            except Exception as error:
                # Capture while the page is still alive; enter_context otherwise loses it.
                error._xhs_diagnostic = record_diagnostic(page, None, error)
                report_progress(stage='closing_browser')
                raise
            try:
                yield page
            finally:
                report_progress(stage='closing_browser')
        return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        config = read_json(CONFIG)
        context = p.chromium.launch_persistent_context(
            str(DATA / "browser-profile"), channel="chrome",
            headless=False if headed else config.get("headless", True), chromium_sandbox=True,
            viewport={"width": 1440, "height": 1000}, locale="zh-CN", timezone_id="Asia/Shanghai",
            accept_downloads=False)
        context.set_default_timeout(10000)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield page
        finally:
            report_progress(stage='closing_browser')
            context.close()


def search(page, target):
    try:
        return search_users(page, target)
    except SitePaused:
        raise
    except Exception:
        # A login dialog or challenge can appear while a click/navigation times out.
        # Recheck visible gates before treating the failure as transient.
        try:
            check_gate(page, retry_body=False)
        except SitePaused:
            raise
        except Exception:
            pass
        raise


def check_response(response):
    if response and response.status == 401:
        raise SitePaused("login_required")
    if response and response.status in (403, 429):
        raise SitePaused("http_" + str(response.status))
    if response and response.status >= 400:
        raise RuntimeError('http_' + str(response.status))


def search_users(page, target):
    url = "https://www.xiaohongshu.com/search_result_ai?" + urllib.parse.urlencode(
        {"keyword": target, "source": "web_explore_feed"})
    navigate(page, url, 'navigate_search')
    page.wait_for_timeout(2000)
    set_stage(page, 'check_access')
    check_gate(page)
    set_stage(page, 'select_users')
    page.get_by_text("用户", exact=True).click(timeout=15000)
    set_stage(page, 'wait_user_results')
    deadline = time.monotonic() + 40
    rows = []
    while time.monotonic() < deadline:
        text = check_gate(page)
        rows = page.locator('a[href*="/user/profile/"]').evaluate_all(
            "els => els.filter(e => e.innerText.includes('小红书号')).map(e => ({text:e.innerText,href:e.href}))")
        if rows:
            # A loading skeleton is never treated as a negative result.
            result = exact_result(rows, target)
            return {"status": "found" if result else "not_found", "result": result,
                    "result_count": len(rows), "checked_at": now()}
        if any(word in text for word in EMPTY):
            return {"status": "not_found", "result": None, "result_count": 0, "checked_at": now()}
        page.wait_for_timeout(1000)
    raise RuntimeError("Search did not produce user rows or an explicit empty result")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Webhook redirect refused")


def webhook(text, *, event=None):
    secret = read_json(ROOT / "secrets.json")
    url = secret["webhook_url"]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ("open.larkoffice.com", "open.feishu.cn", "open.larksuite.com"):
        raise ValueError("Webhook must use an official HTTPS bot endpoint")
    if not parts.path.startswith("/open-apis/bot/v2/hook/"):
        raise ValueError("Invalid webhook path")
    payload = notification_payload(text, secret, event)
    if secret.get("signing_secret"):
        timestamp = str(int(time.time()))
        key = (timestamp + "\n" + secret["signing_secret"]).encode()
        payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    # Do not log URLs, exception messages containing URLs, or secret payloads.
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            result = json.load(response)
    except Exception as error:
        raise RuntimeError("Webhook transport failed: " + type(error).__name__) from None
    code = result.get("code", result.get("StatusCode"))
    if type(code) is not int or code != 0:
        raise RuntimeError("Webhook rejected message, code=" + str(code))


def configured_targets(config):
    return plan_targets(config)


def migrate_state(state, config):
    targets = configured_targets(config)
    if 'targets' not in state:
        state['targets'] = {}
        if targets:
            original = config.get('target', targets[0])
            fields = ('last_attempt_epoch', 'last_status', 'last_checked_at', 'consecutive_errors',
                      'last_error', 'found', 'completed')
            state['targets'][original] = {k: state[k] for k in fields if k in state}
    apply_people(state, config)
    if targets and (state.get('pending') or {}).get('kind') == 'found' and 'target' not in state['pending']:
        state['pending']['target'] = config.get('target', targets[0])
    sync_completion(state)
    return state


def sync_completion(state):
    sync_people(state)
    entries = [state['targets'][t] for t in state['configured_targets']]
    if state.get('multi_account'):
        from rednote_watchdesk.follow_state import sync_multi_follows
        sync_multi_follows(state)
        complete_people(state)
        return
    if state.get('auto_follow_enabled'):
        for target in state['auto_follow_targets']:
            entry = state['targets'][target]
            if not entry.get('found'):
                continue
            if 'follow' not in entry:
                entry['discovery_notified'] = bool(entry.get('completed'))
                entry['follow'] = {'status': 'pending'}
            job = entry['follow']
            entry['completed'] = bool(entry.get('discovery_notified') and job.get('result_notified')
                                      and job.get('status') in ('succeeded', 'failed', 'blocked'))
    state['follow_pending'] = bool(state.get('auto_follow_enabled') and not state.get('follow_paused')
        and any(state['targets'][t].get('follow', {}).get('status') in ('pending', 'running', 'retry')
                for t in state.get('auto_follow_targets', [])))
    state['completed'] = all(e.get('completed', False) for e in entries)
    state['query_complete'] = all(bool(e.get('completed') or e.get('found')) for e in entries)
    if 'people' in state:
        for person in state['people'].values():
            winner = person.get('matched_target')
            if winner and not person['auto_follow']:
                entry = state['targets'][winner]
                entry['completed'] = bool(entry.get('discovery_notified') or entry.get('completed'))
        complete_people(state)


def enqueue(state, event):
    target = event.get('target')
    person = state.get('people', {}).get(state.get('targets', {}).get(target, {}).get('person_id'), {})
    if person:
        event.setdefault('person_id', person['id'])
        event.setdefault('person_name', person['name'])
    event.setdefault('interval_seconds', state.get('interval_seconds', 1800))
    event.setdefault('created_at', now())
    event.setdefault('targets', list(state.get('configured_targets', [])))
    event.setdefault('auto_follow_enabled', bool(state.get('auto_follow_enabled') and
                     event.get('target') in state.get('auto_follow_targets', [])))
    if not state.get('pending'):
        state['pending'] = event
    else:
        state.setdefault('notification_queue', []).append(event)


def deliver(state):
    for _ in range(4):
        pending = state.get('pending')
        if not pending:
            return True
        try:
            report_progress(stage='deliver_notifications')
            webhook(pending['text'], event=pending)
        except Exception as error:
            state['delivery_error'] = str(error)
            state['delivery_attempts'] = state.get('delivery_attempts', 0) + 1
            write_json(STATE, state)
            LOG.error('delivery_failed attempts=%s reason=%s', state['delivery_attempts'], error)
            return False
        state['last_delivered_at'] = now()
        if pending['kind'] == 'found':
            entry = state['targets'][pending['target']]
            entry['discovery_notified'] = True
            if not state.get('auto_follow_enabled') or pending['target'] not in state.get('auto_follow_targets', []):
                entry['completed'] = True
            sync_completion(state)
        elif pending['kind'] in ('follow_success', 'follow_failure'):
            entry = state['targets'][pending['target']]
            if state.get('multi_account'):
                job = entry.get('follows', {}).get(pending.get('account_id', 'legacy_follow'), {})
            else:
                job = entry['follow']
            if job.get('status') == pending.get('follow_status') and job['status'] in ('succeeded', 'failed', 'blocked'):
                job['result_notified'] = True
            sync_completion(state)
        state.pop('pending', None)
        state.pop('delivery_error', None)
        queue = state.get('notification_queue', [])
        if queue:
            state['pending'] = queue.pop(0)
        write_json(STATE, state)
        LOG.info('delivery_ok kind=%s target=%s', pending['kind'], pending.get('target', 'all'))
    return not state.get('pending')


def record_diagnostic(page, target, error):
    report_progress(stage='diagnostics')
    detail = dict(getattr(error, '_xhs_diagnostic', {}) or {})
    if not detail:
        detail = {'stage': getattr(page, '_xhs_stage', 'launch_browser'),
                  'type': type(error).__name__, 'detail': safe_detail(error), 'checked_at': now()}
        if page is not None:
            detail.update(url=safe_url(getattr(page, 'url', '')),
                          http_status=getattr(page, '_xhs_http_status', None),
                          body_attempts=getattr(page, '_xhs_body_attempts', 0))
            started = getattr(page, '_xhs_stage_started', None)
            if isinstance(started, (float, int)):
                detail['elapsed_seconds'] = round(time.monotonic() - started, 2)
            with contextlib.suppress(Exception):
                detail['page_state'] = page.locator('html').evaluate(
                    "() => ({ready_state:document.readyState,body_present:!!document.body})", timeout=1000)
        detail.update(describe(detail))
    detail['target'] = target
    if page is None and target and detail.get('screenshot') == 'last-error.png':
        # Initialization captured the image before the caller knew the target.
        detail.pop('screenshot')
        with contextlib.suppress(OSError):
            filename = 'last-error-' + target + '.png'
            path = DATA / filename
            path.write_bytes((DATA / 'last-error.png').read_bytes())
            path.chmod(0o600)
            detail['screenshot'] = filename
    # Do not let an older screenshot masquerade as evidence for this failure.
    if page is not None:
        detail.pop('screenshot', None)
        with contextlib.suppress(Exception):
            filename = 'last-error-' + target + '.png' if target else 'last-error.png'
            path = DATA / filename
            path.unlink(missing_ok=True)
            page.screenshot(path=str(path), timeout=3000)
            path.chmod(0o600)
            detail['screenshot'] = filename
    with contextlib.suppress(Exception):
        write_json(DATA / 'last-error-detail.json', detail)
        if target:
            write_json(DATA / ('last-error-' + target + '.json'), detail)
    return detail


def config_unchanged(config):
    path = ROOT/'config.json'
    if not path.exists():
        return True
    try:
        return signature(read_json(path)) == signature(config)
    except (ValueError, OSError):
        return False


def finish_follows(state, config, cancelled=lambda: False):
    # Serialize config saves against browser writes, so disabling follow cannot race a click.
    with (DATA/'config.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cancelled() or not config_unchanged(config):
            return
        report_progress(stage='auto_follow')
        run_follow_jobs(state, config, data=DATA, enqueue=enqueue,
                        save=lambda s: write_json(STATE, s), gate=check_gate, site_paused=SitePaused,
                        cancelled=cancelled)
    sync_completion(state)
    write_json(STATE, state)


def search_batch(config, account_ids, due, state=None, force=False, cancelled=lambda: False):
    """Yield results in target order, keeping the failed target at the cursor on failover."""
    search_state = state.get('search', {}) if state is not None else {}
    errors = search_state.setdefault('errors', {})
    account_details = search_state.setdefault('error_details', {})
    active = search_state.get('active_account_id')
    if active in account_ids:
        index = account_ids.index(active)
        account_ids = account_ids[index:] + account_ids[:index]
    candidates = iter(due)
    target = next(candidates, None)
    retryable = None

    def eligible(t):
        return state is None or query_due(state['targets'][t], time.time(), config.get('interval_seconds', 1800), force=force)

    for account_index, account_id in enumerate(account_ids, 1):
        if cancelled() or (state is not None and not config_unchanged(config)):
            return
        with contextlib.ExitStack() as browser:
            try:
                report_progress(stage='launch_browser', target=target, account_id=account_id,
                                account_index=account_index, account_total=len(account_ids))
                page = browser.enter_context(browser_page(account_id=account_id) if account_id else browser_page())
            except Exception as error:
                failure = (error, record_diagnostic(None, target, error))
            else:
                while target is not None:
                    if cancelled() or (state is not None and not config_unchanged(config)):
                        return
                    if eligible(target):
                        try:
                            report_progress(target=target)
                            outcome = search(page, target)
                        except Exception as error:
                            failure = (error, record_diagnostic(page, target, error))
                            if len(account_ids) > 1 or isinstance(error, SitePaused):
                                break
                            yield target, None, failure
                        else:
                            if account_id:
                                search_state['active_account_id'] = account_id
                                errors.pop(account_id, None)
                                account_details.pop(account_id, None)
                            yield target, outcome, None
                    target = next(candidates, None)
                if target is None:
                    return
        error, detail = failure
        reason = str(error) if isinstance(error, SitePaused) else type(error).__name__
        if account_id:
            search_state.pop('active_account_id', None)
            errors[account_id] = reason
            account_details[account_id] = detail
            if state is not None:
                LOG.warning('search_account_failed account=%s target=%s stage=%s code=%s reason=%s',
                            account_id, target, detail['stage'], detail['code'], reason)
                write_json(STATE, state)
        if isinstance(error, SitePaused):
            if reason not in SEARCH_ACCOUNT_BLOCKS:
                yield target, None, failure
                return
        else:
            retryable = failure

    failure = retryable or failure
    if len(account_ids) > 1 and isinstance(failure[0], SitePaused):
        failure = (SitePaused('search_accounts_unavailable'), failure[1])
    yield target, None, failure
    if not isinstance(failure[0], SitePaused):
        for target in candidates:
            if eligible(target):
                yield target, None, failure


def run_check(config, dry_run=False, target=None, force=False, pause_token=None):
    global _progress
    _progress = None
    initial_pause = pause_control.token(DATA) if pause_token is None else pause_token

    def cancelled():
        current = pause_control.token(DATA)
        return bool(current and current != initial_pause)
    account_ids = normalize(config).get('search_account_ids', [None])
    if dry_run:
        if not account_ids:
            print(json.dumps({'status': 'paused', 'reason': 'search_account_not_ready'}))
            return 2
        probe = target or configured_targets(config)[0]
        with contextlib.closing(search_batch(config, account_ids, [probe])) as batch:
            _, outcome, failure = next(batch)
        if failure:
            error, _ = failure
            print(json.dumps({'status': 'paused' if isinstance(error, SitePaused) else 'error',
                              'reason': str(error) if isinstance(error, SitePaused) else type(error).__name__}))
            return 2
        print(json.dumps(outcome, ensure_ascii=False))
        return 0
    state = migrate_state(read_json(STATE), config)
    write_json(STATE, state)
    if force:
        _progress = query_progress.Progress(DATA, manual=True)
    manual_started = now()

    def done(code, successful=0, failed=0, found=0, message=None):
        global _progress
        if force:
            pause_control.apply(DATA, state)
            summary = {'started_at': manual_started, 'finished_at': now(), 'successful': successful,
                       'failed': failed, 'found': found, 'paused': state.get('paused', False)}
            if message:
                summary['message'] = message
            state['manual_check'] = summary
            write_json(STATE, state)
            print(json.dumps({'manual_check': summary}, ensure_ascii=False), flush=True)
        if _progress is not None:
            _progress.finish(code, cancelled=cancelled())
            _progress = None
        return code

    if cancelled():
        return done(0, message='已暂停，本轮调度已取消')
    delivery_failed = False
    if state.get('pending'):
        delivery_failed = not deliver(state)
    if (state.get('paused') and not force) or state.get('completed'):
        return done(0, message='没有待查询目标，已完成的目标不会重复查询')
    current = time.time()
    interval = config.get('interval_seconds', 1800)
    due = [t for t in state['configured_targets']
           if query_due(state['targets'][t], current, interval, force=force)]
    if _progress is None and due:
        _progress = query_progress.Progress(DATA)
    report_progress(total=len(due))
    if force and due and not account_ids:
        return done(2, message='未配置搜索账号，请先在登录账号页面设置')
    if not due or not account_ids:
        finish_follows(state, config, cancelled=cancelled)
        if not delivery_failed:
            deliver(state)
        return done(0, message='本轮调度完成，没有待查询目标')
    state['last_attempt_epoch'] = current
    write_json(STATE, state)
    outcomes = []
    successful = []
    blocked = False

    def failed(t, error, detail):
        nonlocal blocked
        entry = state['targets'][t]
        reason = str(error) if isinstance(error, SitePaused) else type(error).__name__
        entry.update(last_status='paused' if isinstance(error, SitePaused) else 'error',
                     last_error=reason, last_error_detail=detail, last_checked_at=now())
        entry['consecutive_errors'] = entry.get('consecutive_errors', 0) + 1
        state['last_error'] = reason
        outcomes.append('error')
        LOG.warning('check_error target=%s stage=%s code=%s reason=%s http_status=%s body_attempts=%s',
                    t, detail['stage'], detail['code'], reason, detail.get('http_status'), detail.get('body_attempts'))
        if isinstance(error, SitePaused):
            blocked = True
            state.update(paused=True, pause_reason=reason)
            enqueue(state, {'kind': 'attention', 'reason': reason, 'diagnostic': detail, 'text': '【小红书账号监测】已暂停全部查询：' + reason +
                '。监听：' + '、'.join(state['configured_targets']) + '。请在本机检查或重新登录后恢复。'})
        else:
            delay = retry_delay(entry['consecutive_errors'])
            entry['next_retry_epoch'] = time.time() + delay
            if entry['consecutive_errors'] >= 3 and not entry.get('query_fault_notified'):
                entry['query_fault_notified'] = True
                enqueue(state, {'kind': 'query_fault', 'target': t, 'reason': reason, 'diagnostic': detail,
                    'consecutive_errors': entry['consecutive_errors'], 'next_retry_epoch': entry['next_retry_epoch'], 'text':
                    '【小红书账号监测·查询异常】' + t + ' 连续3次查询失败：' + reason +
                    '，阶段：' + detail['stage'] + '。未暂停，继续自动重试；恢复成功后通知。'})
        write_json(STATE, state)

    with contextlib.closing(search_batch(config, account_ids, due, state, force=force, cancelled=cancelled)) as batch:
        for t, outcome, failure in batch:
            entry = state['targets'][t]
            entry['last_attempt_epoch'] = time.time()
            if failure:
                error, detail = failure
                failed(t, error, detail if detail['target'] == t else dict(detail, target=t))
            else:
                entry.update(last_status=outcome['status'], last_checked_at=outcome['checked_at'],
                             result_count=outcome['result_count'], consecutive_errors=0)
                entry.pop('last_error', None)
                entry.pop('last_error_detail', None)
                entry.pop('next_retry_epoch', None)
                if entry.pop('query_fault_notified', False):
                    enqueue(state, {'kind': 'query_recovery', 'target': t, 'text':
                        '【小红书账号监测·查询已恢复】' + t + ' 查询已验证成功，恢复约每半小时检查。'})
                outcomes.append(outcome['status'])
                successful.append(t)
                if outcome['status'] == 'found':
                    item = outcome['result']
                    entry['found'] = item
                    enqueue(state, {'kind': 'found', 'target': t, 'result': item, 'text':
                        '【小红书账号监测】发现小红书号精确匹配：' + item['red_id'] +
                        '\n昵称：' + item['name'] + '\n主页：' + item['url'] +
                        '\n请确认本人及店铺归属。此账号通知成功后停止查询，其他账号继续监听。'})
                sync_completion(state)
                write_json(STATE, state)
                LOG.info('checked target=%s status=%s results=%s', t, outcome['status'], outcome['result_count'])
            report_progress(stage='record_result', completed=len(outcomes), successful=len(successful),
                            failed=outcomes.count('error'), found=outcomes.count('found'))
    if successful and state.pop('recovery_pending', False):
        enqueue(state, {'kind': 'recovery', 'text': '【小红书账号监测·已恢复】查询已验证成功：' +
            '、'.join(successful) + '。当前监听：' + '、'.join(state['configured_targets']) + '，约每半小时检查。'})
    if state.get('auto_follow_enabled') and not delivery_failed and state.get('pending'):
        delivery_failed = not deliver(state)
    if (not state.get('paused') or force) and not blocked and not cancelled():
        finish_follows(state, config, cancelled=cancelled)
    sync_completion(state)
    remaining = [t for t in state['configured_targets'] if not state['targets'][t].get('paused')
                 and not state['targets'][t].get('found') and not state['targets'][t].get('completed') and not state['targets'][t].get('suppressed_by')]
    if not remaining and not state['query_complete'] and not state['completed']:
        state.update(paused=True, pause_reason='all_targets_paused')
    state['last_status'] = ('paused' if blocked or (state.get('paused') and not force) else
        'partial_error' if 'error' in outcomes and successful else
        'error' if 'error' in outcomes else 'found' if 'found' in outcomes else 'not_found')
    state['last_checked_at'] = now()
    state['consecutive_errors'] = max(e.get('consecutive_errors', 0) for e in state['targets'].values())
    if successful and 'error' not in outcomes:
        state.pop('last_error', None)
    write_json(STATE, state)
    if not delivery_failed:
        deliver(state)
    return done(2 if 'error' in outcomes else 0, successful=len(successful),
                failed=outcomes.count('error'), found=outcomes.count('found'),
                message='已暂停，当前步骤已收尾，后续任务已停止' if cancelled() else None)


def login(seconds, fresh=False):
    with browser_page(headed=True) as page:
        if fresh:
            page.context.clear_cookies()
        response = page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        if response and response.status >= 400:
            page.screenshot(path=str(DATA / "login.png"))
            print("LOGIN_PAGE_HTTP_" + str(response.status), flush=True)
            return 2
        button = page.get_by_role("button", name="登录", exact=True)
        if button.count() and button.first.is_visible():
            button.first.click()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            text = visible_body(page)
            page.screenshot(path=str(DATA / "login.png"))
            if any(word in text for word in IP_RISK):
                print("LOGIN_BLOCKED_IP_RISK", flush=True)
                return 2
            if any(word in text for word in RISK):
                # Leave the visible browser available for the user's own verification.
                print("LOGIN_NEEDS_USER_VERIFICATION", flush=True)
                page.wait_for_timeout(5000)
                continue
            logged_in = page.locator('a[href*="/user/profile/"]').count() > 0
            # The logged-in navigation includes one's profile. Login modal must be gone.
            login_visible = "手机号登录" in text or "登录后查看搜索结果" in text
            if logged_in and not login_visible and not (button.count() and button.first.is_visible()):
                with contextlib.suppress(FileNotFoundError):
                    (DATA / "login.png").unlink()
                links = page.locator('a[href*="/user/profile/"]').evaluate_all(
                    "els=>els.filter(e=>e.innerText.trim()==='我').map(e=>e.href.split('?')[0])")
                if len(set(links)) != 1:
                    raise SitePaused('search_account_mismatch')
                profile = links[0]
                if read_json(DATA/'follow-account.json').get('profile_url') == profile:
                    raise SitePaused('search_account_mismatch')
                page.goto(profile, wait_until='domcontentloaded', timeout=45000)
                label = page.locator('.user-info .user-redId')
                label.wait_for(state='visible', timeout=15000)
                check_gate(page)
                match = RED_ID.search(label.inner_text())
                if not match:
                    raise SitePaused('search_account_mismatch')
                write_json(DATA/'search-account.json', {'profile_url': profile, 'red_id': match.group(1),
                    'name': page.locator('.user-info .user-name').inner_text(), 'enrolled_at': now()})
                print("LOGIN_OK", flush=True)
                return 0
            print("LOGIN_WAITING data/login.png", flush=True)
            page.wait_for_timeout(5000)
        print("LOGIN_TIMEOUT", flush=True)
        return 2


def main():
    os.umask(0o077)
    from rednote_watchdesk.paths import HEALTH
    os.environ.setdefault("REDNOTE_WATCHDESK_HEALTH_DIR", str(HEALTH))
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "check-now", "login", "status", "resume", "pause", "follow-login", "follow-resume", "test-webhook", "account-login"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--fresh-login", action="store_true")
    parser.add_argument("--account-id")
    parser.add_argument('--pause-token')
    args = parser.parse_args()
    config = read_json(CONFIG)
    try:
        targets = configured_targets(config)
    except ValueError as error:
        parser.error(str(error))
    if args.target and not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", args.target):
        parser.error("Invalid --target")
    if args.target and not args.dry_run:
        parser.error("--target is only allowed with --dry-run")
    if args.command == "status":
        print(json.dumps({"targets": targets, "interval_seconds": config.get("interval_seconds", 1800),
                          "people": normalize(config)["people"], "state": read_json(STATE)}, ensure_ascii=False, indent=2))
        return 0
    handler = RotatingFileHandler(DATA / "watch.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    if args.command == 'account-login':
        from rednote_watchdesk.accounts import enroll
        enroll(DATA, args.account_id, args.seconds, check_gate, now, fresh=args.fresh_login)
        print('ACCOUNT_LOGIN_OK', flush=True)
        return 0
    if args.command == 'follow-login':
        from rednote_watchdesk.follow import login_account
        account_path = DATA/'follow-account.json'
        previous = {} if args.fresh_login else read_json(account_path)
        small = read_json(DATA/'search-account.json')
        if small.get('profile_url'):
            previous['small_profile_url'] = small['profile_url']
        account = login_account(DATA, previous, args.seconds, check_gate, fresh=args.fresh_login)
        account['enrolled_at'] = now()
        write_json(account_path, account)
        print('BIG_ACCOUNT_LOGIN_OK', flush=True)
        return 0
    if args.command == 'pause':
        pause_control.request_pause(DATA)
        print('PAUSE', flush=True)
        return 0
    with (DATA / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("BUSY", flush=True)
            return 0
        if args.command == "login":
            return login(args.seconds, fresh=args.fresh_login)
        if args.command == "follow-resume":
            state = migrate_state(read_json(STATE), config)
            if state.get('multi_account'):
                from rednote_watchdesk.accounts import require_account
                from rednote_watchdesk.follow_state import resume_account
                require_account(DATA, args.account_id)
                resume_account(state, args.account_id)
                sync_completion(state)
                write_json(STATE, state)
                print('FOLLOW_RESUMED')
                return 0
            state['follow_paused'] = False
            state.pop('follow_pause_reason', None)
            for entry in state['targets'].values():
                job = entry.get('follow', {})
                if job.get('status') in ('failed', 'blocked'):
                    job.update(status='pending', attempts=0, result_notified=False)
                    job.pop('next_attempt_epoch', None)
                    job.pop('failure_notified', None)
            sync_completion(state)
            write_json(STATE, state)
            print('FOLLOW_RESUMED')
            return 0
        if args.command in ("pause", "resume"):
            state = migrate_state(read_json(STATE), config)
            was_paused = state.get("paused", False) or any(e.get("paused") for e in state["targets"].values())
            state["paused"] = args.command == "pause"
            if args.command == "resume":
                state["recovery_pending"] = bool(was_paused)
                state["resumed_at"] = time.time()
                state.pop("pause_reason", None)
                if 'search' in state:
                    state['search'] = {'account_ids': state['search']['account_ids']}
                events = ([state["pending"]] if state.get("pending") else []) + state.get("notification_queue", [])
                events = [e for e in events if e["kind"] != "attention"]
                state.pop("pending", None)
                state["notification_queue"] = []
                for event in events:
                    enqueue(state, event)
                for entry in state["targets"].values():
                    entry["paused"] = False
                    entry["consecutive_errors"] = 0
                    entry.pop("last_attempt_epoch", None)
                    entry.pop("next_retry_epoch", None)
                state["consecutive_errors"] = 0
                state.pop("last_attempt_epoch", None)
            with pause_control.control_lock(DATA):
                pause_control.clear(DATA)
                write_json(STATE, state)
            print(args.command.upper())
            return 0
        if args.command == "test-webhook":
            webhook("【小红书账号监测】通知链路测试成功。", event={"kind":"test","targets":targets})
            print("WEBHOOK_OK")
            return 0
        return run_check(config, args.dry_run, args.target, force=args.command == 'check-now', pause_token=args.pause_token)


if __name__ == "__main__":
    sys.exit(main() or 0)
