import contextlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import follow
import follow_jobs
import watch
from test_watch import WorkflowTests

A='example_alpha'; B='example_beta'
URL='https://www.xiaohongshu.com/user/profile/000000000000000000000001'
ACTOR='https://www.xiaohongshu.com/user/profile/111111111111111111111111'
ACCOUNT={'name':'test big account','profile_url':ACTOR}
FOUND={'red_id':A,'name':'target','url':URL}

class Page:
    def __init__(self, followed=False):
        self.url=URL; self.followed=followed; self.clicks=0; self.reloads=0
        self.actor=ACTOR; self.red_id=A; self.checkpointed=False; self.lose_follow_on_reload=False
    def locator(self, selector):
        page=self
        class Locator:
            def evaluate_all(self, script):return [page.actor]
            def wait_for(self, **kwargs):pass
            def count(self):return 1
            def is_visible(self):return True
            def inner_text(self):
                return '小红书号：'+page.red_id if 'user-redId' in selector else ('已关注' if page.followed else '关注')
            def click(self, **kwargs):
                assert page.checkpointed
                page.clicks+=1;page.followed=True
        return Locator()
    def goto(self,url,**kwargs):self.url=url;return SimpleNamespace(status=200)
    def wait_for_timeout(self,ms):pass
    def reload(self,**kwargs):
        self.reloads+=1
        if self.lose_follow_on_reload:self.followed=False

class FollowBrowserTests(unittest.TestCase):
    def call(self,page,**kwargs):
        @contextlib.contextmanager
        def browser(data):yield page
        def checkpoint():page.checkpointed=True
        with patch.object(follow,'follow_browser',browser):
            return follow.follow_once(Path('/unused'),ACCOUNT,FOUND,A,[A,B],checkpoint,Mock(),**kwargs)
    def test_already_followed_never_clicks(self):
        page=Page(True);self.assertEqual(self.call(page)['status'],'already_followed');self.assertEqual(page.clicks,0)
    def test_success_requires_checkpoint_click_and_reloaded_confirmation(self):
        page=Page();self.assertEqual(self.call(page)['status'],'followed')
        self.assertEqual(page.clicks,1);self.assertEqual(page.reloads,1)
    def test_unconfirmed_reload_is_failure(self):
        page=Page();page.lose_follow_on_reload=True
        with self.assertRaisesRegex(follow.FollowProblem,'follow_not_confirmed'):self.call(page)
        self.assertEqual(page.clicks,1)
    def test_verify_only_never_clicks_even_if_not_followed(self):
        page=Page()
        with self.assertRaisesRegex(follow.FollowProblem,'follow_not_confirmed'):self.call(page,verify_only=True)
        self.assertEqual(page.clicks,0)
    def test_wrong_actor_or_red_id_never_clicks(self):
        for field,value in [('actor',URL),('red_id',B)]:
            page=Page();setattr(page,field,value)
            with self.subTest(field=field),self.assertRaises(follow.FollowProblem):self.call(page)
            self.assertEqual(page.clicks,0)
    def test_not_allowlisted_or_invalid_url_is_rejected_before_browser(self):
        for found,allowed in [(FOUND,[B]),(dict(FOUND,url='https://evil.example'),[A])]:
            with patch.object(follow,'follow_browser') as browser,self.assertRaises(follow.FollowProblem):
                follow.follow_once(Path('/unused'),ACCOUNT,found,A,allowed,Mock(),Mock())
            browser.assert_not_called()
    def test_login_gate_never_clicks(self):
        page=Page()
        @contextlib.contextmanager
        def browser(data):yield page
        with patch.object(follow,'follow_browser',browser),self.assertRaises(watch.SitePaused):
            follow.follow_once(Path('/unused'),ACCOUNT,FOUND,A,[A],Mock(),Mock(side_effect=watch.SitePaused('login_required')))
        self.assertEqual(page.clicks,0)

class FollowWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir()
        self.patch=patch.multiple(watch,ROOT=self.root,DATA=self.data,STATE=self.data/'state.json');self.patch.start()
        watch.write_json(self.data/'follow-account.json',ACCOUNT)
        self.cfg={'target':A,'targets':[A,B],'interval_seconds':0,'auto_follow_enabled':True,'auto_follow_targets':[A,B]}
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def state(self):return watch.read_json(watch.STATE)
    def outcome(self,t,found=True):
        return {'status':'found' if found else 'not_found','result':dict(FOUND,red_id=t) if found else None,'result_count':1,'checked_at':watch.now()}
    def test_two_targets_follow_independently_and_stop_only_after_result_notifications(self):
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t)) as query,patch.object(follow_jobs,'follow_once',return_value={'status':'followed'}) as driver,patch.object(watch,'webhook') as notify:
            watch.run_check(self.cfg);watch.run_check(self.cfg)
            self.assertEqual(query.call_count,2);self.assertEqual(driver.call_count,2)
            self.assertEqual([c.kwargs['event']['kind'] for c in notify.call_args_list],['found','found','follow_success','follow_success'])
        self.assertTrue(self.state()['completed']);self.assertFalse(self.state()['follow_pending'])
    def test_offline_notifications_do_not_repeat_successful_follow(self):
        cfg=dict(self.cfg,targets=[A],auto_follow_targets=[A])
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t)) as query,patch.object(follow_jobs,'follow_once',return_value={'status':'followed'}) as driver:
            with patch.object(watch,'webhook',side_effect=RuntimeError('offline')):watch.run_check(cfg)
            self.assertFalse(self.state()['completed']);self.assertEqual(self.state()['targets'][A]['follow']['status'],'succeeded')
            with patch.object(watch,'webhook') as notify:watch.run_check(cfg);self.assertEqual(notify.call_count,2)
            driver.assert_called_once();query.assert_called_once()
        self.assertTrue(self.state()['completed'])
    def test_big_login_failure_pauses_following_but_other_search_continues(self):
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t,t==A)) as query,patch.object(follow_jobs,'follow_once',side_effect=watch.SitePaused('login_required')) as driver,patch.object(watch,'webhook') as notify:
            watch.run_check(self.cfg);watch.run_check(self.cfg)
            driver.assert_called_once();self.assertEqual([c.args[1] for c in query.call_args_list],[A,B,B])
            self.assertEqual([c.kwargs['event']['kind'] for c in notify.call_args_list],['found','follow_failure'])
        self.assertTrue(self.state()['follow_paused']);self.assertFalse(self.state().get('paused',False))
    def test_preclick_network_failure_retries_without_searching_found_target_again(self):
        cfg=dict(self.cfg,targets=[A],auto_follow_targets=[A],interval_seconds=1800)
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t)) as query,patch.object(follow_jobs,'follow_once',side_effect=[TimeoutError('network'),{'status':'followed'}]) as driver,patch.object(watch,'webhook') as notify:
            for clock in [10000,10001,10300]:
                with patch.object(watch.time,'time',return_value=clock):watch.run_check(cfg)
            self.assertEqual(driver.call_count,2);query.assert_called_once()
            self.assertEqual([c.kwargs['event']['kind'] for c in notify.call_args_list],['found','follow_failure','follow_success'])
        self.assertTrue(self.state()['completed'])
    def test_crash_after_click_intent_resumes_with_verification_only(self):
        cfg=dict(self.cfg,targets=[A],auto_follow_targets=[A])
        def crash(*args,**kwargs):args[5]();raise SystemExit('crash after click intent')
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t)) as query,patch.object(watch,'webhook'):
            with patch.object(follow_jobs,'follow_once',side_effect=crash),self.assertRaises(SystemExit):watch.run_check(cfg)
            self.assertTrue(self.state()['targets'][A]['follow']['click_started'])
            with patch.object(follow_jobs,'follow_once',return_value={'status':'already_followed'}) as driver:watch.run_check(cfg)
            self.assertTrue(driver.call_args.kwargs['verify_only']);query.assert_called_once()
        self.assertTrue(self.state()['completed'])
    def test_three_preclick_failures_end_with_failure_notice(self):
        cfg=dict(self.cfg,targets=[A],auto_follow_targets=[A])
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t)),patch.object(follow_jobs,'follow_once',side_effect=TimeoutError('network')) as driver,patch.object(watch,'webhook') as notify:
            for clock in [10000,10300,10900,13000]:
                with patch.object(watch.time,'time',return_value=clock):watch.run_check(cfg)
            self.assertEqual(driver.call_count,3)
            failures=[c.kwargs['event'] for c in notify.call_args_list if c.kwargs['event']['kind']=='follow_failure']
            self.assertEqual([e['follow_status'] for e in failures],['retry','failed'])
        self.assertTrue(self.state()['completed']);self.assertEqual(self.state()['targets'][A]['follow']['status'],'failed')
    def test_additional_monitor_target_is_not_implicitly_authorized_for_follow(self):
        cfg=dict(self.cfg,targets=[A,B,'third_account'])
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t)),patch.object(follow_jobs,'follow_once',return_value={'status':'followed'}) as driver,patch.object(watch,'webhook'):
            watch.run_check(cfg);watch.run_check(cfg)
            self.assertEqual([c.args[3] for c in driver.call_args_list],[A,B])
        self.assertNotIn('follow',self.state()['targets']['third_account']);self.assertTrue(self.state()['completed'])
    def test_legacy_found_and_notified_account_gets_follow_task_after_enablement(self):
        watch.write_json(watch.STATE,{'completed':True,'found':FOUND})
        with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:self.outcome(t,False)) as query,patch.object(follow_jobs,'follow_once',return_value={'status':'already_followed'}) as driver,patch.object(watch,'webhook') as notify:
            watch.run_check(self.cfg)
            self.assertEqual(query.call_args.args[1],B);driver.assert_called_once()
            self.assertEqual(notify.call_args.kwargs['event']['kind'],'follow_success')
        self.assertTrue(self.state()['targets'][A]['completed'])

if __name__=='__main__':unittest.main()
