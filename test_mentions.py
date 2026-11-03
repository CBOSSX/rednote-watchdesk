import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mentions import mention_text
import watch
import watchdog


class MentionTests(unittest.TestCase):
    def test_optional_and_escaped_deduplicated_mentions(self):
        self.assertEqual(mention_text('body', {}), 'body')
        user = {'user_id': 'ou_example', 'name': 'A & <B>'}
        text = mention_text('body', {'mention_users': [user, user]})
        self.assertEqual(text, '<at user_id="ou_example">A &amp; &lt;B&gt;</at>\nbody')

    def test_invalid_ids_cannot_inject_tags_or_mention_all(self):
        for user_id in ['all', '', 'ou_x"/><at user_id="all', None]:
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                mention_text('body', {'mention_users': [{'user_id': user_id, 'name': 'test'}]})

    def test_main_and_independent_watchdog_send_real_at_tags(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"code":0}'

        class Opener:
            def open(self, req, **kwargs):
                sent.append(json.loads(req.data))
                return Response()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = {'webhook_url': 'https://open.larkoffice.com/open-apis/bot/v2/hook/test',
                      'mention_users': [{'user_id': 'ou_example', 'name': 'test'}]}
            (root/'secrets.json').write_text(json.dumps(secret))
            sent = []
            with patch.object(watch, 'ROOT', root), patch.object(watchdog, 'ROOT', root), patch.object(watchdog, 'SECRETS', root/'secrets.json'), patch('urllib.request.build_opener', return_value=Opener()):
                watch.webhook('account found')
                watchdog.notify('watchdog recovery')
            for payload in sent:
                self.assertEqual(payload['msg_type'], 'interactive')
                self.assertEqual(payload['card']['schema'], '2.0')
                self.assertIn('<at id=ou_example></at>', payload['card']['body']['elements'][-1]['content'])
                self.assertNotIn('user_id=', json.dumps(payload))
            self.assertIn('account found', json.dumps(sent[0]))
            self.assertIn('watchdog recovery', json.dumps(sent[1]))


if __name__ == '__main__':
    unittest.main()
