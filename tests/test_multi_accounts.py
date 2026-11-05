import contextlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from rednote_watchdesk import accounts, follow_jobs, watch
from rednote_watchdesk.follow import FollowProblem
from rednote_watchdesk.follow_state import multi_follow_due, resume_account
from rednote_watchdesk.watch_config import normalize

TARGET = 'example_target'
FOUND = {'red_id': TARGET, 'name': 'Target', 'url': 'https://www.xiaohongshu.com/user/profile/' + '0'*24}


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root/'data'
        self.data.mkdir()
        self.patch = patch.multiple(watch, ROOT=self.root, DATA=self.data, STATE=self.data/'state.json')
        self.patch.start()
        self.actors = [self.actor('one', '1'), self.actor('two', '2')]
        self.config = {'interval_seconds': 0, 'search_account_id': self.actors[0], 'people': [
            {'id': 'target', 'name': 'Target', 'candidates': [TARGET], 'follow_account_ids': self.actors}]}
        self.browser = patch.object(watch, 'browser_page', side_effect=self.page).start()
        self.query = patch.object(watch, 'search', return_value={'status': 'found', 'result': FOUND, 'result_count': 1, 'checked_at': watch.now()}).start()

    @staticmethod
    @contextlib.contextmanager
    def page(**kwargs):
        yield object()

    def actor(self, name, digit):
        aid = accounts.create_account(self.data, name)['id']
        accounts.write(accounts.account_paths(self.data, aid)[0], {'name': name, 'red_id': name, 'profile_url': 'https://www.xiaohongshu.com/user/profile/' + digit*24})
        return aid

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def state(self):
        return watch.read_json(watch.STATE)

    def test_selected_search_and_two_followers_are_independent(self):
        with patch.object(follow_jobs, 'follow_once', return_value={'status': 'followed'}) as driver, patch.object(watch, 'webhook') as notify:
            watch.run_check(self.config)
            watch.run_check(self.config)
        self.browser.assert_called_once_with(account_id=self.actors[0])
        self.assertEqual([call.kwargs['account_id'] for call in driver.call_args_list], self.actors)
        self.assertEqual([c.kwargs['event']['account_id'] for c in notify.call_args_list if c.kwargs['event']['kind'] == 'follow_success'], self.actors)
        self.assertTrue(self.state()['completed'])
        self.query.assert_called_once()

    def test_pause_after_one_follow_does_not_start_next_actor(self):
        from rednote_watchdesk import pause_control
        def follow(*args, **kwargs):
            pause_control.request_pause(self.data)
            return {'status': 'followed'}
        with patch.object(follow_jobs, 'follow_once', side_effect=follow) as driver, patch.object(watch, 'webhook'):
            watch.run_check(self.config)
        driver.assert_called_once()
        self.assertTrue(self.state()['paused'])
        self.assertEqual(self.state()['targets'][TARGET]['follows'][self.actors[1]]['status'], 'pending')

    def test_search_switches_serially_and_keeps_the_current_target_and_previous_match(self):
        self.config['search_account_ids'] = self.actors
        self.config['people'][0]['follow_account_ids'] = []
        self.config['people'].append({'id': 'other', 'name': 'Other', 'candidates': ['example_other', 'example_last']})
        calls = []
        open_browsers = set()

        @contextlib.contextmanager
        def browser(account_id):
            self.assertFalse(open_browsers)
            open_browsers.add(account_id)
            try:
                yield account_id
            finally:
                open_browsers.remove(account_id)

        def query(account_id, target):
            self.assertEqual(open_browsers, {account_id})
            calls.append((account_id, target))
            if target == TARGET:
                return {'status': 'found', 'result': FOUND, 'result_count': 1, 'checked_at': watch.now()}
            if account_id == self.actors[0]:
                raise watch.SitePaused('login_required')
            return {'status': 'not_found', 'result': None, 'result_count': 0, 'checked_at': watch.now()}

        first, backup = self.actors
        self.browser.side_effect = browser
        self.query.side_effect = query
        with patch.object(watch, 'webhook') as notify:
            self.assertEqual(watch.run_check(self.config), 0)
            self.assertEqual(calls, [(first, TARGET), (first, 'example_other'),
                                     (backup, 'example_other'), (backup, 'example_last')])
            self.assertEqual(self.state()['search']['active_account_id'], backup)
            self.assertEqual(self.state()['search']['errors'], {first: 'login_required'})
            self.assertEqual(self.state()['search']['error_details'][first]['code'], 'login_required')
            self.assertNotIn(backup, self.state()['search']['error_details'])
            self.assertEqual(self.state()['targets']['example_other']['consecutive_errors'], 0)
            self.assertEqual(watch.run_check(self.config), 0)
            self.assertEqual(calls[4:], [(backup, 'example_other'), (backup, 'example_last')])
            self.assertEqual([call.kwargs['event']['kind'] for call in notify.call_args_list], ['found'])

    def test_backup_handles_browser_startup_failure(self):
        self.config['search_account_ids'] = self.actors
        self.config['people'][0]['follow_account_ids'] = []
        self.browser.side_effect = [BlockingIOError('profile busy'), self.page()]
        with patch.object(watch, 'webhook'):
            self.assertEqual(watch.run_check(self.config), 0)
        self.assertEqual(self.state()['search']['active_account_id'], self.actors[1])

    def test_exhausted_logins_pause_once_but_transient_errors_retry(self):
        self.config['search_account_ids'] = self.actors
        self.query.side_effect = [TimeoutError('network'), watch.SitePaused('login_required')]
        with patch.object(watch, 'webhook') as notify:
            self.assertEqual(watch.run_check(self.config), 2)
            self.assertFalse(self.state().get('paused'))
            self.assertIn('next_retry_epoch', self.state()['targets'][TARGET])
            notify.assert_not_called()
            self.query.side_effect = watch.SitePaused('login_required')
            with patch.object(watch.time, 'time', return_value=10**12):
                self.assertEqual(watch.run_check(self.config), 2)
                self.assertEqual(watch.run_check(self.config), 0)
            self.assertEqual(self.state()['pause_reason'], 'search_accounts_unavailable')
            notify.assert_called_once()

    def test_platform_gate_pauses_before_opening_backup(self):
        self.config['search_account_ids'] = self.actors
        for reason in ('ip_risk', 'risk_control', 'http_403', 'http_429'):
            with self.subTest(reason=reason), patch.object(watch, 'webhook'):
                watch.STATE.unlink(missing_ok=True)
                self.browser.reset_mock()
                self.query.side_effect = watch.SitePaused(reason)
                self.assertEqual(watch.run_check(self.config), 2)
                self.assertEqual(self.state()['pause_reason'], reason)
                self.browser.assert_called_once_with(account_id=self.actors[0])

    def test_one_blocked_actor_does_not_pause_the_other(self):
        with patch.object(follow_jobs, 'follow_once', side_effect=[FollowProblem('login_required', blocked=True), {'status': 'followed'}]) as driver, patch.object(watch, 'webhook'):
            watch.run_check(self.config)
        state = self.state()
        self.assertEqual(driver.call_count, 2)
        self.assertEqual(state['follow_paused_accounts'], {self.actors[0]: 'login_required'})
        self.assertEqual(state['targets'][TARGET]['follows'][self.actors[1]]['status'], 'succeeded')
        resume_account(state, self.actors[0])
        self.assertTrue(multi_follow_due(state, 10**12))
        self.assertEqual(state['targets'][TARGET]['follows'][self.actors[1]]['status'], 'succeeded')

    def test_crash_after_click_only_verifies_that_actor_on_restart(self):
        def crash(*args, **kwargs):
            args[5]()
            raise SystemExit('interrupted')
        with patch.object(watch, 'webhook'):
            with patch.object(follow_jobs, 'follow_once', side_effect=crash), self.assertRaises(SystemExit):
                watch.run_check(self.config)
            with patch.object(follow_jobs, 'follow_once', return_value={'status': 'already_followed'}) as driver:
                watch.run_check(self.config)
        self.assertTrue(driver.call_args_list[0].kwargs['verify_only'])
        self.assertFalse(driver.call_args_list[1].kwargs['verify_only'])
        self.assertTrue(self.state()['completed'])

    def test_failed_delivery_does_not_repeat_either_actor(self):
        with patch.object(follow_jobs, 'follow_once', return_value={'status': 'followed'}) as driver:
            with patch.object(watch, 'webhook', side_effect=RuntimeError('offline')):
                watch.run_check(self.config)
            self.assertEqual(driver.call_count, 2)
            with patch.object(watch, 'webhook'):
                watch.run_check(self.config)
            self.assertEqual(driver.call_count, 2)
        self.assertTrue(self.state()['completed'])

    def test_more_than_two_actors_continue_on_next_tick_without_repeating(self):
        third = self.actor('three', '3')
        self.config['people'][0]['follow_account_ids'] = self.actors + [third]
        with patch.object(follow_jobs, 'follow_once', return_value={'status': 'followed'}) as driver, patch.object(watch, 'webhook'):
            watch.run_check(self.config)
            self.assertEqual(driver.call_count, 2)
            self.assertTrue(multi_follow_due(self.state(), 10**12))
            watch.run_check(self.config)
            self.assertEqual(driver.call_count, 3)
        self.assertTrue(self.state()['completed'])

    def test_old_success_and_unknown_click_are_migrated_without_a_new_click(self):
        accounts.write(self.data/'follow-account.json', {'name': 'legacy', 'profile_url': 'https://www.xiaohongshu.com/user/profile/' + '4'*24})
        for status, notified in [('succeeded', True), ('running', False)]:
            with self.subTest(status=status):
                watch.write_json(watch.STATE, {'targets': {TARGET: {'found': FOUND, 'completed': True, 'discovery_notified': True,
                    'follow': {'status': status, 'result_notified': notified, 'click_started': True}}}})
                config = dict(self.config, people=[dict(self.config['people'][0], follow_account_ids=['legacy_follow'])])
                with patch.object(follow_jobs, 'follow_once', return_value={'status': 'already_followed'}) as driver, patch.object(watch, 'webhook'):
                    watch.run_check(config)
                if notified:
                    driver.assert_not_called()
                else:
                    self.assertTrue(driver.call_args.kwargs['verify_only'])
                self.assertTrue(self.state()['targets'][TARGET]['follows']['legacy_follow']['click_started'])

    def test_deselect_and_reselect_preserves_completed_jobs(self):
        with patch.object(follow_jobs, 'follow_once', return_value={'status': 'followed'}) as driver, patch.object(watch, 'webhook'):
            watch.run_check(self.config)
            config = dict(self.config, people=[dict(self.config['people'][0], follow_account_ids=[])])
            watch.run_check(config)
            watch.run_check(self.config)
            self.assertEqual(driver.call_count, 2)

    def test_changed_enrolled_identity_is_blocked_before_browser_write(self):
        state = watch.migrate_state({'targets': {TARGET: {'found': FOUND, 'discovery_notified': True}}}, self.config)
        state['targets'][TARGET]['follows'][self.actors[0]]['actor_profile_url'] = 'different'
        watch.write_json(watch.STATE, state)
        with patch.object(follow_jobs, 'follow_once', return_value={'status': 'followed'}) as driver, patch.object(watch, 'webhook'):
            watch.run_check(self.config)
        self.assertEqual(driver.call_count, 1)
        self.assertEqual(driver.call_args.kwargs['account_id'], self.actors[1])
        self.assertEqual(self.state()['follow_paused_accounts'][self.actors[0]], 'follow_account_mismatch')

    def test_invalid_selection_cannot_silently_enable_extra_actors(self):
        for ids in [[self.actors[0], self.actors[0]], 'all', [None]]:
            config = dict(self.config, people=[dict(self.config['people'][0], follow_account_ids=ids)])
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                normalize(config)
        config = normalize(dict(self.config, people=[dict(self.config['people'][0], follow_account_ids=['missing'])]))
        with self.assertRaises(ValueError):
            accounts.validate_selection(config, self.data)

    def test_legacy_notification_for_removed_target_keeps_its_actor_receipt(self):
        state = {'targets': {TARGET: {'found': FOUND, 'follow': {'status': 'succeeded', 'click_started': True}}},
                 'pending': {'kind': 'follow_success', 'target': TARGET, 'follow_status': 'succeeded', 'text': 'done'}}
        config = dict(self.config, people=[])
        watch.write_json(watch.STATE, state)
        with patch.object(watch, 'webhook'), patch.object(follow_jobs, 'follow_once') as driver:
            watch.run_check(config)
        driver.assert_not_called()
        self.assertTrue(self.state()['targets'][TARGET]['follows']['legacy_follow']['result_notified'])

    def test_retry_deadlines_are_independent_and_success_is_not_replayed(self):
        def result(*args, **kwargs):
            if kwargs['account_id'] == self.actors[0] and not attempts:
                attempts.append(True)
                raise TimeoutError('before click')
            return {'status': 'followed'}
        attempts = []
        with patch.object(follow_jobs, 'follow_once', side_effect=result) as driver, patch.object(watch, 'webhook'):
            for clock in [10000, 10001, 10300]:
                with patch.object(watch.time, 'time', return_value=clock):
                    watch.run_check(self.config)
            self.assertEqual([c.kwargs['account_id'] for c in driver.call_args_list], [self.actors[0], self.actors[1], self.actors[0]])
        self.assertTrue(self.state()['completed'])
