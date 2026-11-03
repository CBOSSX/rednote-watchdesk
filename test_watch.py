import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import watch


class MatchTests(unittest.TestCase):
    def test_observed_server_ip_restriction_is_not_a_negative_search(self):
        class Page:
            def locator(self, _): return self
            def inner_text(self, **kwargs):
                return "安全限制\nIP存在风险，请切换可靠网络环境后重试\n300012"
        with self.assertRaisesRegex(watch.SitePaused, "ip_risk"):
            watch.check_gate(Page())

    def test_exact_id_only_and_official_profile(self):
        href = "https://www.xiaohongshu.com/user/profile/000000000000000000000001?xsec_token=example"
        rows = [{"text": "example_alpha\n小红书号：other", "href": href},
                {"text": "someone\n小红书号：example_alpha2", "href": href}]
        self.assertIsNone(watch.exact_result(rows, "example_alpha"))
        rows.append({"text": "Someone\n小红书号：example_alpha\n粉丝・1", "href": href})
        result = watch.exact_result(rows, "example_alpha")
        self.assertEqual(result["name"], "Someone")
        self.assertNotIn("xsec_token", result["url"])
        self.assertIsNone(watch.parse_result(rows[-1]["text"], "https://evil.example/user/profile/000000000000000000000001"))
        self.assertIsNone(watch.parse_result(rows[-1]["text"], "javascript:alert(1)"))


    def test_login_or_risk_shown_during_timeout_takes_priority(self):
        for reason in ('login_required','risk_control','ip_risk'):
            with self.subTest(reason=reason),patch.object(watch,'search_users',side_effect=TimeoutError('click timed out')),patch.object(watch,'check_gate',side_effect=watch.SitePaused(reason)):
                with self.assertRaisesRegex(watch.SitePaused,reason):watch.search(object(),'example_alpha')

    def test_network_error_page_does_not_pause_as_risk(self):
        from unittest.mock import Mock
        page=Mock();page.url='chrome-error://chromewebdata/'
        page.locator.return_value.inner_text.return_value='无法访问此网站'
        page.get_by_role.return_value.count.return_value=0
        with self.assertRaisesRegex(RuntimeError,'unexpected_redirect'):watch.check_gate(page)
        with patch.object(watch,'search_users',side_effect=TimeoutError('navigation timed out')):
            with self.assertRaisesRegex(TimeoutError,'navigation timed out'):watch.search(page,'example_alpha')

    def test_unavailable_gate_does_not_mask_original_network_timeout(self):
        with patch.object(watch,'search_users',side_effect=TimeoutError('original timeout')),patch.object(watch,'check_gate',side_effect=RuntimeError('page unavailable')):
            with self.assertRaisesRegex(TimeoutError,'original timeout'):watch.search(object(),'example_alpha')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.patcher = patch.multiple(watch, ROOT=self.root, DATA=self.data, STATE=self.data / "state.json")
        self.patcher.start()
        self.config = {"target": "example_alpha", "interval_seconds": 0}

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    @staticmethod
    @contextlib.contextmanager
    def browser():
        class Page:
            def screenshot(self, **kwargs):
                pass
        yield Page()

    def test_found_delivery_retries_without_search_then_stops(self):
        outcome = {"status": "found", "result": {"name": "test", "red_id": "example_alpha", "url": "https://www.xiaohongshu.com/user/profile/000000000000000000000001"}, "result_count": 1, "checked_at": watch.now()}
        with patch.object(watch, "browser_page", self.browser), patch.object(watch, "search", return_value=outcome) as search:
            with patch.object(watch, "webhook", side_effect=RuntimeError("test transport failure")):
                watch.run_check(self.config)
            state = watch.read_json(watch.STATE)
            self.assertFalse(state.get("completed", False))
            self.assertEqual(state["pending"]["kind"], "found")
            with patch.object(watch, "webhook") as notify:
                watch.run_check(self.config)
                watch.run_check(self.config)
                notify.assert_called_once()
            search.assert_called_once()
        self.assertTrue(watch.read_json(watch.STATE)["completed"])

    def test_login_failure_pauses_and_notifies_once(self):
        with patch.object(watch, "browser_page", self.browser), patch.object(watch, "search", side_effect=watch.SitePaused("login_required")) as search, patch.object(watch, "webhook") as notify:
            self.assertEqual(watch.run_check(self.config), 2)
            watch.run_check(self.config)
            search.assert_called_once()
            notify.assert_called_once()
        self.assertTrue(watch.read_json(watch.STATE)["paused"])

    def test_negative_result_is_silent_and_dry_run_preserves_state(self):
        outcome = {"status": "not_found", "result": None, "result_count": 15, "checked_at": watch.now()}
        with patch.object(watch, "browser_page", self.browser), patch.object(watch, "search", return_value=outcome), patch.object(watch, "webhook") as notify:
            watch.run_check(self.config)
            before = watch.STATE.read_bytes()
            with patch("builtins.print"):
                watch.run_check(self.config, dry_run=True, target="example_gamma")
            self.assertEqual(before, watch.STATE.read_bytes())
            notify.assert_not_called()

    def test_webhook_requires_application_success(self):
        watch.write_json(self.root / "secrets.json", {"webhook_url": "https://open.larkoffice.com/open-apis/bot/v2/hook/test"})
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"code":19024,"msg":"not allowed"}'
        class Opener:
            def open(self, *args, **kwargs): return Response()
        with patch("urllib.request.build_opener", return_value=Opener()):
            with self.assertRaisesRegex(RuntimeError, "19024"):
                watch.webhook("test")


if __name__ == "__main__":
    unittest.main()
