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


def fake_version_ok(have, op, want):
    a = tuple(map(int, have.split('.')))
    b = tuple(map(int, want.split('.')))
    return {'<<': a < b, '<=': a <= b, '=': a == b, '>=': a >= b, '>>': a > b}[op]


class TestRegistry(unittest.TestCase):
    def test_store_apps_keeps_approved_debs_only(self):
        self.assertEqual([a['package'] for a in registry.store_apps(REG)],
                         ['alpha', 'beta', 'gamma'])

    def test_scan_lists_installed_with_updates_first(self):
        with mock.patch.object(registry, 'newer', fake_newer), \
                mock.patch.object(registry, 'sideloaded', return_value=set()):
            rows = registry.scan(REG, {'gamma': '0.9', 'alpha': '1.1.0', 'beta': '2.0',
                                       'pending': '0.1', 'other': '1'})
        self.assertEqual([(r['package'], r['update']) for r in rows],
                         [('gamma', True), ('alpha', False), ('beta', False)])
        self.assertEqual(rows[0]['installed'], '0.9')

    def test_scan_lists_sideloaded_apps_as_manual_updates(self):
        with mock.patch.object(registry, 'newer', fake_newer), \
                mock.patch.object(registry, 'sideloaded', return_value={'beta'}) as side:
            rows = registry.scan(REG, {'alpha': '1.1.0'})
        side.assert_called_once_with(['beta', 'gamma'])
        self.assertEqual([(r['package'], r['installed'], r['update'], r.get('manual', False))
                          for r in rows],
                         [('beta', 'manual', True, True), ('alpha', '1.1.0', False, False)])

    def test_sideloaded_needs_a_launcher_entry_dpkg_does_not_own(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'applications'))
            os.makedirs(os.path.join(root, 'bin'))
            for name in ('applications/Zero_News.desktop', 'bin/zero-stocks',
                         'applications/music.desktop', 'applications/beta.desktop.orig'):
                open(os.path.join(root, name), 'w').close()

            def owned(paths):
                return any(p.endswith('music.desktop') for p in paths)

            with mock.patch.object(registry, 'dpkg_owned', side_effect=owned):
                found = registry.sideloaded(['zero-news', 'zero-stocks', 'music', 'beta',
                                             'gamma'], root)
        self.assertEqual(found, {'zero-news', 'zero-stocks'})

    def test_sideloaded_without_launcher_dir(self):
        self.assertEqual(registry.sideloaded(['alpha'], '/nonexistent/launcher'), set())

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
            self.assertEqual(installer.simulate(['/x.deb', '/y.deb']),
                             (True, ['libcamera0.7'], 'Conf alpha'))
            self.assertEqual(run.call_args[0][0][-2:], ['/x.deb', '/y.deb'])

    def test_last_line_prefers_apt_errors(self):
        self.assertEqual(installer.last_line('Reading\nE: Unable to lock\nW: x'),
                         'E: Unable to lock')

    def test_parse_relations(self):
        self.assertEqual(
            installer.parse_relations('python3:any (>= 3.9), libicu72 | libicu76,  zlib1g'),
            [[('python3', '>=', '3.9')], [('libicu72', None, None), ('libicu76', None, None)],
             [('zlib1g', None, None)]])
        self.assertEqual(installer.parse_relations(''), [])

    def test_installed_index_includes_provides(self):
        out = ('python3\tii \t3.13.5-1\t\n'
               'libcurl4t64\tii \t8.14.1-2\tlibcurl4 (= 8.14.1-2)\n'
               'gone\trc \t1.0\t\n'
               'libfoo\tii \t2.0\tfoo-virtual, libfoo-api (= 2)\n')
        with mock.patch('subprocess.run') as run:
            run.return_value.stdout = out
            idx = installer.installed_index()
        self.assertEqual(idx['python3'], ['3.13.5-1'])
        self.assertEqual(idx['libcurl4'], ['8.14.1-2'])
        self.assertEqual(idx['foo-virtual'], [None])
        self.assertEqual(idx['libfoo-api'], ['2'])
        self.assertNotIn('gone', idx)

    def fast(self, fields, index):
        with mock.patch.object(installer, 'control_fields', return_value=fields), \
                mock.patch.object(installer, 'version_ok', side_effect=fake_version_ok):
            return installer.fast_path('/x.deb', index)

    def test_fast_path_needs_every_dependency_installed(self):
        index = {'python3': ['3.13.5'], 'python3-pil': ['11.1'], 'libcurl4': ['8.14']}
        self.assertTrue(self.fast({'Depends': 'python3 (>= 3.9), python3-pil'}, index))
        self.assertTrue(self.fast({}, index))
        self.assertTrue(self.fast({'Depends': 'libcurl4t64 | libcurl4'}, index))
        self.assertFalse(self.fast({'Depends': 'python3, python3-numpy'}, index))
        self.assertFalse(self.fast({'Depends': 'python3 (>= 3.14)'}, index))
        self.assertFalse(self.fast({'Pre-Depends': 'dpkg (>= 1.20)'}, index))

    def test_fast_path_rejects_unversioned_provide_for_versioned_dep(self):
        self.assertFalse(self.fast({'Depends': 'foo (>= 1)'}, {'foo': [None]}))
        self.assertTrue(self.fast({'Depends': 'foo'}, {'foo': [None]}))

    def test_fast_path_skips_packages_that_conflict_or_break(self):
        self.assertFalse(self.fast({'Conflicts': 'old-alpha'}, {}))
        self.assertFalse(self.fast({'Breaks': 'beta (<< 2)'}, {}))

    def run_job(self, apps=None, fast=(), sim=None, installed=None, sudo_ok=True,
                sudo_rc=0):
        """Run an Installer with every subprocess mocked. fast: packages whose deps are
        already installed. sim(paths) -> simulate result (default: clean)."""
        apps = apps or [self.app]
        job = installer.Installer(apps, 'pw')
        installed = installed or {a['package']: a['version'] for a in apps}
        sims = []

        def simulate(paths):
            sims.append(list(paths))
            return sim(paths) if sim else (True, [], '')

        with mock.patch.object(installer, 'download',
                               side_effect=lambda a, p: f"/d/{a['package']}.deb"), \
                mock.patch.object(installer, 'installed_index', return_value={}), \
                mock.patch.object(installer, 'fast_path',
                                  side_effect=lambda path, i: path[3:-4] in fast), \
                mock.patch.object(installer, 'simulate', side_effect=simulate), \
                mock.patch.object(installer, 'sudo_ready', return_value=False), \
                mock.patch.object(installer, 'sudo_login', return_value=sudo_ok), \
                mock.patch.object(installer, 'sudo') as sudo, \
                mock.patch.object(installer, 'installed_versions', return_value=installed), \
                mock.patch.object(installer, 'note_in_store') as note, \
                mock.patch('os.remove'):
            sudo.return_value.returncode = sudo_rc
            sudo.return_value.stdout, sudo.return_value.stderr = '', 'E: boom'
            job.run()
        return job, sudo, note, sims

    def two_apps(self):
        beta = dict(self.app, package='beta', title='Beta', version='2.0')
        return [self.app, beta]

    def test_installer_updates_and_forgets_password(self):
        job, sudo, note, _ = self.run_job()
        self.assertEqual((job.jobs[0]['state'], job.jobs[0]['msg']), ('done', '1.1.0'))
        self.assertIn('install', sudo.call_args[0][0])
        note.assert_called_once_with('alpha', '1.1.0')
        self.assertTrue(job.finished)
        self.assertIsNone(job.password)

    def test_installer_batches_apt_into_one_check_and_one_install(self):
        job, sudo, _, sims = self.run_job(apps=self.two_apps())
        self.assertEqual(sims, [['/d/alpha.deb', '/d/beta.deb']])
        self.assertEqual(sudo.call_count, 1)
        self.assertEqual(sudo.call_args[0][0][-3:], ['install', '/d/alpha.deb', '/d/beta.deb'])
        self.assertEqual([j['state'] for j in job.jobs], ['done', 'done'])

    def test_installer_uses_dpkg_when_dependencies_are_installed(self):
        job, sudo, _, sims = self.run_job(apps=self.two_apps(), fast=('alpha', 'beta'))
        self.assertEqual(sims, [])
        cmd = sudo.call_args[0][0]
        self.assertEqual(cmd[0], 'dpkg')
        self.assertEqual(cmd[-3:], ['-i', '/d/alpha.deb', '/d/beta.deb'])
        self.assertEqual([j['state'] for j in job.jobs], ['done', 'done'])

    def test_installer_mixes_dpkg_and_apt(self):
        job, sudo, _, sims = self.run_job(apps=self.two_apps(), fast=('alpha',))
        self.assertEqual(sims, [['/d/beta.deb']])
        self.assertEqual([c[0][0][0] for c in sudo.call_args_list], ['dpkg', 'apt-get'])

    def test_installer_skips_install_that_removes_packages(self):
        job, sudo, _, _ = self.run_job(sim=lambda p: (True, ['libcamera0.7'], ''))
        self.assertEqual(job.jobs[0]['state'], 'failed')
        self.assertIn('libcamera0.7', job.jobs[0]['msg'])
        sudo.assert_not_called()

    def test_installer_drops_only_the_package_that_removes_others(self):
        def sim(paths):
            return (True, ['libcamera0.7'] if '/d/beta.deb' in paths else [], '')
        job, sudo, _, sims = self.run_job(apps=self.two_apps(), sim=sim)
        self.assertEqual(sims, [['/d/alpha.deb', '/d/beta.deb'], ['/d/alpha.deb'],
                                ['/d/beta.deb']])
        self.assertEqual(sudo.call_args[0][0][-2:], ['install', '/d/alpha.deb'])
        self.assertEqual([j['state'] for j in job.jobs], ['done', 'failed'])
        self.assertIn('libcamera0.7', job.jobs[1]['msg'])

    def test_installer_fails_on_wrong_version_or_password(self):
        job, _, _, _ = self.run_job(installed={'alpha': '1.0'})
        self.assertIn('installed version', job.jobs[0]['msg'])
        job, sudo, _, _ = self.run_job(sudo_ok=False)
        self.assertEqual(job.jobs[0]['msg'], 'sudo password needed')
        sudo.assert_not_called()

    def test_installer_reports_install_error(self):
        job, _, _, _ = self.run_job(installed={'alpha': '1.0'}, sudo_rc=100)
        self.assertEqual(job.jobs[0]['msg'], 'E: boom')

    def test_installer_keeps_going_after_failed_download(self):
        apps = self.two_apps()
        job = installer.Installer(apps, 'pw')

        def download(app, progress):
            if app['package'] == 'alpha':
                raise installer.Failed('SHA-256 mismatch')
            return '/d/beta.deb'

        with mock.patch.object(installer, 'download', side_effect=download), \
                mock.patch.object(installer, 'installed_index', return_value={}), \
                mock.patch.object(installer, 'fast_path', return_value=True), \
                mock.patch.object(installer, 'sudo_ready', return_value=True), \
                mock.patch.object(installer, 'sudo') as sudo, \
                mock.patch.object(installer, 'installed_versions',
                                  return_value={'beta': '2.0'}), \
                mock.patch.object(installer, 'note_in_store'), mock.patch('os.remove'):
            sudo.return_value.returncode = 0
            job.run()
        self.assertEqual([j['state'] for j in job.jobs], ['failed', 'done'])
        self.assertEqual(sudo.call_args[0][0][-2:], ['-i', '/d/beta.deb'])

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
