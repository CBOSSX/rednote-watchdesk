"""Stable query error codes and summaries shared by the UI and notifications."""
import re
from urllib.parse import urlsplit, urlunsplit

STAGES = {
    'launch_browser': '启动浏览器',
    'navigate_home': '打开小红书首页',
    'homepage_access': '读取首页正文',
    'read_page_body': '读取页面正文',
    'verify_identity': '校验搜索账号身份',
    'navigate_search': '打开搜索页面',
    'check_access': '读取搜索页面正文',
    'select_users': '切换用户页签',
    'wait_user_results': '等待用户结果',
}
ACCESS_REASONS = {
    'login_required': '搜索账号登录已失效',
    'search_account_not_ready': '搜索账号尚未完成登录',
    'search_account_mismatch': '当前登录身份与所选搜索账号不一致',
    'search_accounts_unavailable': '所有已配置的搜索账号均不可用',
    'ip_risk': '当前网络被平台判定存在风险',
    'risk_control': '平台要求安全验证或触发风控',
    'http_403': '平台拒绝访问',
    'http_429': '平台限制访问频率',
}


def safe_url(value):
    try:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.hostname or '', parts.path, '', ''))
    except (ValueError, TypeError):
        return ''


def safe_detail(value):
    # Browser messages may include query tokens or local profile paths.
    value = re.sub(r'(?:https?|file)://[^\s<>\"\']+', lambda m: safe_url(m[0]), str(value))
    value = re.sub(r'/(?:Users|home)/[^\s\"\']+', '[本地路径]', value)
    return value[:1500]


def describe(detail):
    stage = detail.get('stage', 'launch_browser')
    kind = detail.get('type', '')
    text = detail.get('detail', '')
    if stage == 'launch_browser' and 'Locator.inner_text' in text and 'body' in text:
        # Older records lost the page reference during context initialization.
        stage = 'read_page_body'
    label = STAGES.get(stage, '执行查询')
    if kind == 'SitePaused':
        code, message = text, ACCESS_REASONS.get(text, '平台访问或账号状态需要处理')
    elif 'Timeout' in kind:
        code, message = stage + '_timeout', label + '超时'
    elif kind == 'BlockingIOError':
        code, message = 'browser_busy', '此账号的浏览器正被其他任务使用'
    elif kind == 'TargetClosedError':
        code, message = 'browser_closed', '浏览器或页面意外关闭'
    elif re.search(r'net::ERR_[A-Z_]+', text):
        code, message = 'network_error', '网络连接失败（' + re.search(r'net::ERR_[A-Z_]+', text)[0] + '）'
    elif text.startswith('http_'):
        code, message = text.splitlines()[0], '平台返回 HTTP ' + text.splitlines()[0][5:]
    elif text == 'unexpected_redirect':
        code, message = 'unexpected_redirect', '页面跳转到了非预期地址'
    elif stage == 'wait_user_results':
        code, message = 'user_results_unavailable', '未获得用户列表或明确的空结果'
    else:
        code, message = 'query_error', label + '失败（' + (kind or '未知错误') + '）'
    return {'code': code, 'message': message, 'stage': stage, 'stage_label': label}


def public_error(detail):
    """Keep raw browser messages and page content out of public payloads."""
    return dict(describe(detail), **{k: detail[k] for k in
                ('checked_at', 'http_status', 'body_attempts', 'elapsed_seconds') if k in detail})
