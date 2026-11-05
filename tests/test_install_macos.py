from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_macos


class InstallerTests(unittest.TestCase):
    def test_install_and_uninstall_use_package_entries_and_preserve_data(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'Library').mkdir()
            runtime = home / 'private-data'
            runtime.mkdir()
            sentinel = runtime / 'config.json'
            sentinel.write_text('{"people": []}')
            with patch.object(install_macos.Path, 'home', return_value=home), \
                    patch.object(install_macos, 'RUNTIME', runtime), \
                    patch.object(install_macos, 'ensure_home'), \
                    patch.object(install_macos.sys, 'platform', 'darwin'), \
                    patch.object(install_macos.subprocess, 'run') as launchctl, \
                    patch.object(install_macos.sys, 'argv', ['install_macos']), \
                    patch('builtins.print'):
                install_macos.main()
                expected = {
                    'monitor': [sys.executable, '-m', 'rednote_watchdesk.tick'],
                    'watchdog': ['/usr/bin/python3', '-m', 'rednote_watchdesk.watchdog'],
                    'ui': [sys.executable, '-m', 'rednote_watchdesk.config_server', '--no-scheduler'],
                }
                for role, label in install_macos.LABELS.items():
                    path = home / 'Library/LaunchAgents' / (label + '.plist')
                    payload = plistlib.loads(path.read_bytes())
                    self.assertEqual(payload['ProgramArguments'], expected[role])
                    self.assertEqual(payload['WorkingDirectory'], str(install_macos.PROJECT))
                    self.assertEqual(payload['ProcessType'], 'Background' if role == 'watchdog' else 'Interactive')
                    self.assertEqual(payload['EnvironmentVariables']['REDNOTE_WATCHDESK_HOME'], str(runtime))
                self.assertEqual(sum(call.args[0][1] == 'bootstrap' for call in launchctl.call_args_list), 3)
                with patch.object(install_macos.sys, 'argv', ['install_macos', '--uninstall']):
                    install_macos.main()
                self.assertFalse(list((home / 'Library/LaunchAgents').glob('*.plist')))
                self.assertEqual(sentinel.read_text(), '{"people": []}')
