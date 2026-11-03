import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import watch
import follow_jobs
from watch_config import normalize, signature
from test_watch import WorkflowTests

A,B,C='example_first','example_second','example_other'
URL='https://www.xiaohongshu.com/user/profile/000000000000000000000001'

def person(pid,candidates,auto=False):return {'id':pid,'name':pid,'candidates':candidates,'auto_follow':auto}
def outcome(t,found):return {'status':'found' if found else 'not_found','result':{'red_id':t,'name':t,'url':URL} if found else None,'result_count':1,'checked_at':watch.now()}

class PeopleTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir()
  self.p=patch.multiple(watch,ROOT=self.root,DATA=self.data,STATE=self.data/'state.json');self.p.start()
  self.cfg={'interval_seconds':0,'people':[person('alice',[A,B]),person('bob',[C])]}
 def tearDown(self):self.p.stop();self.tmp.cleanup()
 def state(self):return watch.read_json(watch.STATE)
 def test_first_hit_skips_remaining_candidate_in_same_batch(self):
  with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t,t==A)) as query,patch.object(watch,'webhook'):
   watch.run_check(self.cfg);watch.run_check(self.cfg)
   self.assertEqual([c.args[1] for c in query.call_args_list],[A,C,C])
  self.assertEqual(self.state()['people']['alice']['matched_target'],A)
  self.assertEqual(self.state()['targets'][B]['suppressed_by'],A)
 def test_second_candidate_hit_and_restart_do_not_search_first_again(self):
  cfg=dict(self.cfg,people=[person('alice',[A,B])])
  with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t,t==B)) as query,patch.object(watch,'webhook'):
   watch.run_check(cfg);watch.run_check(cfg)
   self.assertEqual([c.args[1] for c in query.call_args_list],[A,B])
  self.assertTrue(self.state()['completed'])
 def test_follow_is_per_person_and_only_winning_candidate(self):
  cfg=dict(self.cfg,people=[person('alice',[A,B],True),person('bob',[C],False)])
  watch.write_json(self.data/'follow-account.json',{'name':'big','profile_url':URL})
  with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t,True)) as query,patch.object(follow_jobs,'follow_once',return_value={'status':'followed'}) as driver,patch.object(watch,'webhook') as notify:
   watch.run_check(cfg)
   self.assertEqual([c.args[1] for c in query.call_args_list],[A,C])
   driver.assert_called_once();self.assertEqual(driver.call_args.args[3],A)
   self.assertEqual([c.kwargs['event']['kind'] for c in notify.call_args_list],['found','found','follow_success'])
   self.assertEqual(notify.call_args.kwargs['event']['person_name'],'alice')
 def test_old_completed_state_migrates_without_new_queries_or_follows(self):
  legacy={'target':A,'targets':[A,C],'auto_follow_enabled':True,'auto_follow_targets':[A,C]}
  state=watch.migrate_state({},legacy)
  for t in [A,C]:state['targets'][t].update(found=outcome(t,True)['result'],completed=True,discovery_notified=True,follow={'status':'succeeded','result_notified':True,'click_started':True})
  cfg=dict(self.cfg,people=[person('alice',[A,B],True),person('bob',[C],True)])
  watch.write_json(watch.STATE,state)
  with patch.object(watch,'browser_page') as browser,patch.object(follow_jobs,'follow_once') as driver,patch.object(watch,'webhook') as notify:
   watch.run_check(cfg);browser.assert_not_called();driver.assert_not_called();notify.assert_not_called()
  self.assertTrue(self.state()['completed'])
 def test_add_person_reopens_completed_monitor(self):
  cfg=dict(self.cfg,people=[person('alice',[A,B])])
  with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t,t==A)) as query,patch.object(watch,'webhook'):
   watch.run_check(cfg);self.assertTrue(self.state()['completed'])
   watch.run_check(self.cfg);self.assertEqual(query.call_args.args[1],C)
  self.assertFalse(self.state()['completed'])
 def test_duplicate_casefold_candidate_and_non_boolean_follow_rejected(self):
  for people in [[person('alice',[A,A.upper()])],[person('alice',[A]),person('bob',[A])],[person('alice',[A],'false')]]:
   with self.subTest(people=people),self.assertRaises(ValueError):normalize({'people':people})
 def test_notification_retry_does_not_resume_losing_candidates(self):
  cfg=dict(self.cfg,people=[person('alice',[A,B])])
  with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=lambda p,t:outcome(t,True)) as query:
   with patch.object(watch,'webhook',side_effect=RuntimeError('offline')):watch.run_check(cfg)
   with patch.object(watch,'webhook'):watch.run_check(cfg)
   query.assert_called_once()
  self.assertTrue(self.state()['completed'])
 def test_config_changed_during_search_prevents_old_follow_and_next_query(self):
  cfg=dict(self.cfg,people=[person('alice',[A,B],True),person('bob',[C])])
  watch.write_json(self.root/'config.json',cfg)
  def search(p,t):
   changed=dict(cfg,people=[person('alice',[A,B],False),person('bob',[C])])
   watch.write_json(self.root/'config.json',changed)
   return outcome(t,True)
  with patch.object(watch,'browser_page',WorkflowTests.browser),patch.object(watch,'search',side_effect=search) as query,patch.object(follow_jobs,'follow_once') as driver,patch.object(watch,'webhook'):
   watch.run_check(cfg);query.assert_called_once();driver.assert_not_called()
 def test_removing_matched_candidate_requires_new_person_task(self):
  state=watch.migrate_state({},self.cfg);state['targets'][A]['found']=outcome(A,True)['result'];watch.sync_completion(state)
  with self.assertRaisesRegex(ValueError,'已命中'):watch.migrate_state(state,dict(self.cfg,people=[person('alice',[B])]))

 def test_empty_people_list_is_idle_and_complete(self):
  with patch.object(watch,'browser_page') as browser:
   watch.run_check({'people':[],'interval_seconds':1800});browser.assert_not_called()
  self.assertTrue(self.state()['completed'])
