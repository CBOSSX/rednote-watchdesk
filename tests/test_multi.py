import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from rednote_watchdesk import watch
from tests import test_watch

A='example_alpha'
B='example_beta'


def outcome(target,found=False):
    return {'status':'found' if found else 'not_found','result':{'red_id':target,'name':target,'url':'https://www.xiaohongshu.com/user/profile/000000000000000000000001'} if found else None,'result_count':1,'checked_at':watch.now()}


class MultiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir()
        self.p=patch.multiple(watch,ROOT=self.root,DATA=self.data,STATE=self.data/'state.json');self.p.start()
        self.cfg={'target':A,'targets':[A,B],'interval_seconds':0}
    def tearDown(self):self.p.stop();self.tmp.cleanup()
    def state(self):return watch.read_json(watch.STATE)
    def test_finding_one_keeps_other_monitoring(self):
        def search(page,t):return outcome(t,t==A)
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=search) as query,patch.object(watch,'webhook') as notify:
            watch.run_check(self.cfg);watch.run_check(self.cfg)
            self.assertEqual([c.args[1] for c in query.call_args_list],[A,B,B]);notify.assert_called_once()
        self.assertTrue(self.state()['targets'][A]['completed']);self.assertFalse(self.state()['completed'])
    def test_both_found_pending_notifications_retry_without_requery(self):
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t,True)) as query:
            with patch.object(watch,'webhook',side_effect=RuntimeError('offline')):watch.run_check(self.cfg)
            self.assertTrue(self.state()['query_complete']);self.assertFalse(self.state()['completed'])
            with patch.object(watch,'webhook') as notify:watch.run_check(self.cfg);self.assertEqual(notify.call_count,2)
            self.assertEqual(query.call_count,2)
        self.assertTrue(self.state()['completed'])
    def test_one_failing_target_retries_independently_without_pausing(self):
        def search(p,t):
            if t==A:raise TimeoutError('timeout')
            return outcome(t)
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=search) as query,patch.object(watch,'webhook') as notify:
            clock=10000
            for count,delay in enumerate([300,600,1200,1800,1800,1800],1):
                with patch.object(watch.time,'time',return_value=clock):watch.run_check(self.cfg)
                entry=self.state()['targets'][A]
                self.assertEqual(entry['next_retry_epoch'],clock+delay)
                self.assertEqual(entry['consecutive_errors'],count)
                self.assertFalse(entry.get('paused',False))
                self.assertEqual(notify.call_count,1 if count>=3 else 0)
                clock+=delay
            self.assertEqual(sum(c.args[1]==A for c in query.call_args_list),6)
            self.assertEqual(sum(c.args[1]==B for c in query.call_args_list),6)
        self.assertFalse(self.state().get('paused',False))

    def test_persisted_retries_recover_and_return_to_normal_interval(self):
        cfg=dict(self.cfg,interval_seconds=1800)
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=TimeoutError('timeout')) as query,patch.object(watch,'webhook') as notify:
            for clock in [10000,10300,10900]:
                with patch.object(watch.time,'time',return_value=clock):watch.run_check(cfg)
            self.assertEqual(notify.call_count,2)
            with patch.object(watch.time,'time',return_value=11000):watch.run_check(cfg)
            self.assertEqual(query.call_count,6)
            query.side_effect=lambda p,t:outcome(t)
            with patch.object(watch.time,'time',return_value=12100):watch.run_check(cfg)
            self.assertEqual(notify.call_count,4)
            for entry in self.state()['targets'].values():
                self.assertNotIn('next_retry_epoch',entry)
                self.assertNotIn('query_fault_notified',entry)
                self.assertEqual(entry['consecutive_errors'],0)
            with patch.object(watch.time,'time',return_value=12400):watch.run_check(cfg)
            self.assertEqual(query.call_count,8)
            with patch.object(watch.time,'time',return_value=13900):watch.run_check(cfg)
            self.assertEqual(query.call_count,10);self.assertEqual(notify.call_count,4)

    def test_offline_fault_and_recovery_are_delivered_in_order(self):
        watch.write_json(watch.STATE,{'targets':{A:{'consecutive_errors':2},B:{'completed':True,'found':outcome(B,True)['result']}}})
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=TimeoutError('offline')) as query:
            with patch.object(watch,'webhook',side_effect=RuntimeError('offline')):
                with patch.object(watch.time,'time',return_value=10000):watch.run_check(self.cfg)
                query.side_effect=lambda p,t:outcome(t)
                with patch.object(watch.time,'time',return_value=11200):watch.run_check(self.cfg)
            state=self.state()
            self.assertEqual(state['pending']['kind'],'query_fault')
            self.assertEqual(state['notification_queue'][0]['kind'],'query_recovery')
            with patch.object(watch,'webhook') as notify:
                with patch.object(watch.time,'time',return_value=11201):watch.run_check(self.cfg)
                self.assertIn('查询异常',notify.call_args_list[0].args[0])
                self.assertIn('查询已恢复',notify.call_args_list[1].args[0])

    def test_browser_startup_failures_do_not_pause(self):
        with patch.object(watch,'browser_page',side_effect=RuntimeError('browser launch failed')),patch.object(watch,'webhook'):
            for clock in [10000,10300,10900,12100]:
                with patch.object(watch.time,'time',return_value=clock):watch.run_check(self.cfg)
        self.assertFalse(self.state().get('paused',False))
        for entry in self.state()['targets'].values():
            self.assertFalse(entry.get('paused',False));self.assertEqual(entry['consecutive_errors'],4)
    def test_security_gate_pauses_all(self):
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=watch.SitePaused('login_required')) as query,patch.object(watch,'webhook') as notify:
            watch.run_check(self.cfg);watch.run_check(self.cfg);query.assert_called_once();notify.assert_called_once()
        self.assertTrue(self.state()['paused'])
    def test_legacy_completed_account_is_not_requeried_after_adding_target(self):
        watch.write_json(watch.STATE,{'completed':True,'found':outcome(A,True)['result']})
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t)) as query,patch.object(watch,'webhook') as notify:
            watch.run_check(self.cfg);self.assertEqual(query.call_args.args[1],B);notify.assert_not_called()
        self.assertFalse(self.state()['completed']);self.assertTrue(self.state()['targets'][A]['completed'])
    def test_resume_recovery_notice_waits_for_success(self):
        watch.write_json(watch.STATE,{'recovery_pending':True})
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t)),patch.object(watch,'webhook') as notify:
            watch.run_check(self.cfg);watch.run_check(self.cfg);notify.assert_called_once()
            self.assertIn('已恢复',notify.call_args.args[0]);self.assertIn(B,notify.call_args.args[0])
    def test_only_due_target_is_queried_when_clocks_are_staggered(self):
        import time
        now=time.time()
        cfg=dict(self.cfg,interval_seconds=1800)
        watch.write_json(watch.STATE,{'targets':{A:{'last_attempt_epoch':now},B:{'last_attempt_epoch':now-1900}}})
        with patch.object(watch,'browser_page',test_watch.WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t)) as query,patch.object(watch,'webhook'):
            watch.run_check(cfg);self.assertEqual(query.call_args.args[1],B);query.assert_called_once()


if __name__=='__main__':unittest.main()
