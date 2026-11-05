"""Loopback-only configuration UI; private state is never served as static files."""
import argparse
from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse

from rednote_watchdesk.paths import RUNTIME, PROJECT, MONITOR_LABEL
from rednote_watchdesk.watch_config import SEARCH_ACCOUNT_FIELDS, normalize, signature
from rednote_watchdesk import accounts
from rednote_watchdesk.query_diagnostics import public_error
from rednote_watchdesk import pause_control, query_progress

TOKEN = secrets.token_urlsafe(32)
WAKE = threading.Event()
JOBS = {}
JOB_LOCK = threading.Lock()
SCHEDULER = False


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {} if default is None else default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name+'.tmp.'+str(os.getpid())+'.'+secrets.token_hex(3))
    fd = os.open(tmp, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2)
        f.write('\n')
    tmp.replace(path)


def ensure_home():
    RUNTIME.mkdir(parents=True,exist_ok=True,mode=0o700)
    (RUNTIME/'data').mkdir(exist_ok=True,mode=0o700)
    if not (RUNTIME/'config.json').exists():
        write(RUNTIME/'config.json',{'interval_seconds':1800,'people':[]})
    if not (RUNTIME/'data/state.json').exists():
        write(RUNTIME/'data/state.json',{})


def wake():
    if SCHEDULER:
        WAKE.set()
    elif sys.platform=='darwin':
        subprocess.run(['/bin/launchctl','kickstart','gui/'+str(os.getuid())+'/'+MONITOR_LABEL],capture_output=True,timeout=10)


def payload():
    cfg = read(RUNTIME/'config.json')
    state = pause_control.apply(RUNTIME/'data', read(RUNTIME/'data/state.json'))
    heartbeat = read(RUNTIME/'data/heartbeat.json')
    notice = read(RUNTIME/'secrets.json')
    plan = accounts.ui_config(cfg, RUNTIME/'data')
    search = state.get('search', {})
    if search.get('account_ids') != plan['search_account_ids']:
        search = {}
    people = []
    for person in plan['people']:
        saved = state.get('people',{}).get(person['id'],{})
        matched = saved.get('matched_target') or next((t for t in person['candidates'] if state.get('targets',{}).get(t,{}).get('found')),None)
        entry = state.get('targets',{}).get(matched,{}) if matched else {}
        checks = [state.get('targets',{}).get(t,{}) for t in person['candidates']]
        query_errors = []
        if not matched:
            for target, check in zip(person['candidates'], checks):
                if check.get('last_error'):
                    detail = check.get('last_error_detail') or {'type': check['last_error']}
                    query_errors.append(dict(public_error(detail), target=target,
                                             consecutive_errors=check.get('consecutive_errors', 0),
                                             next_retry_epoch=check.get('next_retry_epoch')))
        jobs = []
        for aid in person['follow_account_ids']:
            job = entry.get('follows', {}).get(aid, entry.get('follow', {}) if aid == accounts.LEGACY_FOLLOW else {})
            jobs.append({'account_id': aid, 'status': job.get('status', 'pending' if matched else 'waiting_match'), 'reason': job.get('reason')})
        status = next((status for status in ['failed','blocked','retry','running','pending','succeeded'] if any(j['status'] == status for j in jobs)), None)
        people.append({'id':person['id'],'matched_target':matched,'completed':saved.get('completed',entry.get('completed',False)),
                       'result':entry.get('found'),'follow_status':status,'follow_jobs':jobs,'query_errors':query_errors,
                       'last_checked_at':max((e.get('last_checked_at','') for e in checks),default='')})
    account_list = accounts.list_accounts(RUNTIME/'data')
    paused_accounts = dict(state.get('follow_paused_accounts', {}))
    if state.get('follow_paused') and not state.get('multi_account'):
        paused_accounts[accounts.LEGACY_FOLLOW] = state.get('follow_pause_reason', 'login_required')
    for account in account_list:
        account['follow_paused'] = account['id'] in paused_accounts
        account['follow_needs_resume'] = account['follow_paused'] or any(
            job['account_id'] == account['id'] and job['status'] in ('failed', 'blocked')
            for person in people for job in person['follow_jobs'])
    with JOB_LOCK:
        jobs = [dict(j) for j in JOBS.values()][-8:]
    return {'config':plan,'version':signature(cfg),'pending_apply':state.get('config_signature')!=signature(cfg),
            'state':{'paused':state.get('paused',False),'completed':state.get('completed',False),
                     'follow_paused':state.get('follow_paused',False),'people':people,
                     'heartbeat_at':heartbeat.get('updated_at'),'worker_fault':heartbeat.get('worker_fault'),
                     'pending_notifications':bool(state.get('pending')),
                     'query_progress':query_progress.read(RUNTIME/'data'),
                     'query_timings':query_progress.timings(RUNTIME/'data'),
                     'active_search_account_id':search.get('active_account_id'),
                     'search_account_errors':search.get('errors', {}),
                     'search_account_error_details':{aid:public_error(detail) for aid, detail in search.get('error_details', {}).items()}},
            'accounts':account_list,'notifications':{'configured':bool(notice.get('webhook_url')),
                'signing_configured':bool(notice.get('signing_secret')),'mention_users':notice.get('mention_users',[])},'jobs':jobs}


@contextmanager
def config_lock():
    with (RUNTIME/'data/config.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('正在执行自动关注，请待本轮结束后再保存配置')
        yield


def save_config(body):
    with config_lock():
        current = read(RUNTIME/'config.json')
        if body.get('version') != signature(current):
            raise RuntimeError('配置已被其他页面修改，请刷新后重试')
        submitted = body.get('config')
        plan = normalize(submitted)
        if (SEARCH_ACCOUNT_FIELDS & current.keys() and not SEARCH_ACCOUNT_FIELDS & submitted.keys()
                or 'search_account_ids' in current and 'search_account_ids' not in submitted):
            raise RuntimeError('账号配置已升级，请刷新页面后重试')
        if 'search_account_ids' in plan:
            accounts.validate_selection(plan, RUNTIME/'data')
        if plan['interval_seconds'] < 60:
            raise ValueError('轮询间隔至少为 1 分钟')
        # Validate migration against an isolated copy; reject removal of a matched candidate.
        from rednote_watchdesk.watch import migrate_state
        migrate_state(read(RUNTIME/'data/state.json'),plan)
        if 'search_account_ids' in plan:
            accounts.persist_registry(RUNTIME/'data')
        write(RUNTIME/'config.json',plan)
    wake()


@contextmanager
def notification_lock():
    with (RUNTIME/'data/notifications.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_notifications(body):
    url = body.get('webhook_url', '')
    signing = body.get('signing_secret', '')
    if not isinstance(url, str) or not isinstance(signing, str):
        raise ValueError('Webhook 和签名密钥必须是文本')
    url = url.strip()
    if url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme!='https' or parts.hostname not in ('open.larkoffice.com','open.feishu.cn','open.larksuite.com') or not parts.path.startswith('/open-apis/bot/v2/hook/') or parts.username or parts.password:
            raise ValueError('请填写飞书/Lark 官方自定义机器人 HTTPS webhook')
    from rednote_watchdesk.mentions import mention_tags
    with notification_lock():
        current = read(RUNTIME/'secrets.json')
        users = body.get('mention_users', current.get('mention_users', []))
        mention_tags({'mention_users':users}, card=True)
        if url:
            current['webhook_url'] = url
        if signing:
            current['signing_secret'] = signing
        current['mention_users'] = users
        write(RUNTIME/'secrets.json', current)
    wake()


def clear_notifications(body):
    fields = body.get('fields')
    allowed = {'webhook_url', 'signing_secret', 'mention_users'}
    if not isinstance(fields, list) or not fields or any(not isinstance(f, str) or f not in allowed for f in fields):
        raise ValueError('请选择要清除的通知配置')
    with notification_lock():
        current = read(RUNTIME/'secrets.json')
        for field in fields:
            current.pop(field, None)
        write(RUNTIME/'secrets.json', current)
    wake()


def start_job(command, *, fresh=False, title='操作', account_id=None, scope='people'):
    with JOB_LOCK:
        if any(j['status']=='running' for j in JOBS.values()):
            raise RuntimeError('已有操作正在进行，请完成后再试')
        jid = secrets.token_hex(8)
        JOBS[jid] = {'id':jid,'title':title,'scope':scope,'account_id':account_id,'status':'running','started_at':time.time()}
        pause_token = pause_control.token(RUNTIME/'data')
        for old_id in list(JOBS)[:-30]:
            if JOBS[old_id]['status'] != 'running':
                del JOBS[old_id]
    def run():
        argv=([sys.executable,'-m','rednote_watchdesk.tick','--now'] if command == 'check-now' else
              [sys.executable,'-m','rednote_watchdesk.watch',command])
        if command == 'check-now':
            argv += ['--pause-token', pause_token]
        if account_id is not None:
            argv += ['--account-id',account_id]
        if command in ('login','follow-login','account-login'):
            argv += ['--seconds','600']
            if fresh:argv += ['--fresh-login']
        log=RUNTIME/'data'/('ui-job-'+jid+'.log')
        try:
            with log.open('w') as out:
                log.chmod(0o600)
                # tick owns the timeout and terminates its browser worker process group.
                proc=subprocess.run(argv,stdout=out,stderr=subprocess.STDOUT,
                                    timeout=None if command == 'check-now' else 620,cwd=PROJECT,
                                    env=dict(os.environ, REDNOTE_WATCHDESK_JOB_ID=jid) if command == 'check-now' else None)
            output = log.read_text().strip()
            busy = output=='BUSY'
            success=proc.returncode==0 and not busy
            failure = '操作未完成；请确认登录的是此账号。同一账号无需重复添加，更换身份请新增账号' if command == 'account-login' else '操作未完成，请检查配置和本机日志后重试'
            message='操作完成' if success else '当前任务正忙，请稍后重试' if busy else failure
            if command == 'check-now' and not busy:
                try:
                    summary = json.loads(output.splitlines()[-1])['manual_check']
                    message = summary.get('message') or ('本轮完成：查询成功 ' + str(summary['successful']) +
                        ' 个，失败 ' + str(summary['failed']) + ' 个，发现 ' + str(summary['found']) + ' 个')
                except (ValueError, KeyError, IndexError, TypeError):
                    message = '本轮调度未完成，请检查后台运行状态和本机日志'
                    success = False
        except Exception:
            success,message=False,'操作未完成，请稍后重试'
        with JOB_LOCK:
            JOBS[jid].update(status='succeeded' if success else 'failed',message=message,finished_at=time.time())
        if command != 'check-now':
            wake()
    threading.Thread(target=run,daemon=True).start()
    return jid


class Handler(BaseHTTPRequestHandler):
    server_version='RednoteWatchdesk'
    def log_message(self,*args):pass
    def valid_host(self):
        return self.headers.get('Host') in ('127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port))
    def respond(self,code,value,mime='application/json; charset=utf-8'):
        data = value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if not self.valid_host():return self.respond(403,{'error':'仅支持本机访问'})
        try:
            if self.path=='/api/state':return self.respond(200,payload())
            assets={'/':('index.html','text/html; charset=utf-8'),'/favicon.svg':('favicon.svg','image/svg+xml'),'/js/app.js':('js/app.js','application/javascript; charset=utf-8'),'/js/motion.js':('js/motion.js','application/javascript; charset=utf-8'),'/js/accounts.js':('js/accounts.js','application/javascript; charset=utf-8'),'/css/style.css':('css/style.css','text/css; charset=utf-8'),'/css/motion.css':('css/motion.css','text/css; charset=utf-8')}
            if self.path not in assets:return self.respond(404,{'error':'未找到'})
            name,mime=assets[self.path];text=(PROJECT/'web'/name).read_text()
            if name=='index.html':text=text.replace('__CSRF_TOKEN__',TOKEN)
            self.respond(200,text,mime)
        except Exception:
            self.respond(500,{'error':'读取配置失败，请检查本机日志'})
    def do_POST(self):
        origin=self.headers.get('Origin')
        origins={'http://127.0.0.1:'+str(self.server.server_port),'http://localhost:'+str(self.server.server_port)}
        if not self.valid_host() or origin not in origins or not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),TOKEN):
            return self.respond(403,{'error':'请求来源或页面会话无效，请刷新页面'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 65536 or self.headers.get_content_type()!='application/json':
                raise ValueError('请求格式不正确')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('请求必须是对象')
            if self.path=='/api/config':save_config(body);return self.respond(200,{'ok':True})
            if self.path=='/api/notifications':save_notifications(body);return self.respond(200,{'ok':True})
            if self.path=='/api/notifications/clear':clear_notifications(body);return self.respond(200,{'ok':True})
            if self.path=='/api/accounts':
                account=accounts.create_account(RUNTIME/'data', body.get('label'))
                return self.respond(201,{'ok':True,'account':account})
            if self.path=='/api/accounts/rename':
                account=accounts.rename_account(RUNTIME/'data', body.get('account_id'), body.get('label'), body.get('previous_label'))
                return self.respond(200,{'ok':True,'account':account})
            if self.path=='/api/login':
                aid=body.get('account_id')
                accounts.require_account(RUNTIME/'data', aid)
                jid=start_job('account-login',fresh=body.get('fresh') is True,title='账号登录 / 校验',account_id=aid,scope='accounts')
                return self.respond(202,{'ok':True,'job_id':jid})
            if self.path=='/api/control':
                action=body.get('action')
                if action not in ('pause','resume','follow-resume','test-webhook','check-now'):raise ValueError('操作无效')
                if action == 'pause':
                    pause_control.request_pause(RUNTIME/'data')
                    wake()
                    return self.respond(200,{'ok':True,'message':'已暂停后续调度，正在执行的步骤将安全收尾'})
                if action == 'check-now':
                    plan = accounts.ui_config(read(RUNTIME/'config.json'), RUNTIME/'data')
                    if not plan['people']:raise ValueError('请先保存监控目标')
                    if not plan['search_account_ids']:raise ValueError('请先选择搜索账号并保存配置')
                aid=body.get('account_id') if action=='follow-resume' else None
                if action=='follow-resume':
                    accounts.require_account(RUNTIME/'data', aid)
                scope='settings' if action=='test-webhook' else 'accounts' if action=='follow-resume' else 'people'
                return self.respond(202,{'ok':True,'job_id':start_job(action,account_id=aid,scope=scope,title={'pause':'暂停监测','resume':'恢复监测','follow-resume':'恢复此账号的关注任务','test-webhook':'测试飞书通知','check-now':'立即查询一次'}[action])})
            self.respond(404,{'error':'未找到'})
        except RuntimeError as error:self.respond(409,{'error':str(error)})
        except (ValueError,TypeError,KeyError) as error:self.respond(400,{'error':str(error)})
        except Exception:self.respond(500,{'error':'操作失败，请检查本机日志'})


def scheduler():
    while True:
        try:
            # The tick process bounds and terminates its own worker, including failover.
            subprocess.run([sys.executable,'-m','rednote_watchdesk.tick'],cwd=PROJECT)
        except Exception:
            pass
        WAKE.wait(60);WAKE.clear()


def main():
    global SCHEDULER
    parser=argparse.ArgumentParser(description='Rednote Watchdesk local configuration page')
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--no-scheduler',action='store_true')
    args=parser.parse_args()
    os.umask(0o077);ensure_home()
    SCHEDULER=not args.no_scheduler
    if SCHEDULER:threading.Thread(target=scheduler,daemon=True).start()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print('Rednote Watchdesk: http://127.0.0.1:'+str(server.server_port),flush=True)
    server.serve_forever()

if __name__=='__main__':main()
