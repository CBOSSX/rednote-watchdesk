"""Validated person/candidate configuration, shared by the worker and scheduler."""
import hashlib
import json
import re

RED_ID = re.compile(r'[A-Za-z0-9_-]{3,40}')
PERSON_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')

def normalize(config):
    if not isinstance(config, dict):
        raise ValueError('配置必须是 JSON 对象')
    interval = config.get('interval_seconds', 1800)
    if type(interval) is not int or not 0 <= interval <= 86400:
        raise ValueError('轮询间隔必须为 0–86400 秒的整数')
    if 'people' in config:
        people = config['people']
        if not isinstance(people, list) or len(people) > 10:
            raise ValueError('监控目标列表最多支持 10 组')
    else:
        candidates = config.get('targets') or ([config['target']] if config.get('target') else [])
        if not isinstance(candidates, list):
            raise ValueError('targets 必须是列表')
        enabled = config.get('auto_follow_enabled', False)
        allowed = config.get('auto_follow_targets', []) if enabled else []
        if type(enabled) is not bool or not isinstance(allowed, list) or any(t not in candidates for t in allowed):
            raise ValueError('自动关注名单必须是监听目标的子集')
        people = [{'id': 'account_'+str(t).lower(), 'name': t, 'candidates': [t], 'auto_follow': t in allowed} for t in candidates]
    multi = 'search_account_id' in config or any(isinstance(p, dict) and 'follow_account_ids' in p for p in people)
    search_account = config.get('search_account_id')
    if multi and search_account is not None and (not isinstance(search_account, str) or not PERSON_ID.fullmatch(search_account)):
        raise ValueError('搜索账号标识无效')
    result, ids, owners = [], set(), {}
    for person in people:
        if not isinstance(person, dict):
            raise ValueError('每个目标配置必须是对象')
        pid, name = person.get('id'), person.get('name')
        candidates, follow = person.get('candidates'), person.get('auto_follow', False)
        if not isinstance(pid, str) or not PERSON_ID.fullmatch(pid) or pid in ids:
            raise ValueError('目标 id 必须唯一，只能包含字母、数字、下划线或连字符')
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('请填写 1–80 字的目标名称')
        if not isinstance(candidates, list) or not candidates or len(candidates) > 10:
            raise ValueError(name+'：请填写 1–10 个候选小红书号')
        if type(follow) is not bool:
            raise ValueError(name+'：自动关注必须为 true 或 false')
        followers = person.get('follow_account_ids', [])
        if multi:
            if not isinstance(followers, list) or len(followers) > 20 or any(not isinstance(a, str) or not PERSON_ID.fullmatch(a) for a in followers) or len(set(followers)) != len(followers):
                raise ValueError(name+'：关注账号列表必须唯一且有效')
            if 'follow_account_ids' not in person and follow:
                followers = ['legacy_follow']
            follow = bool(followers)
        for candidate in candidates:
            if not isinstance(candidate, str) or not RED_ID.fullmatch(candidate):
                raise ValueError(name+'：小红书号只支持 3–40 位字母、数字、下划线或连字符')
            key = candidate.casefold()
            if key in owners:
                raise ValueError('候选小红书号重复：'+candidate+'（'+owners[key]+' / '+name+'）；大小写不同视为同一个号')
            owners[key] = name
        ids.add(pid)
        result.append({'id': pid, 'name': name.strip(), 'candidates': list(candidates), 'auto_follow': follow})
        if multi:
            result[-1]['follow_account_ids'] = list(followers)
    if len(owners) > 10:
        raise ValueError('全部分组合计最多支持 10 个候选账号')
    plan = {'interval_seconds': interval, 'people': result}
    if multi:
        plan['search_account_id'] = search_account
    return plan

def targets(config):
    return [t for p in normalize(config)['people'] for t in p['candidates']]

def signature(config):
    return hashlib.sha256(json.dumps(normalize(config), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def needs_reconcile(config, state):
    # Old, unversioned fixtures/configs keep their legacy scheduling behavior.
    return ('people' in config or 'config_signature' in state) and signature(config) != state.get('config_signature')
