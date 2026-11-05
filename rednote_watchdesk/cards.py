"""Shared Card 2.0 layout for account notifications and watchdog events."""
import datetime as dt
from html import escape
import re
from rednote_watchdesk.mentions import mention_tags

STYLES = {
    'found': ('发现账号', 'green', '小红书号已精确匹配', '请确认本人及店铺归属后关注；此账号通知成功后结束监测，其他账号继续。'),
    'attention': ('监测已暂停', 'orange', '需要你处理后恢复', '请在本机检查登录或完成验证，处理后恢复监测。'),
    'query_fault': ('查询正在重试', 'orange', '连续查询失败，正在自动重试', '脚本会继续重试，最长每半小时尝试一次；成功后再通知你。'),
    'query_recovery': ('查询已恢复', 'green', '查询已验证成功', '已恢复正常查询间隔，约每 30–35 分钟检查一次。'),
    'recovery': ('监测已恢复', 'green', '监测已恢复运行', '继续监听目标账号，约每 30–35 分钟检查一次。'),
    'fault': ('运行异常', 'red', '监测运行出现异常', '请检查本机运行情况；看门狗会继续检查，恢复后通知。'),
    'follow_success': ('自动关注成功', 'green', '执行账号已关注该账号', '已刷新主页确认关注状态，其他目标继续按配置监听。'),
    'follow_failure': ('自动关注未成功', 'orange', '执行账号自动关注未成功', '请打开主页确认，必要时手动关注；搜索账号继续监听其他未发现的目标。'),
    'follow_ready': ('自动关注已就绪', 'green', '执行账号登录已验证，自动关注已就绪', '搜索账号发现目标后，执行账号将核对账号并关注；成功或失败都会通过卡片提醒。'),
    'test': ('通知测试', 'blue', '卡片通知已启用', '这是一条测试消息，不代表发现账号或发生故障。'),
}
REASONS = {'login_required': '搜索账号登录已失效', 'ip_risk': '当前网络被平台判定存在风险',
           'risk_control': '平台要求安全验证或触发风控', 'http_403': '平台拒绝访问',
           'http_429': '平台限制访问频率', 'TimeoutError': '页面加载或查询超时',
           'TargetClosedError': '浏览器意外关闭',
           'search_account_not_ready': '搜索账号尚未完成登录',
           'search_account_mismatch': '当前登录身份与所选搜索账号不一致',
           'search_accounts_unavailable': '所有已配置的搜索账号均不可用',
           'follow_account_not_ready': '执行账号尚未完成登录配置',
           'follow_account_mismatch': '当前登录账号与配置的执行账号不一致',
           'follow_target_mismatch': '主页上的小红书号或主页地址与目标不一致',
           'follow_button_unavailable': '暂时无法识别主页上的关注按钮',
           'follow_not_confirmed': '未能确认关注成功，需要人工检查',
           'follow_not_allowlisted': '目标不在已授权的自动关注名单中',
           'follow_invalid_profile': '目标主页地址无法通过校验',
           'follow_self_refused': '目标是执行账号本人，已停止操作'}


def safe_text(value):
    # Profile text must not introduce links, formatting, or @ tags.
    return ''.join('&#' + str(ord(c)) + ';' if c in '*~[]()#:_`\\' else escape(c)
                   for c in str(value))


def markdown(content, **extra):
    return {'tag': 'markdown', 'content': content, 'margin': '0px', **extra}


def local_time(value):
    stamp = (dt.datetime.fromtimestamp(value, dt.timezone.utc) if isinstance(value, (int, float))
             else dt.datetime.fromisoformat(value))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')


def notification_payload(text, secret, event=None, source='monitor'):
    event = event or {}
    kind = event.get('kind', 'test')
    title, color, headline, action = STYLES.get(kind, ('状态更新', 'blue', '监测状态已更新', ''))
    if kind in ('query_recovery', 'recovery') and source != 'watchdog':
        interval = event.get('interval_seconds', 1800) / 60
        action = '已恢复正常轮询，配置间隔为 ' + format(interval, 'g') + ' 分钟。'
    if kind == 'found' and event.get('auto_follow_enabled'):
        action = '已交由选中的账号分别自动关注，成功或失败将另发结果卡片；你也可以先打开主页确认本人及店铺归属。'
    if kind == 'follow_success' and event.get('already_followed'):
        action = '执行账号此前已关注，本次未重复点击关注按钮。'
    if kind == 'follow_failure' and event.get('follow_status') == 'retry':
        action = ('后续将自动核对关注状态，不再重复点击。' if event.get('click_started') else
                  '稍后自动重试，最多尝试 3 次；成功后通知。')
    if kind == 'follow_failure' and event.get('follow_status') == 'blocked':
        action = '此账号自动关注已暂停，请检查或重新登录后恢复；其他账号的任务可继续。'
    if source == 'watchdog' and kind == 'recovery':
        title, headline = '运行已恢复', '主监测调度及查询尝试已恢复'
        action = '看门狗将继续检查运行情况；查询结果以账号查询通知为准。'
    targets = [event['target']] if event.get('target') else event.get('targets', [])
    if not targets:
        targets = []
    info = [markdown("<font color='grey'>监听账号</font>\n" +
                     ('\n'.join(safe_text(t) for t in targets) if targets else '当前配置的目标账号'))]
    if event.get('person_name'):
        info.insert(0, markdown("<font color='grey'>监控目标</font>\n" + safe_text(event['person_name'])))
    result = event.get('result') or {}
    url = result.get('url', '')
    if kind in ('found', 'follow_success', 'follow_failure'):
        name_match = re.search(r'昵称：[ \t]*(.*)', text)
        name = result.get('name') or (name_match.group(1) if name_match else '')
        if name:
            info.append(markdown("<font color='grey'>账号昵称</font>\n" + safe_text(name)))
        if not url:
            match = re.search(r'https://www\.xiaohongshu\.com/user/profile/[a-fA-F0-9]{24}', text)
            url = match.group(0) if match else ''
    reason = event.get('reason')
    if reason:
        info.append(markdown("<font color='grey'>异常原因</font>\n" + safe_text(REASONS.get(reason, reason))))
    elif kind not in ('found', 'query_recovery', 'recovery', 'follow_success', 'follow_ready'):
        detail = re.sub(r'^【[^】]+】\s*', '', text).strip()
        info.append(markdown("<font color='grey'>说明</font>\n" + safe_text(detail)))
    if source == 'watchdog' and kind == 'recovery':
        previous = event.get('previous_reason')
        if not previous:
            match = re.search(r'此前异常：(.*)', text, re.S)
            previous = match.group(1) if match else ''
        if previous:
            info.append(markdown("<font color='grey'>此前异常</font>\n" + safe_text(previous)))
    if event.get('actor_name'):
        info.append(markdown("<font color='grey'>关注使用的执行账号</font>\n" + safe_text(event['actor_name'])))
    elements = [markdown('**' + headline + '**\n' + action),
                {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
                    {'tag': 'column', 'width': 'weighted', 'weight': 1,
                     'background_style': color + '-50', 'padding': '12px',
                     'vertical_spacing': '8px', 'elements': info}]}]
    if kind in ('found', 'follow_success', 'follow_failure') and re.fullmatch(r'https://www\.xiaohongshu\.com/user/profile/[a-fA-F0-9]{24}', url):
        elements.append({'tag': 'button', 'type': 'primary_filled', 'width': 'fill',
                         'text': {'tag': 'plain_text', 'content': '打开小红书主页'},
                         'behaviors': [{'type': 'open_url', 'default_url': url}]})
    mentions = mention_tags(secret, card=True)
    stamp = event.get('created_at')
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    footer = (mentions + '\n' if mentions else '') + "<font color='grey'>" + (
        '发生时间：' + local_time(stamp) if stamp else '发送时间：' + local_time(now)) + '（北京时间）</font>'
    elements.append(markdown(footer, text_size='notation'))
    return {'msg_type': 'interactive', 'card': {
        'schema': '2.0',
        'config': {'update_multi': True, 'width_mode': 'default',
                   'summary': {'content': '小红书监测 · ' + title + (' · ' + '、'.join(targets) if targets else '')}},
        'header': {'title': {'tag': 'plain_text', 'content': '小红书监测 · ' + title},
                   'subtitle': {'tag': 'plain_text', 'content': '运行守护' if source == 'watchdog' else '账号查询'},
                   'template': color},
        'body': {'direction': 'vertical', 'padding': '12px', 'vertical_spacing': '12px', 'elements': elements}}}
