"""Small, persistent query progress snapshots; browser work remains serialized."""
import json
import os
import time
import uuid
from pathlib import Path
from rednote_watchdesk.query_diagnostics import STAGES

LABELS = dict(STAGES, prepare='准备查询', record_result='保存查询结果',
              deliver_notifications='发送待处理通知', auto_follow='核对并执行关注',
              diagnostics='采集失败诊断', closing_browser='关闭浏览器并保存会话',
              done='本轮已完成', failed='本轮查询失败', cancelled='本轮已暂停')
BUDGETS = {'launch_browser': 180, 'navigate_home': 45, 'navigate_search': 45,
           'homepage_access': 15, 'check_access': 15, 'select_users': 15,
           'wait_user_results': 40, 'verify_identity': 10, 'deliver_notifications': 20,
           'diagnostics': 5, 'closing_browser': 15}


def read(data, name='query-progress.json'):
    try:
        return json.loads((Path(data)/name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _write(path, value):
    tmp = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False)
    tmp.replace(path)


class Progress:
    def __init__(self, data, manual=False):
        self.data = Path(data)
        self.value = {'run_id': uuid.uuid4().hex, 'job_id': os.environ.get('REDNOTE_WATCHDESK_JOB_ID'),
                      'manual': manual, 'status': 'running', 'started_at': time.time(),
                      'completed': 0, 'total': 0, 'successful': 0, 'failed': 0, 'found': 0,
                      'stage_history': []}
        self.update(stage='prepare')

    def update(self, **fields):
        now = time.time()
        if 'stage' in fields:
            if self.value.get('stage'):
                self.value['stage_history'].append({'stage': self.value['stage'],
                    'elapsed_seconds': round(now-self.value['stage_started_at'], 2)})
                self.value['stage_history'] = self.value['stage_history'][-32:]
            self.value['stage_started_at'] = now
            self.value['stage_label'] = LABELS.get(fields['stage'], '执行查询')
            self.value['stage_budget_seconds'] = BUDGETS.get(fields['stage'])
        self.value.update(fields, updated_at=now)
        try:
            _write(self.data/'query-progress.json', self.value)
        except OSError:
            pass  # Progress reporting must not turn a successful query into a failure.

    def finish(self, code, cancelled=False):
        status = 'cancelled' if cancelled else 'failed' if code else 'done'
        self.update(stage=status, status=status, finished_at=time.time())
        if self.value['completed']:
            history = read(self.data, 'query-timings.json').get('runs', [])
            history.append({'elapsed_seconds': round(self.value['finished_at']-self.value['started_at'], 2),
                            'targets': self.value['completed'], 'failed': self.value['failed'], 'status': status})
            try:
                _write(self.data/'query-timings.json', {'runs': history[-20:]})
            except OSError:
                pass


def timings(data):
    runs = read(data, 'query-timings.json').get('runs', [])
    normal = [r['elapsed_seconds'] for r in runs if r['status'] == 'done' and not r['failed'] and r['targets'] == 1]
    return {'normal_single_target_samples': len(normal),
            'normal_single_target_average_seconds': round(sum(normal)/len(normal), 1) if normal else None}
