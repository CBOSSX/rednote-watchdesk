import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from rednote_watchdesk import config_server as server
from rednote_watchdesk.watch_config import signature

class ServerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.patch=patch.object(server,'RUNTIME',self.root);self.patch.start();server.ensure_home()
  server.write(self.root/'secrets.json',{'webhook_url':'https://open.feishu.cn/open-apis/bot/v2/hook/PRIVATE_TEST_VALUE','signing_secret':'PRIVATE_SIGNING_VALUE','mention_users':[]})
  self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.port=self.http.server_port
  threading.Thread(target=self.http.serve_forever,daemon=True).start()
 def tearDown(self):self.http.shutdown();self.http.server_close();self.patch.stop();self.tmp.cleanup()
 def request(self,path,body=None,**headers):
  c=http.client.HTTPConnection('127.0.0.1',self.port)
  h={'Host':'127.0.0.1:'+str(self.port),'Content-Type':'application/json','Origin':'http://127.0.0.1:'+str(self.port),'X-CSRF-Token':server.TOKEN};h.update(headers)
  c.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),headers=h)
  r=c.getresponse();data=r.read().decode();c.close();return r.status,data
 def test_state_never_returns_notification_secrets(self):
  code,data=self.request('/api/state');self.assertEqual(code,200);self.assertNotIn('PRIVATE_TEST_VALUE',data);self.assertNotIn('PRIVATE_SIGNING_VALUE',data)
 def test_page_and_module_assets_are_served_after_directory_move(self):
  code,html=self.request('/');self.assertEqual(code,200)
  self.assertIn('type="module" src="/js/app.js"',html)
  self.assertNotIn('__CSRF_TOKEN__',html)
  for asset in ['/favicon.svg','/js/app.js','/js/motion.js','/js/accounts.js','/css/style.css','/css/motion.css']:
   code,body=self.request(asset);self.assertEqual(code,200,asset);self.assertTrue(body)
  for path in ['/js/../../secrets.json','/rednote_watchdesk/paths.py','/examples/secrets.example.json']:
   self.assertEqual(self.request(path)[0],404)
 def test_create_accounts_and_save_ordered_search_and_multiple_followers(self):
  ids=[]
  for label in ['first','second']:
   code,data=self.request('/api/accounts',{'label':label});self.assertEqual(code,201)
   ids.append(json.loads(data)['account']['id'])
  config={'interval_seconds':1800,'search_account_ids':ids[::-1],'people':[{'id':'person','name':'Person','candidates':['example_person'],'follow_account_ids':ids}]}
  with patch.object(server,'wake'):
   code,_=self.request('/api/config',{'version':signature(server.read(self.root/'config.json')),'config':config})
  self.assertEqual(code,200)
  code,data=self.request('/api/state');result=json.loads(data)
  self.assertEqual(result['config']['search_account_ids'],ids[::-1])
  self.assertEqual(result['config']['people'][0]['follow_account_ids'],ids)
  self.assertTrue(result['config']['people'][0]['auto_follow'])
 def test_missing_actor_and_old_client_cannot_overwrite_multi_account_config(self):
  version=signature(server.read(self.root/'config.json'))
  for invalid in [['missing'], ['missing', 'missing'], 'missing', None]:
   config={'interval_seconds':1800,'search_account_ids':invalid,'people':[]}
   self.assertEqual(self.request('/api/config',{'version':version,'config':config})[0],400)
  config={'interval_seconds':1800,'search_account_ids':[],'people':[]}
  server.write(self.root/'config.json',config)
  for stale in [{'people':[]}, {'people':[], 'search_account_id':None}]:
   self.assertEqual(self.request('/api/config',{'version':signature(config),'config':stale})[0],409)
 def test_login_and_control_jobs_have_account_identity_and_page_scope(self):
  _,data=self.request('/api/accounts',{'label':'one'});aid=json.loads(data)['account']['id']
  with patch.object(server,'start_job',return_value='test-job') as start:
   self.assertEqual(self.request('/api/login',{'account_id':aid})[0],202)
   self.assertEqual(start.call_args.kwargs['account_id'],aid);self.assertEqual(start.call_args.kwargs['scope'],'accounts')
   self.assertEqual(self.request('/api/control',{'action':'test-webhook'})[0],202)
   self.assertEqual(start.call_args.kwargs['scope'],'settings')
   self.assertEqual(self.request('/api/login',{'account_id':'../escape'})[0],400)
 def test_manual_check_requires_saved_targets_and_search_account(self):
  with patch.object(server,'start_job',return_value='manual-job') as start:
   self.assertEqual(self.request('/api/control',{'action':'check-now'})[0],400)
   server.write(self.root/'config.json',{'people':[{'id':'person','name':'Person','candidates':['example_person']}],'search_account_ids':[]})
   self.assertEqual(self.request('/api/control',{'action':'check-now'})[0],400)
   server.write(self.root/'config.json',{'target':'example_person'})
   self.assertEqual(self.request('/api/control',{'action':'check-now'},**{'X-CSRF-Token':''})[0],403)
   code,data=self.request('/api/control',{'action':'check-now'})
   self.assertEqual(code,202);self.assertEqual(json.loads(data)['job_id'],'manual-job')
   start.assert_called_once_with('check-now',account_id=None,scope='people',title='立即查询一次')
 def test_pause_bypasses_running_job_and_worker_lock(self):
  import fcntl
  from rednote_watchdesk import pause_control
  with patch.object(server,'JOBS',{'manual':{'id':'manual','status':'running'}}),patch.object(server,'wake') as wake,patch.object(server,'start_job') as start:
   with (self.root/'data/watch.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    code,data=self.request('/api/control',{'action':'pause'})
   self.assertEqual(code,200);self.assertIn('已暂停后续调度',json.loads(data)['message'])
   self.assertTrue(pause_control.token(self.root/'data'));start.assert_not_called();wake.assert_called_once()
   self.assertTrue(server.payload()['state']['paused'])
 def test_legacy_selection_survives_config_upgrade_without_enrolled_files(self):
  config={'interval_seconds':1800,'people':[{'id':'person','name':'Person','candidates':['example_person'],'auto_follow':True}]}
  server.write(self.root/'config.json',config)
  _,data=self.request('/api/state');payload=json.loads(data)
  with patch.object(server,'wake'):
   self.assertEqual(self.request('/api/config',{'version':payload['version'],'config':payload['config']})[0],200)
  _,data=self.request('/api/state');payload=json.loads(data)
  self.assertEqual({a['id'] for a in payload['accounts']},{'legacy_search','legacy_follow'})
  self.assertEqual(payload['config']['search_account_ids'],['legacy_search'])
  self.assertEqual(payload['config']['people'][0]['follow_account_ids'],['legacy_follow'])
 def test_rename_account_does_not_wake_monitor_or_change_configuration(self):
  _,data=self.request('/api/accounts',{'label':'original'});aid=json.loads(data)['account']['id']
  config=(self.root/'config.json').read_bytes();state=(self.root/'data/state.json').read_bytes()
  body={'account_id':aid,'label':'my account','previous_label':'original'}
  with patch.object(server,'wake') as wake,patch.object(server,'start_job') as job:
   self.assertEqual(self.request('/api/accounts/rename',body,**{'X-CSRF-Token':''})[0],403)
   self.assertEqual(self.request('/api/accounts/rename',body)[0],200)
   self.assertEqual(self.request('/api/accounts/rename',body)[0],409)
   wake.assert_not_called();job.assert_not_called()
  self.assertEqual((self.root/'config.json').read_bytes(),config)
  self.assertEqual((self.root/'data/state.json').read_bytes(),state)
  _,data=self.request('/api/state')
  self.assertEqual(json.loads(data)['accounts'][0]['label'],'my account')
 def test_secret_files_and_path_traversal_not_served(self):
  for path in ['/secrets.json','/data/state.json','/../secrets.json']:
   self.assertEqual(self.request(path)[0],404)
 def test_cross_origin_or_missing_token_cannot_mutate(self):
  for headers in [{'Origin':'https://evil.example'},{'X-CSRF-Token':''},{'Host':'evil.example'}]:
   self.assertEqual(self.request('/api/config',{},**headers)[0],403)
 def test_save_and_conflict_without_losing_stored_configuration(self):
  cfg={'interval_seconds':1200,'people':[{'id':'alice','name':'Alice','candidates':['example_alice','example_other'],'auto_follow':False}]}
  body={'config':cfg,'version':signature(server.read(self.root/'config.json'))}
  with patch.object(server,'wake'):
   self.assertEqual(self.request('/api/config',body)[0],200)
   self.assertEqual(server.read(self.root/'config.json'),cfg)
   self.assertEqual(self.request('/api/config',body)[0],409)
 def test_configuration_lock_rejects_save_during_follow(self):
  import fcntl
  body={'config':{'people':[],'interval_seconds':1800},'version':signature(server.read(self.root/'config.json'))}
  with (self.root/'data/config.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   self.assertEqual(self.request('/api/config',body)[0],409)
 def test_notification_update_preserves_secret_and_requires_valid_mentions(self):
  with patch.object(server,'wake'):
   code,_=self.request('/api/notifications',{'webhook_url':'','mention_users':[{'name':'Alice','user_id':'ou_example'}]})
   self.assertEqual(code,200);self.assertIn('PRIVATE_TEST_VALUE',server.read(self.root/'secrets.json')['webhook_url'])
   code,_=self.request('/api/notifications',{'webhook_url':'https://evil.example','mention_users':[]});self.assertEqual(code,400)
 def test_clear_webhook_preserves_other_notification_settings(self):
  with patch.object(server,'wake'):
   code,_=self.request('/api/notifications/clear',{'fields':['webhook_url']})
  self.assertEqual(code,200)
  saved=server.read(self.root/'secrets.json')
  self.assertNotIn('webhook_url',saved);self.assertEqual(saved['signing_secret'],'PRIVATE_SIGNING_VALUE')
  code,data=self.request('/api/state');state=json.loads(data)
  self.assertFalse(state['notifications']['configured']);self.assertTrue(state['notifications']['signing_configured'])
 def test_clear_signing_preserves_webhook(self):
  with patch.object(server,'wake'):
   code,_=self.request('/api/notifications/clear',{'fields':['signing_secret']})
  self.assertEqual(code,200);saved=server.read(self.root/'secrets.json')
  self.assertNotIn('signing_secret',saved);self.assertIn('PRIVATE_TEST_VALUE',saved['webhook_url'])
 def test_clear_all_removes_only_notification_fields(self):
  saved=server.read(self.root/'secrets.json');saved['other_setting']='keep';saved['mention_users']=[{'name':'Alice','user_id':'ou_example'}]
  server.write(self.root/'secrets.json',saved)
  config_before=(self.root/'config.json').read_bytes();state_before=(self.root/'data/state.json').read_bytes()
  with patch.object(server,'wake'):
   code,_=self.request('/api/notifications/clear',{'fields':['webhook_url','signing_secret','mention_users']})
  self.assertEqual(code,200);self.assertEqual(server.read(self.root/'secrets.json'),{'other_setting':'keep'})
  self.assertEqual((self.root/'config.json').read_bytes(),config_before)
  self.assertEqual((self.root/'data/state.json').read_bytes(),state_before)
  self.assertEqual((self.root/'secrets.json').stat().st_mode & 0o777,0o600)
 def test_clear_requires_valid_scope_and_csrf(self):
  original=(self.root/'secrets.json').read_bytes()
  for fields in [[],['config'],['webhook_url',{}],'webhook_url']:
   self.assertEqual(self.request('/api/notifications/clear',{'fields':fields})[0],400)
  self.assertEqual(self.request('/api/notifications/clear',{'fields':['webhook_url']},**{'X-CSRF-Token':''})[0],403)
  self.assertEqual((self.root/'secrets.json').read_bytes(),original)
 def test_partial_save_after_clear_does_not_resurrect_secret(self):
  with patch.object(server,'wake'):
   self.assertEqual(self.request('/api/notifications/clear',{'fields':['webhook_url']})[0],200)
   self.assertEqual(self.request('/api/notifications',{'mention_users':[{'name':'Alice','user_id':'ou_example'}]})[0],200)
  saved=server.read(self.root/'secrets.json')
  self.assertNotIn('webhook_url',saved);self.assertEqual(saved['mention_users'][0]['name'],'Alice')
