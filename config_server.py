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

from paths import RUNTIME, PROJECT, MONITOR_LABEL
from watch_config import normalize, signature

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
    state = read(RUNTIME/'data/state.json')
    heartbeat = read(RUNTIME/'data/heartbeat.json')
    notice = read(RUNTIME/'secrets.json')
    plan = normalize(cfg)
    people = []
    for person in plan['people']:
        saved = state.get('people',{}).get(person['id'],{})
        matched = saved.get('matched_target') or next((t for t in person['candidates'] if state.get('targets',{}).get(t,{}).get('found')),None)
        entry = state.get('targets',{}).get(matched,{}) if matched else {}
        checks = [state.get('targets',{}).get(t,{}) for t in person['candidates']]
        people.append({'id':person['id'],'matched_target':matched,'completed':saved.get('completed',entry.get('completed',False)),
                       'result':entry.get('found'),'follow_status':entry.get('follow',{}).get('status'),
                       'last_checked_at':max((e.get('last_checked_at','') for e in checks),default='')})
    accounts = {}
    for role, filename in [('search','search-account.json'),('follow','follow-account.json')]:
        account = read(RUNTIME/'data'/filename)
        accounts[role] = {k:account[k] for k in ('name','red_id','enrolled_at') if k in account}
    with JOB_LOCK:
        jobs = [dict(j) for j in JOBS.values()][-8:]
    return {'config':plan,'version':signature(cfg),'pending_apply':state.get('config_signature')!=signature(cfg),
            'state':{'paused':state.get('paused',False),'completed':state.get('completed',False),
                     'follow_paused':state.get('follow_paused',False),'people':people,
                     'heartbeat_at':heartbeat.get('updated_at'),'worker_fault':heartbeat.get('worker_fault'),
                     'pending_notifications':bool(state.get('pending'))},
            'accounts':accounts,'notifications':{'configured':bool(notice.get('webhook_url')),
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
        plan = normalize(body.get('config'))
        if plan['interval_seconds'] < 60:
            raise ValueError('轮询间隔至少为 1 分钟')
        # Validate migration against an isolated copy; reject removal of a matched candidate.
        from watch import migrate_state
        migrate_state(read(RUNTIME/'data/state.json'),plan)
        write(RUNTIME/'config.json',plan)
    wake()


def save_notifications(body):
    current = read(RUNTIME/'secrets.json')
    url = body.get('webhook_url','').strip()
    if url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme!='https' or parts.hostname not in ('open.larkoffice.com','open.feishu.cn','open.larksuite.com') or not parts.path.startswith('/open-apis/bot/v2/hook/') or parts.username or parts.password:
            raise ValueError('请填写飞书/Lark 官方自定义机器人 HTTPS webhook')
        current['webhook_url'] = url
    if body.get('signing_secret'):
        current['signing_secret'] = body['signing_secret']
    users = body.get('mention_users',[])
    from mentions import mention_tags
    mention_tags({'mention_users':users},card=True)
    current['mention_users'] = users
    write(RUNTIME/'secrets.json',current)
    wake()


def start_job(command, *, fresh=False, title='操作'):
    with JOB_LOCK:
        if any(j['status']=='running' for j in JOBS.values()):
            raise RuntimeError('已有操作正在进行，请完成后再试')
        jid = secrets.token_hex(8)
        JOBS[jid] = {'id':jid,'title':title,'status':'running','started_at':time.time()}
    def run():
        argv=[sys.executable,str(PROJECT/'watch.py'),command]
        if command in ('login','follow-login'):
            argv += ['--seconds','600']
            if fresh:argv += ['--fresh-login']
        log=RUNTIME/'data'/('ui-job-'+jid+'.log')
        try:
            with log.open('w') as out:
                log.chmod(0o600)
                proc=subprocess.run(argv,stdout=out,stderr=subprocess.STDOUT,timeout=620)
            busy = log.read_text().strip()=='BUSY'
            success=proc.returncode==0 and not busy
            message='操作完成' if success else '当前任务正忙，请稍后重试' if busy else '操作未完成；请检查登录窗口或稍后重试'
        except Exception:
            success,message=False,'操作未完成，请稍后重试'
        with JOB_LOCK:
            JOBS[jid].update(status='succeeded' if success else 'failed',message=message,finished_at=time.time())
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
            assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','application/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
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
            if self.path=='/api/login':
                role=body.get('role')
                if role not in ('search','follow'):raise ValueError('登录角色无效')
                jid=start_job('login' if role=='search' else 'follow-login',fresh=body.get('fresh') is True,title='小号登录' if role=='search' else '大号登录')
                return self.respond(202,{'ok':True,'job_id':jid})
            if self.path=='/api/control':
                action=body.get('action')
                if action not in ('pause','resume','follow-resume','test-webhook'):raise ValueError('操作无效')
                return self.respond(202,{'ok':True,'job_id':start_job(action,title={'pause':'暂停监测','resume':'恢复监测','follow-resume':'恢复自动关注','test-webhook':'测试飞书通知'}[action])})
            self.respond(404,{'error':'未找到'})
        except RuntimeError as error:self.respond(409,{'error':str(error)})
        except (ValueError,TypeError,KeyError) as error:self.respond(400,{'error':str(error)})
        except Exception:self.respond(500,{'error':'操作失败，请检查本机日志'})


def scheduler():
    while True:
        try:
            subprocess.run([sys.executable,str(PROJECT/'tick.py')],timeout=510)
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
