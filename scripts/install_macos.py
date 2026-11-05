#!/usr/bin/env python3
"""Install per-user services; no personal paths or credentials in source."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from rednote_watchdesk.paths import RUNTIME, PROJECT, MONITOR_LABEL
from rednote_watchdesk.config_server import ensure_home

LABELS = {'monitor':MONITOR_LABEL, 'watchdog':'app.rednote-watchdesk.watchdog','ui':'app.rednote-watchdesk.ui'}

def main():
    if sys.platform!='darwin':raise SystemExit('本安装器仅用于 macOS；其他系统请运行 ./run.sh')
    parser=argparse.ArgumentParser();parser.add_argument('--uninstall',action='store_true');args=parser.parse_args()
    domain='gui/'+str(os.getuid());agents=Path.home()/'Library/LaunchAgents';agents.mkdir(exist_ok=True)
    ensure_home();logs=RUNTIME/'data'
    for role,label in LABELS.items():
        path=agents/(label+'.plist')
        subprocess.run(['/bin/launchctl','bootout',domain+'/'+label],capture_output=True)
        if args.uninstall:
            path.unlink(missing_ok=True);continue
        if role=='monitor':argv=[sys.executable,'-m','rednote_watchdesk.tick'];interval=60
        elif role=='watchdog':argv=['/usr/bin/python3','-m','rednote_watchdesk.watchdog'];interval=300
        else:argv=[sys.executable,'-m','rednote_watchdesk.config_server','--no-scheduler'];interval=None
        payload={'Label':label,'ProgramArguments':argv,'WorkingDirectory':str(PROJECT),
                 'EnvironmentVariables':{'REDNOTE_WATCHDESK_HOME':str(RUNTIME)},
                 # Browser workers inherit launchd resource limits. Background can
                 # stall Chrome even while foreground diagnostics are healthy.
                 'RunAtLoad':True,'ProcessType':'Background' if role=='watchdog' else 'Interactive','LimitLoadToSessionType':'Aqua',
                 'StandardOutPath':str(logs/(role+'-launchd.out.log')),
                 'StandardErrorPath':str(logs/(role+'-launchd.err.log'))}
        if interval:payload['StartInterval']=interval
        else:payload['KeepAlive']=True
        path.write_bytes(plistlib.dumps(payload));path.chmod(0o600)
        subprocess.run(['/bin/launchctl','bootstrap',domain,str(path)],check=True)
    print('已卸载后台服务，私有数据保留。' if args.uninstall else '后台服务已安装：http://127.0.0.1:8767')

if __name__=='__main__':main()
