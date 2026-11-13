import hashlib
import io
import os
import tempfile
import unittest
from unittest import mock

import installer
import keys
import registry
import views
import zero_updates as Z
from hold_exit import ExitHold


def store_app(pkg, version, status='approved', dtype='deb'):
    return {'title': pkg.title(), 'version': version, 'review': {'status': status},
            'download': {'type': dtype, 'package': pkg, 'url': f'https://x/{pkg}.deb',
                         'sha256': 'ab' * 32, 'size': 10}}


REG = {'apps': [store_app('alpha', '1.1.0'), store_app('beta', '2.0'),
                store_app('gamma', '1.0'), store_app('pending', '1.0', status='pending'),
                store_app('zipped', '1.0', dtype='zip')]}


def fake_newer(latest, installed):
    return tuple(map(int, latest.split('.'))) > tuple(map(int, installed.split('.')))


class TestRegistry(unittest.TestCase):
    def test_store_apps_keeps_approved_debs_only(self):
        self.assertEqual([a['package'] for a in registry.store_apps(REG)],
                         ['alpha', 'beta', 'gamma'])

    def test_scan_lists_installed_with_updates_first(self):
        with mock.patch.object(registry, 'newer', fake_newer):
            rows = registry.scan(REG, {'gamma': '0.9', 'alpha': '1.1.0', 'beta': '2.0',
                                       'pending': '0.1', 'other': '1'})
        self.assertEqual([(r['package'], r['update']) for r in rows],
                         [('gamma', True), ('alpha', False), ('beta', False)])
        self.assertEqual(rows[0]['installed'], '0.9')

    def test_installed_versions_parses_dpkg_query(self):
        out = 'alpha\tii \t1.0\nbeta:arm64\tii \t2.0\ngone\trc \t1.0\n'
        with mock.patch('subprocess.run') as run:
            run.return_value.stdout = out
            self.assertEqual(registry.installed_versions(), {'alpha': '1.0', 'beta': '2.0'})

    def test_checker_falls_back_to_cache_offline(self):
        c = registry.Checker()
        with mock.patch.object(registry, 'fetch_registry', side_effect=OSError('down')), \
                mock.patch.object(registry, 'cached_registry', return_value=(REG, 5)), \
                mock.patch.object(registry, 'scan', return_value=['row']):
            c.check()
        self.assertEqual((c.rows, c.offline, c.error, c.checked_at), (['row'], True, None, 5))

    def test_checker_error_without_cache(self):
        c = registry.Checker()
        with mock.patch.object(registry, 'fetch_registry', side_effect=OSError('down')), \
                mock.patch.object(registry, 'cached_registry', return_value=(None, 0)):
            c.check()
        self.assertTrue(c.error.startswith('offline'))


class TestNet(unittest.TestCase):
    def test_ipv4_sorted_first(self):
        import socket
        import net
        v6 = (socket.AF_INET6, 1, 6, '', ('::1', 443, 0, 0))
        v4 = (socket.AF_INET, 1, 6, '', ('1.2.3.4', 443))
        with mock.patch.object(net, '_getaddrinfo', return_value=[v6, v6, v4]):
            self.assertEqual(net._ipv4_first('h', 443), [v4, v6, v6])


class TestCheckProgress(unittest.TestCase):
    def test_stages_advance_to_full(self):
        c = registry.Checker()
        seen = []
        c._at = lambda stage, pct: seen.append((stage, round(pct, 2)))

        def fetch(progress):
            progress(100 * 1024, 200 * 1024)
            return REG

        with mock.patch.object(registry, 'fetch_registry', side_effect=fetch), \
                mock.patch.object(registry, 'installed_versions',
                                  return_value={'alpha': '1.0.0', 'beta': '2.0'}), \
                mock.patch.object(registry, 'newer', return_value=False):
            c.check()
        self.assertEqual(seen[1], ('Downloading store list  100 KB of 200 KB', 0.3))
        self.assertEqual(seen[-1], ('Comparing versions  2/2', 1.0))
        self.assertEqual([p for _, p in seen], sorted(p for _, p in seen))


class FakeResponse(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(installer, 'DOWNLOADS', self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.body = b'0123456789'
        self.app = {'package': 'alpha', 'title': 'Alpha', 'version': '1.1.0',
                    'installed': '1.0', 'url': 'https://x/a.deb', 'size': 10,
                    'sha256': hashlib.sha256(self.body).hexdigest()}

    def fetch(self, fields='Package: alpha\nVersion: 1.1.0\n'):
        with mock.patch('urllib.request.urlopen', return_value=FakeResponse(self.body)), \
                mock.patch('subprocess.run') as run:
            run.return_value.stdout = fields
            seen = []
            return installer.download(self.app, seen.append), seen

    def test_download_verifies_and_reports_progress(self):
        path, seen = self.fetch()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(seen[-1], 1.0)

    def test_download_rejects_bad_hash(self):
        self.app['sha256'] = '0' * 64
        with self.assertRaisesRegex(installer.Failed, 'SHA-256'):
            self.fetch()

    def test_download_rejects_other_package(self):
        with self.assertRaisesRegex(installer.Failed, 'package is'):
            self.fetch('Package: evil\nVersion: 1.1.0\n')

    def test_simulate_finds_removals(self):
        out = 'Inst alpha [1.0] (1.1.0 local)\nRemv libcamera0.7 [0.7.2]\nConf alpha\n'
        with mock.patch('subprocess.run') as run:
            run.return_value.returncode, run.return_value.stdout = 0, out
            run.return_value.stderr = ''
            self.assertEqual(installer.simulate('/x.deb'), (True, ['libcamera0.7'], 'Conf alpha'))

    def test_last_line_prefers_apt_errors(self):
        self.assertEqual(installer.last_line('Reading\nE: Unable to lock\nW: x'),
                         'E: Unable to lock')

    def run_job(self, removals=(), installed='1.1.0', sudo_ok=True):
        job = installer.Installer([self.app], 'pw')
        with mock.patch.object(installer, 'download', return_value='/x.deb'), \
                mock.patch.object(installer, 'simulate', return_value=(True, list(removals), '')), \
                mock.patch.object(installer, 'sudo_ready', return_value=False), \
                mock.patch.object(installer, 'sudo_login', return_value=sudo_ok), \
                mock.patch.object(installer, 'sudo') as sudo, \
                mock.patch.object(installer, 'installed_versions',
                                  return_value={'alpha': installed}), \
                mock.patch.object(installer, 'note_in_store') as note:
            sudo.return_value.returncode = 0
            job.run()
        return job, sudo, note

    def test_installer_updates_and_forgets_password(self):
        job, sudo, note = self.run_job()
        self.assertEqual((job.jobs[0]['state'], job.jobs[0]['msg']), ('done', '1.1.0'))
        self.assertIn('install', sudo.call_args[0][0])
        note.assert_called_once_with('alpha', '1.1.0')
        self.assertTrue(job.finished)
        self.assertIsNone(job.password)

    def test_installer_skips_install_that_removes_packages(self):
        job, sudo, _ = self.run_job(removals=['libcamera0.7'])
        self.assertEqual(job.jobs[0]['state'], 'failed')
        self.assertIn('libcamera0.7', job.jobs[0]['msg'])
        sudo.assert_not_called()

    def test_installer_fails_on_wrong_version_or_password(self):
        job, _, _ = self.run_job(installed='1.0')
        self.assertIn('installed version', job.jobs[0]['msg'])
        job, sudo, _ = self.run_job(sudo_ok=False)
        self.assertEqual(job.jobs[0]['msg'], 'sudo password needed')
        sudo.assert_not_called()

    def test_note_in_store_updates_matching_entry(self):
        path = os.path.join(self.tmp.name, 'installed.json')
        with open(path, 'w') as f:
            f.write('{"u1": {"package": "alpha", "version": "1.0"},'
                    ' "u2": {"package": "beta", "version": "2.0"}}')
        with mock.patch.object(installer, 'STORE_RECORD', path):
            installer.note_in_store('alpha', '1.1.0')
        import json
        with open(path) as f:
            rec = json.load(f)
        self.assertEqual((rec['u1']['version'], rec['u2']['version']), ('1.1.0', '2.0'))


class TestApp(unittest.TestCase):
    def setUp(self):
        self.app = Z.App()
        self.app.checker.rows = Z.preview_rows()
        self.app.checker.checked_at = 1
        self.hold = ExitHold()

    def press(self, *codes):
        for c in codes:
            self.app.handle(c, self.hold, 0)

    def test_update_all_button_is_first_and_selected(self):
        self.assertEqual(self.app.items[0], {'all': 3})
        with mock.patch.object(installer, 'sudo_ready', return_value=True):
            self.press(keys.K_SPACE, keys.K_ENTER)   # SPACE on the button does nothing
        self.assertEqual(self.app.picked, set())
        self.assertEqual(len(self.app.targets), 3)

    def test_no_button_without_updates(self):
        for r in self.app.rows:
            r['update'] = False
        self.assertEqual(self.app.items, self.app.rows)

    def test_space_picks_only_updatable_rows(self):
        self.press(45, keys.K_SPACE, 45, 45, 45, keys.K_SPACE)   # X moves down
        self.assertEqual(self.app.picked, {'advancewebui'})

    def test_enter_updates_picked_or_current(self):
        with mock.patch.object(installer, 'sudo_ready', return_value=False):
            self.press(45, 45, keys.K_ENTER)
        self.assertEqual(self.app.view, 'confirm')
        self.assertEqual([t['package'] for t in self.app.targets], ['meshterm'])
        self.assertTrue(self.app.need_password)

    def test_enter_on_current_row_without_update_does_nothing(self):
        self.app.sel = 5
        self.press(keys.K_ENTER)
        self.assertEqual(self.app.view, 'list')

    def test_a_updates_all(self):
        with mock.patch.object(installer, 'sudo_ready', return_value=True):
            self.press(Z.K_A)
        self.assertEqual(len(self.app.targets), 3)
        self.assertFalse(self.app.need_password)

    def test_password_typing_uses_letters_not_arrows(self):
        with mock.patch.object(installer, 'sudo_ready', return_value=False):
            self.press(Z.K_A)
        self.press(33, 44, 2 + keys.SHIFT, keys.K_BACKSPACE, 46)   # f z ! <bs> c
        self.assertEqual(self.app.password, 'fzc')
        self.press(keys.K_ESC)
        self.assertEqual((self.app.view, self.app.password), ('list', ''))

    def test_wrong_password_clears_and_says_so(self):
        with mock.patch.object(installer, 'sudo_ready', return_value=False):
            self.press(Z.K_A)
        self.press(33)
        with mock.patch.object(installer, 'sudo_login', return_value=False), \
                mock.patch('threading.Thread') as T:
            self.press(keys.K_ENTER)
            T.call_args[1]['target']()
        self.assertEqual(self.app.note[0], 'wrong password')
        self.assertEqual(self.app.password, '')

    def test_failed_check_ends_checking_with_error(self):
        with mock.patch.object(self.app.checker, 'check', side_effect=RuntimeError('boom')), \
                mock.patch('threading.Thread') as T:
            self.app.check()
            self.assertTrue(self.app.checking)
            T.call_args[1]['target']()
        self.assertFalse(self.app.checking)
        self.assertEqual(self.app.checker.error, 'boom')

    def test_esc_in_list_starts_exit_hold(self):
        self.press(keys.K_ESC)
        self.assertIsNotNone(self.hold.start)

    def test_renders(self):
        for argv in ([], ['--confirm'], ['--progress'], ['--progress', '--finished'],
                     ['--checking'], ['--checking', '--empty']):
            with tempfile.NamedTemporaryFile(suffix='.png') as f:
                Z.main(['--preview', f.name] + argv)
        img = views.render_list([], 0, 0, set(), ('', 0))
        self.assertEqual(img.size, (320, 170))


if __name__ == '__main__':
    unittest.main()
