#!/usr/bin/python3
"""Independent stdlib-only scheduler watchdog, compatible with system Python."""
import argparse
import base64
import ctypes
import datetime
import fcntl
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request
from cards import notification_payload
from paths import RUNTIME, MONITOR_LABEL

ROOT = Path(os.environ.get('REDNOTE_WATCHDOG_ROOT', str(RUNTIME/'health')))
SECRETS = RUNTIME/'secrets.json'
DATA = ROOT / 'data'
FILE = DATA / 'watchdog-state.json'
LABEL = MONITOR_LABEL
LOG = logging.getLogger('watchdog')


def awake_clock():
    import sys
    if sys.platform != "darwin":
        return time.monotonic()
    # Python 3.9's monotonic clock on macOS has a process-relative origin.
    # Call Mach directly so values remain comparable across launchd invocations.
    class Timebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    info = Timebase()
    lib.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
    if lib.mach_timebase_info(ctypes.byref(info)) != 0:
        raise RuntimeError('awake_clock_unavailable')
    lib.mach_absolute_time.restype = ctypes.c_uint64
    return lib.mach_absolute_time() * info.numer / info.denom / 1e9


def read(path):
    return json.loads(path.read_text())


def save(state):
    tmp = FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(FILE)


def clock_grace(state, wall, awake):
    pw, pa = state.get('clock_wall'), state.get('clock_awake')
    if pw is None or pa is None or awake < pa or (wall-pw)-(awake-pa) > 60 or wall < pw:
        state['grace_until'] = wall + 600
    state.update(clock_wall=wall, clock_awake=awake)
    return wall < state.get('grace_until', 0)


def diagnose(wall, monitor, heartbeat, loaded):
    if monitor.get('paused') or monitor.get('completed') or (monitor.get('query_complete') and not monitor.get('follow_pending')):
        return None
    if not loaded:
        return '主监测的系统调度任务未加载'
    if not heartbeat:
        return '主调度器没有执行记录'
    if heartbeat.get('status') == 'fatal':
        return '主程序启动失败或超时：' + heartbeat.get('reason', 'unknown')
    limit = heartbeat.get('timeout_seconds', 240) + 60
    if heartbeat.get('status') in ('checking', 'starting') and wall-heartbeat.get('started_at', wall) > limit:
        return '主程序运行超过' + str(limit//60) + '分钟，可能卡住'
    if heartbeat.get('worker_fault'):
        return '主程序故障尚未通过后续执行验证恢复：'+heartbeat['worker_fault']
    if wall-heartbeat.get('updated_at', 0) > 900:
        return '主调度器超过15分钟没有运行记录'
    if wall-max(monitor.get('last_attempt_epoch', 0), monitor.get('resumed_at', 0)) > 2700:
        return '超过45分钟没有新的查询尝试'
    return None


def timestamp(wall):
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.fromtimestamp(wall, tz).strftime('%m-%d %H:%M:%S')


def transition(state, reason, wall, targets=None):
    names = "、".join(targets or ["当前配置的目标"])
    queue = state.setdefault('queue', [])
    if reason and not state.get('incident'):
        state['incident'] = {'started_at': wall, 'reason': reason}
        queue.append({'kind': 'fault', 'created_at': wall, 'targets': targets or [], 'reason': reason, 'text': '【小红书监测·看门狗异常】'+timestamp(wall)+'\n'+reason+'\n目标：'+names+'。请检查本机；看门狗不会擅自恢复已暂停的查询。'})
        LOG.warning('incident_started reason=%s', reason)
    elif not reason and state.get('incident'):
        incident = state.pop('incident')
        queue.append({'kind': 'recovery', 'created_at': wall, 'targets': targets or [], 'previous_reason': incident['reason'], 'text': '【小红书监测·看门狗恢复】'+timestamp(wall)+'\n主监测调度及查询尝试已恢复正常。\n此前异常：'+incident['reason']})
        LOG.info('incident_recovered')
    elif reason:
        state['incident']['latest_reason'] = reason


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('webhook_redirect_refused')


def notify(text, *, event=None):
    secret = read(SECRETS)
    url = secret['webhook_url']
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ('open.larkoffice.com', 'open.feishu.cn', 'open.larksuite.com') or not parts.path.startswith('/open-apis/bot/v2/hook/'):
        raise RuntimeError('invalid_webhook_destination')
    payload = notification_payload(text, secret, event, source='watchdog')
    if secret.get('signing_secret'):
        stamp = str(int(time.time()))
        key = (stamp+'\n'+secret['signing_secret']).encode()
        payload.update(timestamp=stamp, sign=base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            reply = json.load(response)
    except Exception as error:
        raise RuntimeError('webhook_transport_'+type(error).__name__) from None
    code = reply.get('code', reply.get('StatusCode'))
    if type(code) is not int or code != 0:
        raise RuntimeError('webhook_rejected_'+str(code))


def deliver(state, sender=notify, persist=save):
    for _ in range(2):
        if not state.get('queue'):
            break
        try:
            sender(state['queue'][0]['text'], event=state['queue'][0])
        except Exception as error:
            state['delivery_error'] = str(error)
            persist(state)
            LOG.warning('delivery_failed reason=%s', error)
            return
        event = state['queue'].pop(0)
        state.pop('delivery_error', None)
        state['last_delivered_at'] = time.time()
        persist(state)
        LOG.info('delivered kind=%s', event['kind'])


def main():
    os.umask(0o077)
    DATA.mkdir(exist_ok=True, mode=0o700)
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    handler = RotatingFileHandler(DATA / 'watchdog.log', maxBytes=500000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    with (DATA / 'watchdog.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if args.self_test:
            notify('【小红书监测·看门狗自检】模拟异常通知测试。主监测未受影响，这不是实际故障或账号开通提醒。')
            notify('【小红书监测·看门狗自检恢复】恢复通知链路测试成功。主监测继续按原配置运行。')
            print('WATCHDOG_NOTIFICATION_SELF_TEST_OK')
            return
        try:
            state = read(FILE) if FILE.exists() else {}
        except (ValueError, OSError):
            state = {'state_recreated': True}
        wall = time.time()
        grace = clock_grace(state, wall, awake_clock())
        reason = None
        monitor = {}
        try:
            monitor = read(DATA / 'state.json')
            intentional = bool(monitor.get('paused') or monitor.get('completed') or (monitor.get('query_complete') and not monitor.get('follow_pending')))
            if not intentional:
                r = subprocess.run(['/bin/launchctl', 'print', 'gui/'+str(os.getuid())+'/'+LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                try:
                    heartbeat = read(DATA / 'heartbeat.json')
                except (ValueError, OSError):
                    heartbeat = {}
                reason = diagnose(wall, monitor, heartbeat, r.returncode == 0)
        except Exception as error:
            intentional = False
            reason = '监测状态或系统调度无法检查：'+type(error).__name__
        if not grace and not intentional:
            transition(state, reason, wall, monitor.get("configured_targets"))
        state.update(last_checked_at=wall, current_reason=reason, in_grace=grace)
        save(state)
        # Keep queued real events ordered even if the monitor is later paused.
        deliver(state)


if __name__ == '__main__':
    main()
