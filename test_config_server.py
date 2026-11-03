import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import config_server as server
from watch_config import signature

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
