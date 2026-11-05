import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from rednote_watchdesk import accounts, config_server, watch
from rednote_watchdesk.cards import notification_payload
from rednote_watchdesk.query_diagnostics import public_error, safe_detail


class QueryDiagnosticsTests(unittest.TestCase):
    def page(self, reads):
        locator = Mock()
        locator.inner_text.side_effect = reads
        locator.evaluate.return_value = {'ready_state': 'complete', 'body_present': True}
        page = SimpleNamespace(locator=Mock(return_value=locator),
                               goto=Mock(return_value=SimpleNamespace(status=200)),
                               wait_for_timeout=Mock(), screenshot=Mock(),
                               url='https://www.xiaohongshu.com/explore?xsec_token=PRIVATE')
        page.get_by_role = Mock()
        page.get_by_role.return_value.count.return_value = 0
        return page

    def test_delayed_body_recovers_and_still_checks_platform_gate(self):
        for body, reason in [('normal page', None), ('扫码登录', 'login_required'),
                             ('安全验证', 'risk_control'), ('IP存在风险', 'ip_risk')]:
            with self.subTest(body=body):
                page = self.page([TimeoutError('body not ready'), body])
                if reason:
                    with self.assertRaisesRegex(watch.SitePaused, reason):
                        watch.check_gate(page)
                else:
                    self.assertEqual(watch.check_gate(page), body)
                self.assertEqual([c.kwargs['timeout'] for c in page.locator.return_value.inner_text.call_args_list], [5000, 10000])
                page.goto.assert_not_called()

    def test_body_wait_is_bounded_and_other_errors_are_not_retried(self):
        for error, calls in [(TimeoutError('body unavailable'), 2), (RuntimeError('closed'), 1)]:
            with self.subTest(error=error):
                page = self.page(error)
                with self.assertRaises(type(error)):
                    watch.visible_body(page)
                self.assertEqual(page.locator.return_value.inner_text.call_count, calls)

    def test_homepage_error_is_captured_before_browser_closes(self):
        page = self.page(TimeoutError('body timeout'))
        closed = False

        @contextlib.contextmanager
        def browser(*args):
            nonlocal closed
            try:
                yield page
            finally:
                closed = True

        def screenshot(path, **kwargs):
            self.assertFalse(closed)
            Path(path).write_bytes(b'test')

        page.screenshot.side_effect = screenshot
        with tempfile.TemporaryDirectory() as tmp, patch.object(watch, 'DATA', Path(tmp)), \
             patch.object(accounts, 'require_account', return_value={'profile_url': 'expected'}), \
             patch.object(accounts, 'browser', browser):
            with self.assertRaises(TimeoutError) as caught:
                with watch.browser_page(account_id='test'):
                    self.fail('homepage was not ready')
            self.assertTrue(closed)
            detail = watch.record_diagnostic(None, 'example_target', caught.exception)
            self.assertEqual(detail['stage'], 'homepage_access')
            self.assertEqual(detail['code'], 'homepage_access_timeout')
            self.assertEqual(detail['body_attempts'], 2)
            self.assertEqual(detail['http_status'], 200)
            self.assertEqual(detail['page_state']['body_present'], True)
            self.assertEqual(detail['screenshot'], 'last-error-example_target.png')
            self.assertEqual((Path(tmp)/detail['screenshot']).read_bytes(), b'test')
            self.assertNotIn('PRIVATE', json.dumps(detail))

    def test_http_failure_cannot_be_treated_as_an_empty_search(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                error_type = watch.SitePaused if status in (401, 403, 429) else RuntimeError
                with self.assertRaises(error_type):
                    watch.check_response(SimpleNamespace(status=status))

    def test_network_and_timeout_details_are_safe_for_ui_and_cards(self):
        detail = {'stage': 'navigate_search', 'type': 'Error', 'detail':
                  'net::ERR_CONNECTION_RESET https://www.xiaohongshu.com/search?xsec_token=PRIVATE /Users/test/private/profile'}
        self.assertEqual(public_error(detail)['code'], 'network_error')
        self.assertNotIn('PRIVATE', safe_detail(detail['detail']))
        self.assertNotIn('/Users/test', safe_detail(detail['detail']))
        detail.update(stage='homepage_access', type='TimeoutError')
        card = notification_payload('', {}, {'kind': 'query_fault', 'reason': 'TimeoutError',
            'diagnostic': detail, 'next_retry_epoch': 1793868481})
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn('读取首页正文超时', text)
        self.assertIn('下次重试', text)
        self.assertNotIn('PRIVATE', text)

    def test_old_body_timeout_does_not_claim_browser_launch_failed(self):
        detail = {'stage': 'launch_browser', 'type': 'TimeoutError',
                  'detail': 'Locator.inner_text: Timeout 5000ms exceeded, waiting for locator("body")'}
        error = public_error(detail)
        self.assertEqual(error['stage'], 'read_page_body')
        self.assertEqual(error['message'], '读取页面正文超时')

    def test_state_exposes_query_errors_without_raw_messages_and_clears_on_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config_server, 'RUNTIME', Path(tmp)):
            config_server.ensure_home()
            config_server.write(Path(tmp)/'config.json', {'target': 'example_target', 'interval_seconds': 1200})
            error = {'stage': 'check_access', 'type': 'TimeoutError', 'detail': 'PRIVATE'}
            state = {'targets': {'example_target': {'last_error': 'TimeoutError', 'last_error_detail': error,
                     'consecutive_errors': 3, 'next_retry_epoch': 1793868481}}}
            config_server.write(Path(tmp)/'data/state.json', state)
            payload = config_server.payload()
            errors = payload['state']['people'][0]['query_errors']
            self.assertEqual(errors[0]['message'], '读取搜索页面正文超时')
            self.assertEqual(errors[0]['consecutive_errors'], 3)
            self.assertNotIn('PRIVATE', json.dumps(payload))
            state['targets']['example_target'] = {'last_status': 'not_found'}
            config_server.write(Path(tmp)/'data/state.json', state)
            self.assertEqual(config_server.payload()['state']['people'][0]['query_errors'], [])
