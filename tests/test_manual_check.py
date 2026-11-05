import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rednote_watchdesk import config_server as server, follow_jobs, tick, watch

TARGET = 'example_alpha'
OTHER = 'example_beta'
FOUND = {'red_id': OTHER, 'name': 'Beta', 'url': 'https://www.xiaohongshu.com/user/profile/' + '0' * 24}


class ManualCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.patcher = patch.multiple(watch, ROOT=self.root, DATA=self.data, STATE=self.data/'state.json')
        self.patcher.start()
        self.config = {'targets': [TARGET, OTHER], 'interval_seconds': 1800}
        self.outcome = {'status': 'not_found', 'result': None, 'result_count': 15, 'checked_at': watch.now()}

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    @staticmethod
    @contextlib.contextmanager
    def page(**kwargs):
        yield object()

    def save(self, paused):
        watch.write_json(watch.STATE, {'paused': paused, 'targets': {
            TARGET: {'last_attempt_epoch': time.time(), 'next_retry_epoch': time.time()+1800},
            OTHER: {'found': FOUND, 'completed': True}}})

    def test_manual_ignores_deadline_keeps_pause_and_skips_completed_targets(self):
        for paused in (True, False):
            with self.subTest(paused=paused):
                self.save(paused)
                with patch.object(watch, 'browser_page', self.page), \
                     patch.object(watch, 'search', return_value=self.outcome) as query, \
                     patch.object(watch, 'webhook'), patch('builtins.print'):
                    watch.run_check(self.config)
                    query.assert_not_called()
                    self.assertEqual(watch.run_check(self.config, force=True), 0)
                    query.assert_called_once()
                    self.assertEqual(query.call_args.args[1], TARGET)
                    state = watch.read_json(watch.STATE)
                    self.assertEqual(state['paused'], paused)
                    self.assertEqual(state['manual_check']['successful'], 1)
                    self.assertEqual(state['manual_check']['failed'], 0)
                    self.assertNotIn('next_retry_epoch', state['targets'][TARGET])
                    watch.run_check(self.config)
                    query.assert_called_once()

    def test_manual_risk_stops_before_follow_and_pauses_running_monitor(self):
        self.save(False)
        with patch.object(watch, 'browser_page', self.page), \
             patch.object(watch, 'search', side_effect=watch.SitePaused('ip_risk')), \
             patch.object(watch, 'finish_follows') as follow, patch.object(watch, 'webhook'), patch('builtins.print'):
            self.assertEqual(watch.run_check(self.config, force=True), 2)
            follow.assert_not_called()
        state = watch.read_json(watch.STATE)
        self.assertTrue(state['paused'])
        self.assertEqual(state['pause_reason'], 'ip_risk')
        self.assertEqual(state['manual_check']['failed'], 1)

    def test_manual_transient_failure_keeps_pause_and_backoff(self):
        self.save(True)
        with patch.object(watch, 'browser_page', self.page), \
             patch.object(watch, 'search', side_effect=TimeoutError('body missing')), \
             patch.object(watch, 'webhook'), patch('builtins.print'):
            self.assertEqual(watch.run_check(self.config, force=True), 2)
        state = watch.read_json(watch.STATE)
        self.assertTrue(state['paused'])
        self.assertEqual(state['manual_check']['failed'], 1)
        self.assertGreater(state['targets'][TARGET]['next_retry_epoch'], time.time())

    def test_manual_found_uses_configured_follow_without_resuming_polling(self):
        self.save(True)
        config = dict(self.config, auto_follow_enabled=True, auto_follow_targets=[TARGET])
        watch.write_json(self.data/'follow-account.json', {'name': 'Actor', 'profile_url': 'expected'})
        found = dict(self.outcome, status='found', result=dict(FOUND, red_id=TARGET))
        with patch.object(watch, 'browser_page', self.page), patch.object(watch, 'search', return_value=found), \
             patch.object(follow_jobs, 'follow_once', return_value={'status': 'followed'}) as follow, \
             patch.object(watch, 'webhook') as notify, patch('builtins.print'):
            self.assertEqual(watch.run_check(config, force=True), 0)
            follow.assert_called_once()
            self.assertEqual([c.kwargs['event']['kind'] for c in notify.call_args_list], ['found', 'follow_success'])
        self.assertTrue(watch.read_json(watch.STATE)['paused'])

    def test_tick_dispatches_manual_even_when_paused_and_reports_busy(self):
        self.save(True)
        watch.write_json(self.root/'config.json', self.config)
        with patch.multiple(tick, ROOT=self.root, DATA=self.data, HEALTH=self.root/'health'), \
             patch.object(tick.subprocess, 'Popen', return_value=Mock(wait=Mock(return_value=0))) as worker:
            self.assertEqual(tick.main(force=True), 0)
            self.assertEqual(worker.call_args.args[0][3], 'check-now')
            worker.reset_mock()
            with (self.data/'tick.lock').open('a') as lock, patch('builtins.print') as output:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(tick.main(force=True), 0)
                output.assert_called_once_with('BUSY', flush=True)
                worker.assert_not_called()

    def test_manual_budget_accounts_for_all_pending_targets(self):
        watch.write_json(self.root/'config.json', self.config)
        watch.write_json(watch.STATE, {'paused': True, 'targets': {TARGET: {}, OTHER: {}}})
        child = Mock(wait=Mock(return_value=0))
        with patch.multiple(tick, ROOT=self.root, DATA=self.data, HEALTH=self.root/'health'), \
             patch.object(tick.subprocess, 'Popen', return_value=child):
            tick.main(force=True)
        self.assertEqual(child.wait.call_args.kwargs['timeout'], 355)

    def test_watch_lock_rejects_manual_without_mutating_state(self):
        self.save(True)
        watch.write_json(self.root/'config.json', self.config)
        before = watch.STATE.read_bytes()
        with (self.data/'watch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, '-m', 'rednote_watchdesk.watch', 'check-now'],
                env=dict(os.environ, REDNOTE_WATCHDESK_HOME=str(self.root)), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'BUSY')
        self.assertEqual(watch.STATE.read_bytes(), before)

    def test_cli_no_pending_targets_returns_summary_without_browser(self):
        watch.write_json(self.root/'config.json', {'target': OTHER})
        watch.write_json(watch.STATE, {'paused': True, 'targets': {OTHER: {'found': FOUND, 'completed': True}}})
        result = subprocess.run([sys.executable, '-m', 'rednote_watchdesk.tick', '--now'],
            env=dict(os.environ, REDNOTE_WATCHDESK_HOME=str(self.root)), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)['manual_check']
        self.assertTrue(summary['paused'])
        self.assertEqual(summary['successful'], 0)
        self.assertIn('没有待查询目标', summary['message'])

    def test_job_uses_bounded_scheduler_and_returns_summary_without_waking_polling(self):
        class ImmediateThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()

        for output, code, status, message in [
            ('BUSY', 0, 'failed', '当前任务正忙'),
            (json.dumps({'manual_check': {'successful': 1, 'failed': 0, 'found': 0}}), 0, 'succeeded', '查询成功 1 个'),
            (json.dumps({'manual_check': {'successful': 0, 'failed': 1, 'found': 0}}), 2, 'failed', '失败 1 个'),
            ('', 1, 'failed', '本轮调度未完成')]:
            with self.subTest(output=output):
                def run(argv, **kwargs):
                    self.assertEqual(argv[2:4], ['rednote_watchdesk.tick', '--now'])
                    kwargs['stdout'].write(output)
                    return SimpleNamespace(returncode=code)
                with patch.multiple(server, RUNTIME=self.root, JOBS={}), \
                     patch.object(server.threading, 'Thread', ImmediateThread), \
                     patch.object(server.subprocess, 'run', side_effect=run), patch.object(server, 'wake') as wake:
                    jid = server.start_job('check-now', title='立即查询一次')
                    self.assertEqual(server.JOBS[jid]['status'], status)
                    self.assertIn(message, server.JOBS[jid]['message'])
                    wake.assert_not_called()

    def test_second_ui_job_is_rejected(self):
        with patch.object(server, 'JOBS', {'first': {'status': 'running'}}):
            with self.assertRaisesRegex(RuntimeError, '已有操作正在进行'):
                server.start_job('check-now')
