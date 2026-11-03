from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rednote_watchdesk import accounts, follow


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)/'data'
        self.data.mkdir()
    def tearDown(self):
        self.tmp.cleanup()
    def test_legacy_profiles_are_reused_without_copying_credentials(self):
        accounts.write(self.data/'search-account.json', {'name': 'search', 'profile_url': 'old-search', 'cookie': 'not-public'})
        accounts.write(self.data/'follow-account.json', {'name': 'follow', 'profile_url': 'old-follow'})
        self.assertEqual({a['id'] for a in accounts.list_accounts(self.data)}, {'legacy_search', 'legacy_follow'})
        self.assertEqual(accounts.account_paths(self.data, 'legacy_search')[1], self.data/'browser-profile')
        self.assertEqual(accounts.account_paths(self.data, 'legacy_follow')[1], self.data/'follow-browser-profile')
        self.assertNotIn('cookie', str(accounts.list_accounts(self.data)))
        self.assertNotIn('profile_url', str(accounts.list_accounts(self.data)))
    def test_new_accounts_use_unique_profiles_and_reject_paths(self):
        first = accounts.create_account(self.data, 'first')['id']
        second = accounts.create_account(self.data, 'second')['id']
        self.assertNotEqual(accounts.account_paths(self.data, first)[1], accounts.account_paths(self.data, second)[1])
        for value in ['../outside', '/tmp/escape', '', None]:
            with self.assertRaises(ValueError): accounts.account_paths(self.data, value)
        self.assertEqual((self.data/'accounts.json').stat().st_mode & 0o777, 0o600)
    def test_relogin_preserves_expected_identity_even_when_clearing_cookies(self):
        aid = accounts.create_account(self.data, 'one')['id']
        identity = {'name': 'one', 'profile_url': 'expected'}
        accounts.write(accounts.account_paths(self.data, aid)[0], identity)
        with patch.object(follow, 'login_account', return_value=identity) as login:
            accounts.enroll(self.data, aid, 10, lambda page: None, lambda: 'now', fresh=True)
        self.assertEqual(login.call_args.args[1]['profile_url'], 'expected')
        self.assertTrue(login.call_args.kwargs['fresh'])
        self.assertEqual(login.call_args.kwargs['account_id'], aid)
    def test_duplicate_identity_is_not_assigned_to_a_second_actor(self):
        first = accounts.create_account(self.data, 'one')['id']
        second = accounts.create_account(self.data, 'two')['id']
        identity = {'name': 'one', 'profile_url': 'same'}
        accounts.write(accounts.account_paths(self.data, first)[0], identity)
        with patch.object(follow, 'login_account', return_value=identity), self.assertRaisesRegex(follow.FollowProblem, 'already_enrolled'):
            accounts.enroll(self.data, second, 10, lambda page: None, lambda: 'now')
        self.assertFalse(accounts.account_paths(self.data, second)[0].exists())

    def test_rename_legacy_account_preserves_identity_profile_and_tasks(self):
        identity, profile, _ = accounts.account_paths(self.data, 'legacy_search')
        accounts.write(identity, {'name': 'site nickname', 'profile_url': 'same-identity'})
        profile.mkdir(); (profile/'Cookies').write_bytes(b'private-test-placeholder')
        accounts.write(self.data.parent/'config.json', {'people': [], 'search_account_id': 'legacy_search'})
        accounts.write(self.data/'state.json', {'follow_paused': True, 'receipt': 'keep'})
        protected = [identity, profile/'Cookies', self.data.parent/'config.json', self.data/'state.json']
        before = {p: p.read_bytes() for p in protected}
        accounts.rename_account(self.data, 'legacy_search', '  我的日常账号  ', '原搜索账号')
        listed = accounts.list_accounts(self.data)
        self.assertEqual(listed[0]['id'], 'legacy_search')
        self.assertEqual(listed[0]['label'], '我的日常账号')
        self.assertEqual(listed[0]['name'], 'site nickname')
        self.assertEqual({p: p.read_bytes() for p in protected}, before)
        self.assertEqual((self.data/'accounts.json').stat().st_mode & 0o777, 0o600)

    def test_invalid_and_stale_renames_leave_registry_unchanged(self):
        aid = accounts.create_account(self.data, 'original')['id']
        path = self.data/'accounts.json'; before = path.read_bytes()
        for label in ['', '   ', 'x'*41, None, 42]:
            with self.subTest(label=label), self.assertRaises(ValueError):
                accounts.rename_account(self.data, aid, label, 'original')
        with self.assertRaises(ValueError):
            accounts.rename_account(self.data, 'missing', 'new', 'original')
        with self.assertRaises(ValueError):
            accounts.rename_account(self.data, '../outside', 'new', 'original')
        self.assertEqual(path.read_bytes(), before)
        accounts.rename_account(self.data, aid, 'first edit', 'original')
        with self.assertRaises(RuntimeError):
            accounts.rename_account(self.data, aid, 'stale edit', 'original')
        self.assertEqual(accounts.list_accounts(self.data)[0]['label'], 'first edit')
