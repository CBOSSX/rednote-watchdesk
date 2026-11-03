import json
import unittest
from rednote_watchdesk.cards import notification_payload


class CardTests(unittest.TestCase):
    def test_legacy_queued_found_keeps_profile_action_and_escapes_nickname(self):
        url = 'https://www.xiaohongshu.com/user/profile/000000000000000000000001'
        text = '【小红书账号监测】发现小红书号精确匹配：example_alpha\n昵称：<at id=all></at> [fake](https://evil.example)\n主页：' + url
        card = notification_payload(text, {}, {'kind': 'found', 'target': 'example_alpha'})['card']
        encoded = json.dumps(card, ensure_ascii=False)
        self.assertNotIn('<at id=all>', encoded)
        self.assertIn('&lt;at', encoded)
        button = card['body']['elements'][2]
        self.assertEqual(button['behaviors'], [{'type': 'open_url', 'default_url': url}])

    def test_untrusted_profile_url_cannot_become_button(self):
        card = notification_payload('found', {}, {'kind': 'found', 'result': {'url': 'https://evil.example'}})['card']
        self.assertFalse(any(e['tag'] == 'button' for e in card['body']['elements']))

    def test_event_time_and_watchdog_previous_failure_survive_delayed_delivery(self):
        card = notification_payload('recovery', {}, {'kind': 'recovery', 'created_at': '2026-11-02T03:00:00+00:00',
            'targets': ['example_alpha', 'example_beta'], 'previous_reason': 'worker timed out'}, source='watchdog')['card']
        encoded = json.dumps(card, ensure_ascii=False)
        self.assertIn('worker timed out', encoded)
        self.assertIn('2026-11-02 11:00:00', encoded)
        self.assertIn('example_beta', encoded)
        self.assertEqual(card['header']['title']['content'], '小红书监测 · 运行已恢复')

    def test_all_event_types_have_consistent_grouped_layout_and_true_mentions(self):
        secret = {'mention_users': [{'user_id': 'ou_example', 'name': 'test'}]}
        for kind, color in [('found','green'),('attention','orange'),('query_fault','orange'),
                            ('query_recovery','green'),('recovery','green'),('fault','red'),('test','blue')]:
            with self.subTest(kind=kind):
                card = notification_payload('details', secret, {'kind': kind, 'targets': ['example_beta']})['card']
                self.assertEqual(card['header']['template'], color)
                elements = card['body']['elements']
                self.assertTrue(2 <= len(elements) <= 5)
                self.assertEqual(elements[1]['tag'], 'column_set')
                self.assertIn('<at id=ou_example></at>', elements[-1]['content'])
                self.assertNotIn('callback', json.dumps(card))
    def test_follow_result_cards_have_actor_outcome_and_profile_action(self):
        url='https://www.xiaohongshu.com/user/profile/000000000000000000000001'
        for kind,extra,expected in [('follow_success',{'already_followed':True},'本次未重复点击'),
                                     ('follow_failure',{'follow_status':'blocked','reason':'login_required'},'自动关注已暂停'),
                                     ('follow_failure',{'follow_status':'retry','click_started':True,'reason':'TimeoutError'},'不再重复点击')]:
            with self.subTest(kind=kind,extra=extra):
                event=dict(kind=kind,target='example_alpha',actor_name='big account',result={'url':url},**extra)
                card=notification_payload('result',{},event)['card']
                text=json.dumps(card,ensure_ascii=False)
                self.assertIn(expected,text);self.assertIn('big account',text)
                self.assertTrue(any(e['tag']=='button' for e in card['body']['elements']))


if __name__ == '__main__':
    unittest.main()
