"""Account registry, isolated browser profiles and stable enrolled identities."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import uuid

LEGACY_SEARCH = 'legacy_search'
LEGACY_FOLLOW = 'legacy_follow'
ACCOUNT_ID = re.compile(r'[a-z0-9_-]{1,64}')


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    tmp.replace(path)


def account_paths(data, account_id):
    if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
        raise ValueError('账号标识无效')
    data = Path(data)
    if account_id == LEGACY_SEARCH:
        return data/'search-account.json', data/'browser-profile', data/'search-browser.lock'
    if account_id == LEGACY_FOLLOW:
        return data/'follow-account.json', data/'follow-browser-profile', data/'follow-browser.lock'
    home = data/'accounts'/account_id
    return home/'identity.json', home/'browser-profile', home/'browser.lock'


def list_accounts(data):
    registry = read(data/'accounts.json').get('accounts', [])
    result = list(registry)
    config = read(data.parent/'config.json')
    legacy_config = 'search_account_id' not in config and bool(config.get('people') or config.get('targets') or config.get('target'))
    for aid, label in [(LEGACY_SEARCH, '原搜索账号'), (LEGACY_FOLLOW, '原关注账号')]:
        identity, profile, _ = account_paths(data, aid)
        if (legacy_config or identity.exists() or profile.exists()) and not any(a['id'] == aid for a in result):
            result.append({'id': aid, 'label': label})
    return [dict(item, **{k: value for k, value in read(account_paths(data, item['id'])[0]).items()
                         if k in ('name', 'red_id', 'enrolled_at')},
                 ready=bool(read(account_paths(data, item['id'])[0]).get('profile_url'))) for item in result]


def require_account(data, account_id):
    account_paths(data, account_id)
    if not any(a['id'] == account_id for a in list_accounts(data)):
        raise ValueError('账号不存在，请先添加账号')
    return read(account_paths(data, account_id)[0])


def validate_label(label):
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > 40:
        raise ValueError('请填写 1–40 字的账号备注')
    return label.strip()


def create_account(data, label):
    label = validate_label(label)
    with (data/'accounts.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = list_accounts(data)
        if len(current) >= 20:
            raise ValueError('最多保存 20 个账号')
        account = {'id': 'acct_' + uuid.uuid4().hex, 'label': label.strip()}
        # Persist legacy slots too, including slots whose identity is not enrolled yet.
        write(data/'accounts.json', {'accounts': [{'id': a['id'], 'label': a['label']} for a in current] + [account]})
    return account


def rename_account(data, account_id, label, previous_label):
    account_paths(data, account_id)
    label = validate_label(label)
    with (data/'accounts.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = list_accounts(data)
        account = next((a for a in current if a['id'] == account_id), None)
        if account is None:
            raise ValueError('账号不存在，请刷新页面')
        if account['label'] != previous_label:
            raise RuntimeError('备注已被其他页面修改，请取消后刷新页面再试')
        account['label'] = label
        # Persist only display labels. Identity, browser profiles and job IDs stay intact.
        write(data/'accounts.json', {'accounts': [{'id': a['id'], 'label': a['label']} for a in current]})
    return {'id': account_id, 'label': label}


def persist_registry(data):
    with (data/'accounts.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write(data/'accounts.json', {'accounts': [{'id': a['id'], 'label': a['label']} for a in list_accounts(data)]})


def ui_config(config, data):
    from rednote_watchdesk.watch_config import normalize
    plan = normalize(config)
    if 'search_account_id' in plan:
        return plan
    available = {a['id'] for a in list_accounts(data)}
    plan['search_account_id'] = LEGACY_SEARCH if LEGACY_SEARCH in available else None
    for person in plan['people']:
        person['follow_account_ids'] = [LEGACY_FOLLOW] if person['auto_follow'] else []
    return plan


def validate_selection(config, data):
    ids = {a['id'] for a in list_accounts(data)}
    selected = config.get('search_account_id')
    if selected is not None and selected not in ids:
        raise ValueError('所选搜索账号不存在')
    if config['people'] and selected is None:
        raise ValueError('请先选择用于搜索的账号')
    for person in config['people']:
        if any(aid not in ids for aid in person.get('follow_account_ids', [])):
            raise ValueError(person['name'] + '：所选关注账号不存在')


@contextlib.contextmanager
def browser(data, account_id, headed=False):
    from playwright.sync_api import sync_playwright
    require_account(data, account_id)
    _, profile, lock_path = account_paths(data, account_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel='chrome', headless=not headed, chromium_sandbox=True,
                viewport={'width': 1440, 'height': 1000}, locale='zh-CN',
                timezone_id='Asia/Shanghai', accept_downloads=False)
            context.set_default_timeout(10000)
            try:
                yield context.pages[0] if context.pages else context.new_page()
            finally:
                context.close()


def enroll(data, account_id, seconds, gate, now, fresh=False):
    from rednote_watchdesk.follow import login_account, FollowProblem
    previous = require_account(data, account_id)
    # An ID always identifies the same actor. Re-login must never reassign old jobs.
    previous.pop('small_profile_url', None)
    identity = login_account(data, previous, seconds, gate, fresh=fresh, account_id=account_id)
    with (data/'accounts.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for item in list_accounts(data):
            other = read(account_paths(data, item['id'])[0])
            if item['id'] != account_id and other.get('profile_url') == identity['profile_url']:
                raise FollowProblem('account_already_enrolled', blocked=True)
        identity['enrolled_at'] = now()
        write(account_paths(data, account_id)[0], identity)
    return identity
