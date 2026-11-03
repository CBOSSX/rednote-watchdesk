import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from rednote_watchdesk import tick
from rednote_watchdesk import watchdog


class WatchdogTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'Mach clock requires macOS')
    def test_awake_clock_has_same_origin_in_separate_processes(self):
        here=watchdog.awake_clock()
        other=float(subprocess.check_output(['/usr/bin/python3','-c','from rednote_watchdesk import watchdog; print(watchdog.awake_clock())'],text=True))
        self.assertLess(abs(other-here),10)
        self.assertGreater(here,10)
    def test_fault_deduplicates_and_recovery_is_ordered(self):
        s={}
        watchdog.transition(s,'failure',100);watchdog.transition(s,'failure',200)
        self.assertEqual(len(s['queue']),1)
        watchdog.transition(s,None,300);watchdog.transition(s,None,400)
        self.assertEqual([e['kind'] for e in s['queue']],['fault','recovery'])

    def test_offline_retains_events_then_delivers_in_order(self):
        s={};watchdog.transition(s,'failure',100);watchdog.transition(s,None,200)
        watchdog.deliver(s,sender=Mock(side_effect=RuntimeError('offline')),persist=lambda s:None)
        self.assertEqual(len(s['queue']),2)
        sender=Mock();watchdog.deliver(s,sender=sender,persist=lambda s:None)
        self.assertEqual(sender.call_count,2)
        self.assertIn('异常',sender.call_args_list[0].args[0]);self.assertIn('恢复',sender.call_args_list[1].args[0])
        self.assertFalse(s['queue'])

    def test_wake_reboot_and_initial_grace(self):
        s={}
        self.assertTrue(watchdog.clock_grace(s,10000,100))
        self.assertFalse(watchdog.clock_grace(s,10700,800))
        self.assertTrue(watchdog.clock_grace(s,14300,801))
        self.assertFalse(watchdog.clock_grace(s,15000,1501))
        self.assertTrue(watchdog.clock_grace(s,15100,5))

    def test_missing_hung_stale_and_no_attempts(self):
        m={'last_attempt_epoch':9900};h={'status':'idle','updated_at':9990}
        self.assertIsNone(watchdog.diagnose(10000,m,h,True))
        self.assertIn('未加载',watchdog.diagnose(10000,m,h,False))
        self.assertIn('5分钟',watchdog.diagnose(10000,m,{'status':'checking','started_at':9600},True))
        self.assertIn('15分钟',watchdog.diagnose(10000,m,{'updated_at':9000},True))
        self.assertIn('45分钟',watchdog.diagnose(10000,{'last_attempt_epoch':7000},h,True))

    def test_paused_and_completed_do_not_alarm(self):
        for m in [{'paused':True},{'completed':True},{'query_complete':True}]:self.assertIsNone(watchdog.diagnose(10000,m,{'status':'fatal'},False))

    def test_resume_waits_for_next_scheduled_check_without_false_alarm(self):
        m={'resumed_at':9990}
        self.assertIsNone(watchdog.diagnose(10000,m,{'status':'idle','updated_at':9990},True))

    def test_handled_query_error_is_not_scheduler_fault(self):
        self.assertIsNone(watchdog.diagnose(10000,{'last_attempt_epoch':9900},{'status':'handled_error','updated_at':9990,'reason':'worker_exit_2'},True))

    def test_idle_does_not_prematurely_claim_recovery(self):
        h={'status':'idle','updated_at':9990,'worker_fault':'worker_timeout'}
        self.assertIsNotNone(watchdog.diagnose(10000,{'last_attempt_epoch':9900},h,True))
    def test_pending_follow_is_not_suppressed_as_completed_search(self):
        monitor={'query_complete':True,'follow_pending':True,'last_attempt_epoch':9900}
        self.assertIn('未加载',watchdog.diagnose(10000,monitor,{'status':'idle','updated_at':9990},False))
        heartbeat={'status':'checking','started_at':9500,'timeout_seconds':480,'updated_at':9990}
        self.assertIsNone(watchdog.diagnose(10000,monitor,heartbeat,True))
        heartbeat['started_at']=9400
        self.assertIn('9分钟',watchdog.diagnose(10000,monitor,heartbeat,True))


class TickTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir()
        (self.root/'config.json').write_text('{"interval_seconds":1800}')
        (self.data/'state.json').write_text('{"paused":false,"last_attempt_epoch":0}')
        self.patcher=patch.multiple(tick,ROOT=self.root,DATA=self.data,HEALTH=self.root/'health');self.patcher.start()
    def tearDown(self):self.patcher.stop();self.tmp.cleanup()
    def health(self):return json.loads((self.data/'heartbeat.json').read_text())
    def test_bad_config_is_reportable(self):
        (self.root/'config.json').write_text('broken');tick.main();self.assertEqual(self.health()['status'],'fatal')
    def test_missing_worker_is_reportable(self):
        with patch.object(tick.subprocess,'Popen',side_effect=FileNotFoundError()):tick.main()
        self.assertIn('FileNotFoundError',self.health()['worker_fault'])
    def test_due_second_target_is_not_hidden_by_recent_first_target(self):
        import time
        now=time.time()
        (self.root/'config.json').write_text(json.dumps({'targets':['example_alpha','example_beta'],'interval_seconds':1800}))
        (self.data/'state.json').write_text(json.dumps({'last_attempt_epoch':now,'targets':{'example_alpha':{'last_attempt_epoch':now},'example_beta':{'last_attempt_epoch':now-1900}}}))
        child=Mock();child.wait.return_value=0
        with patch.object(tick.subprocess,'Popen',return_value=child) as worker:
            tick.main();worker.assert_called_once()
            self.assertEqual(worker.call_args.args[0], [sys.executable, '-m', 'rednote_watchdesk.watch', 'check'])
            self.assertEqual(worker.call_args.kwargs['cwd'], tick.PROJECT)
    def test_timeout_terminates_own_group(self):
        child=Mock(pid=12345);child.wait.side_effect=[subprocess.TimeoutExpired('worker',180),0]
        with patch.object(tick.subprocess,'Popen',return_value=child),patch.object(tick.os,'killpg') as kill:
            tick.main();kill.assert_called_once_with(12345,signal.SIGTERM)
        self.assertEqual(self.health()['worker_fault'],'worker_timeout')
    def test_retry_deadline_overrides_interval_but_prevents_early_queries(self):
        import time
        now=time.time()
        (self.root/'config.json').write_text(json.dumps({'targets':['example_alpha'],'interval_seconds':1800}))
        def save(deadline):
            (self.data/'state.json').write_text(json.dumps({'targets':{'example_alpha':{'last_attempt_epoch':now-400,'next_retry_epoch':deadline}}}))
        child=Mock();child.wait.return_value=0
        with patch.object(tick.subprocess,'Popen',return_value=child) as worker:
            save(now+200);tick.main();worker.assert_not_called()
            save(now-1);tick.main();worker.assert_called_once()
    def test_finished_queries_still_dispatch_follow_tasks(self):
        cfg={'targets':['example_alpha'],'interval_seconds':1800,'auto_follow_enabled':True,'auto_follow_targets':['example_alpha']}
        (self.root/'config.json').write_text(json.dumps(cfg))
        (self.data/'state.json').write_text(json.dumps({'query_complete':True,'completed':False,'targets':{'example_alpha':{'found':{'red_id':'example_alpha'},'follow':{'status':'pending'}}}}))
        child=Mock();child.wait.return_value=0
        with patch.object(tick.subprocess,'Popen',return_value=child) as worker:
            tick.main();worker.assert_called_once();child.wait.assert_called_once_with(timeout=480)
    def test_fault_survives_idle_until_actual_worker_runs(self):
        tick.heartbeat('fatal',reason='worker_timeout');(self.data/'state.json').write_text('{"paused":true}')
        tick.main();self.assertEqual(self.health()['worker_fault'],'worker_timeout')
        (self.data/'state.json').write_text('{"paused":false,"last_attempt_epoch":0}')
        child=Mock();child.wait.return_value=0
        with patch.object(tick.subprocess,'Popen',return_value=child):tick.main()
        self.assertIsNone(self.health()['worker_fault'])


if __name__=='__main__':unittest.main()
