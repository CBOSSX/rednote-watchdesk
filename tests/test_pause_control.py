import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rednote_watchdesk import pause_control, query_progress, watch
from tests import test_manual_check as manual
from tests.test_manual_check import TARGET, OTHER


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root/'data'
        self.data.mkdir()
        self.patch = patch.multiple(watch, ROOT=self.root, DATA=self.data, STATE=self.data/'state.json')
        self.patch.start()
        self.config = {'targets': [TARGET, OTHER], 'interval_seconds': 0}
        watch.write_json(watch.STATE, {'paused': False, 'targets': {TARGET: {}, OTHER: {}}})

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_pause_is_accepted_while_worker_holds_lock_and_worker_keeps_results(self):
        state = watch.read_json(watch.STATE)
        with (self.data/'watch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pause_control.request_pause(self.data)
            self.assertTrue(watch.read_json(watch.STATE)['paused'])
            state['targets'][TARGET]['last_status'] = 'not_found'
            watch.write_json(watch.STATE, state)
        state = json.loads(watch.STATE.read_text())
        self.assertTrue(state['paused'])
        self.assertEqual(state['targets'][TARGET]['last_status'], 'not_found')

    def test_pause_between_queries_stops_next_target_and_follow(self):
        def query(page, target):
            pause_control.request_pause(self.data)
            return {'status': 'not_found', 'result': None, 'result_count': 1, 'checked_at': watch.now()}
        with patch.object(watch, 'browser_page', manual.ManualCheckTests.page), \
             patch.object(watch, 'search', side_effect=query) as search, \
             patch.object(watch, 'finish_follows') as follow, patch.object(watch, 'webhook'), patch('builtins.print'):
            watch.run_check(self.config, force=True)
        search.assert_called_once()
        follow.assert_not_called()
        self.assertTrue(watch.read_json(watch.STATE)['paused'])
        self.assertEqual(query_progress.read(self.data)['status'], 'cancelled')

    def test_pause_cancels_queued_manual_but_explicit_new_manual_is_allowed(self):
        old_token = pause_control.token(self.data)
        pause_control.request_pause(self.data)
        result = {'status': 'not_found', 'result': None, 'result_count': 1, 'checked_at': watch.now()}
        with patch.object(watch, 'browser_page', manual.ManualCheckTests.page), \
             patch.object(watch, 'search', return_value=result) as search, \
             patch.object(watch, 'webhook'), patch('builtins.print'):
            watch.run_check(self.config, force=True, pause_token=old_token)
            search.assert_not_called()
            watch.run_check(self.config, force=True)
            self.assertEqual(search.call_count, 2)
        self.assertTrue(watch.read_json(watch.STATE)['paused'])

    def test_cli_pause_works_while_busy_and_resume_clears_request(self):
        watch.write_json(self.root/'config.json', self.config)
        env = dict(os.environ, REDNOTE_WATCHDESK_HOME=str(self.root))
        with (self.data/'watch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, '-m', 'rednote_watchdesk.watch', 'pause'],
                                    env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), 'PAUSE', result.stderr)
        result = subprocess.run([sys.executable, '-m', 'rednote_watchdesk.watch', 'resume'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(pause_control.token(self.data))
        self.assertFalse(watch.read_json(watch.STATE)['paused'])


class ProgressTests(unittest.TestCase):
    def test_stage_and_counts_are_persisted_and_failed_runs_do_not_inflate_normal_average(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            with patch('time.time', return_value=100):
                progress = query_progress.Progress(data, manual=True)
            with patch('time.time', return_value=105):
                progress.update(stage='homepage_access', total=1)
            snapshot = query_progress.read(data)
            self.assertEqual(snapshot['stage_label'], '读取首页正文')
            self.assertEqual(snapshot['stage_started_at'], 105)
            progress.update(completed=1, successful=1)
            with patch('time.time', return_value=110):
                progress.finish(0)
            self.assertEqual(query_progress.timings(data)['normal_single_target_average_seconds'], 10)
            failed = query_progress.Progress(data)
            failed.update(completed=1, failed=1)
            failed.finish(2)
            self.assertEqual(query_progress.timings(data)['normal_single_target_samples'], 1)
