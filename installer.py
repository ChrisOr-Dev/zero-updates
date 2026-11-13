"""Download, verify and install store packages, one after another, in a thread.

Each package is downloaded from the registry URL, checked against the registry
size and SHA-256, and its control fields against the expected package and
version. An apt-get simulation runs first; a package whose install would remove
other packages is skipped, since apt can silently drop libraries other apps
link against. The install itself runs as root through sudo: the password is
checked once with `sudo -S -v` and later commands use `sudo -n`, so it never
reaches apt's stdin.
"""
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.request

from registry import UA, installed_versions

DOWNLOADS = os.path.expanduser('~/.cache/zero-updates/downloads')
STORE_RECORD = os.path.expanduser('~/.local/share/cardputerzero-appstore/installed.json')
APT = ['apt-get', '-y', '-o', 'Dpkg::Options::=--force-confdef',
       '-o', 'Dpkg::Options::=--force-confold']
ENV = dict(os.environ, DEBIAN_FRONTEND='noninteractive', LC_ALL='C')
CHUNK = 64 * 1024


class Failed(Exception):
    pass


def sudo_ready():
    """Whether sudo runs without a password right now (cached or NOPASSWD)."""
    return subprocess.run(['sudo', '-n', 'true'], stdin=subprocess.DEVNULL,
                          capture_output=True).returncode == 0


def sudo_login(password):
    """Validate the password and refresh sudo's timestamp. True when accepted."""
    r = subprocess.run(['sudo', '-S', '-p', '', '-v'], input=password + '\n', text=True,
                       capture_output=True, timeout=30)
    return r.returncode == 0


def last_line(text):
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith(('E:', 'dpkg:'))]
    return (errors or lines or ['unknown error'])[-1]


def download(app, progress):
    """Fetch the .deb into DOWNLOADS, verify it, and return its path."""
    os.makedirs(DOWNLOADS, exist_ok=True)
    path = os.path.join(DOWNLOADS, f"{app['package']}_{app['version']}.deb")
    req = urllib.request.Request(app['url'], headers={'User-Agent': UA})
    digest, got = hashlib.sha256(), 0
    with urllib.request.urlopen(req, timeout=30) as r, open(path + '.part', 'wb') as f:
        total = app['size'] or int(r.headers.get('Content-Length') or 0)
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if total:
                progress(min(1.0, got / total))
    if app['size'] and got != app['size']:
        raise Failed(f"size {got} != {app['size']}")
    if digest.hexdigest() != app['sha256'].lower():
        raise Failed('SHA-256 mismatch')
    os.replace(path + '.part', path)
    fields = subprocess.run(['dpkg-deb', '-f', path, 'Package', 'Version'],
                            capture_output=True, text=True).stdout
    meta = dict(ln.split(': ', 1) for ln in fields.splitlines() if ': ' in ln)
    if meta.get('Package') != app['package'] or meta.get('Version') != app['version']:
        raise Failed(f"package is {meta.get('Package')} {meta.get('Version')}")
    return path


def simulate(path):
    """(ok, removals, error) from an apt-get dry run (needs no root)."""
    r = subprocess.run(APT + ['-s', 'install', path], capture_output=True, text=True, env=ENV)
    removals = [ln.split()[1] for ln in r.stdout.splitlines() if ln.startswith('Remv ')]
    return r.returncode == 0, removals, last_line(r.stderr or r.stdout)


def sudo(cmd, timeout):
    return subprocess.run(['sudo', '-n'] + cmd, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, env=ENV, timeout=timeout)


def note_in_store(package, version):
    """Keep the AppStore's own record of the package's version current."""
    try:
        with open(STORE_RECORD) as f:
            record = json.load(f)
    except (OSError, ValueError):
        return
    hit = False
    for entry in record.values():
        if isinstance(entry, dict) and entry.get('package') == package:
            entry['version'] = version
            hit = True
    if hit:
        tmp = STORE_RECORD + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, STORE_RECORD)


class Installer(threading.Thread):
    """Updates the given apps in order. jobs[i] = {'app', 'state', 'pct', 'msg'};
    state: waiting | download | check | install | done | failed."""

    def __init__(self, apps, password=None):
        super().__init__(daemon=True)
        self.jobs = [{'app': a, 'state': 'waiting', 'pct': 0.0, 'msg': '', 'since': 0}
                     for a in apps]
        self.password = password
        self.updated_lists = False
        self.version = 0
        self.finished = False

    def _set(self, job, state, msg=''):
        job.update(state=state, msg=msg, since=time.time())
        self.version += 1

    def _root(self):
        if not sudo_ready() and not (self.password and sudo_login(self.password)):
            raise Failed('sudo password needed')

    def _install(self, job):
        app = job['app']

        def progress(p):
            job['pct'] = p
            self.version += 1

        self._set(job, 'download')
        path = download(app, progress)
        self._set(job, 'check')
        ok, removals, err = simulate(path)
        if not ok and not self.updated_lists:
            self._root()
            self.updated_lists = True
            sudo(['apt-get', 'update'], 600)
            ok, removals, err = simulate(path)
        if not ok:
            raise Failed(err)
        if removals:
            raise Failed('would remove ' + ', '.join(removals[:3]))
        self._root()
        self._set(job, 'install')
        r = sudo(APT + ['install', path], 1800)
        if r.returncode != 0:
            raise Failed(last_line(r.stderr or r.stdout))
        have = installed_versions().get(app['package'])
        if have != app['version']:
            raise Failed(f'installed version is {have}')
        note_in_store(app['package'], app['version'])
        try:
            os.remove(path)
        except OSError:
            pass

    def run(self):
        for job in self.jobs:
            try:
                self._install(job)
                self._set(job, 'done', job['app']['version'])
            except Exception as e:
                self._set(job, 'failed', str(e) or type(e).__name__)
        self.password = None
        self.finished = True
        self.version += 1
