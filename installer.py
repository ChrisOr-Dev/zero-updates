"""Download, verify and install store packages in a thread.

Each package is downloaded from the registry URL, checked against the registry
size and SHA-256, and its control fields against the expected package and
version. Every apt run loads all package lists (~150 MB, about 20 s on the
device), so the packages are installed in batches rather than one by one:

- A package whose dependencies are all installed already, and that declares no
  Conflicts or Breaks, goes to a single `dpkg -i` with the others like it. dpkg
  never removes other packages for it; it fails instead.
- The rest go to apt: one simulation for all of them, then one install. A
  package whose install would remove other packages is skipped, since apt can
  silently drop libraries other apps link against. When the batch simulation
  fails or removes something, each package is simulated alone to find which.

Root commands run through sudo: the password is checked once with `sudo -S -v`
and later commands use `sudo -n`, so it never reaches apt's stdin.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from registry import UA, installed_versions

DOWNLOADS = os.path.expanduser('~/.cache/zero-updates/downloads')
STORE_RECORD = os.path.expanduser('~/.local/share/cardputerzero-appstore/installed.json')
APT = ['apt-get', '-y', '-o', 'Dpkg::Options::=--force-confdef',
       '-o', 'Dpkg::Options::=--force-confold']
DPKG = ['dpkg', '--force-confdef', '--force-confold']
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
    meta = control_fields(path, 'Package', 'Version')
    if meta.get('Package') != app['package'] or meta.get('Version') != app['version']:
        raise Failed(f"package is {meta.get('Package')} {meta.get('Version')}")
    return path


def control_fields(path, *names):
    """{field: value} for the named control fields of a .deb (continuation lines joined)."""
    out = subprocess.run(['dpkg-deb', '-f', path, *names], capture_output=True, text=True).stdout
    fields, key = {}, None
    for line in out.splitlines():
        if line[:1].isspace() and key:
            fields[key] += ' ' + line.strip()
        elif ': ' in line:
            key, value = line.split(': ', 1)
            fields[key] = value.strip()
    return fields


RELATION = re.compile(r'([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?\s*'
                      r'(?:\(\s*(<<|<=|=|>=|>>)\s*([^)\s]+)\s*\))?')


def parse_relations(text):
    """'a (>= 1), b | c' -> [[('a', '>=', '1')], [('b', None, None), ('c', None, None)]]."""
    groups = []
    for part in (text or '').split(','):
        alts = [m.groups() for m in (RELATION.match(a.strip()) for a in part.split('|')) if m]
        if alts:
            groups.append(alts)
    return groups


def installed_index():
    """{name: [version, ...]} for fully installed packages and the names they provide
    (None for a provide without a version)."""
    out = subprocess.run(['dpkg-query', '-W',
                          '-f=${Package}\t${db:Status-Abbrev}\t${Version}\t${Provides}\n'],
                         capture_output=True, text=True).stdout
    index = {}
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) != 4 or parts[1].strip() != 'ii':
            continue
        index.setdefault(parts[0].split(':')[0], []).append(parts[2])
        for group in parse_relations(parts[3]):
            name, _, version = group[0]
            index.setdefault(name, []).append(version)
    return index


def version_ok(have, op, want):
    return subprocess.run(['dpkg', '--compare-versions', have, op, want]).returncode == 0


def satisfied(alts, index):
    """Whether an installed package (or provide) meets one of the alternatives."""
    for name, op, want in alts:
        for have in index.get(name, []):
            if not op or (have is not None and version_ok(have, op, want)):
                return True
    return False


def fast_path(path, index):
    """Whether dpkg alone can install the .deb: every dependency is installed already
    and the package conflicts with and breaks nothing."""
    fields = control_fields(path, 'Depends', 'Pre-Depends', 'Conflicts', 'Breaks')
    if fields.get('Conflicts') or fields.get('Breaks'):
        return False
    groups = parse_relations(fields.get('Pre-Depends')) + parse_relations(fields.get('Depends'))
    return all(satisfied(alts, index) for alts in groups)


def simulate(paths):
    """(ok, removals, error) from an apt-get dry run (needs no root)."""
    r = subprocess.run(APT + ['-s', 'install', *paths], capture_output=True, text=True, env=ENV)
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
    """Downloads the given apps in order, then installs them in batches.
    jobs[i] = {'app', 'state', 'pct', 'msg'};
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

    def _fail(self, jobs, msg):
        for job in jobs:
            if job['state'] not in ('done', 'failed'):
                self._set(job, 'failed', msg)

    def _download(self, job):
        def progress(p):
            job['pct'] = p
            self.version += 1

        self._set(job, 'download')
        job['path'] = download(job['app'], progress)
        self._set(job, 'check')

    def _vet(self, jobs):
        """The jobs apt can install without removing other packages; marks the rest failed."""
        paths = [j['path'] for j in jobs]
        ok, removals, err = simulate(paths)
        if not ok and not self.updated_lists:
            self._root()
            self.updated_lists = True
            sudo(['apt-get', 'update'], 600)
            ok, removals, err = simulate(paths)
        if ok and not removals:
            return jobs
        if len(jobs) > 1:
            return [j for j in jobs if self._vet([j])]
        self._fail(jobs, err if not ok else 'would remove ' + ', '.join(removals[:3]))
        return []

    def _batch(self, jobs, cmd, vet=False):
        """Install the jobs with one root command: cmd + their .deb paths. vet: simulate with
        apt first and drop the jobs that would remove other packages."""
        if not jobs:
            return
        try:
            if vet:
                jobs = self._vet(jobs)
            if jobs:
                self._run_install(jobs, cmd)
        except Exception as e:
            self._fail(jobs, str(e) or type(e).__name__)

    def _run_install(self, jobs, cmd):
        """Run cmd + the jobs' .deb paths as root and settle each job by its installed version."""
        self._root()
        for job in jobs:
            self._set(job, 'install')
        r = sudo(cmd + [j['path'] for j in jobs], 1800)
        have = installed_versions()
        for job in jobs:
            app = job['app']
            got = have.get(app['package'])
            if got != app['version']:
                self._fail([job], last_line(r.stderr or r.stdout) if r.returncode
                           else f'installed version is {got}')
                continue
            note_in_store(app['package'], app['version'])
            try:
                os.remove(job['path'])
            except OSError:
                pass
            self._set(job, 'done', app['version'])

    def _install_all(self):
        for job in self.jobs:
            try:
                self._download(job)
            except Exception as e:
                self._fail([job], str(e) or type(e).__name__)
        ready = [j for j in self.jobs if j['state'] == 'check']
        index = installed_index()
        fast = [j for j in ready if fast_path(j['path'], index)]
        self._batch(fast, DPKG + ['-i'])
        self._batch([j for j in ready if j not in fast], APT + ['install'], vet=True)

    def run(self):
        try:
            self._install_all()
        except Exception as e:
            self._fail(self.jobs, str(e) or type(e).__name__)
        self.password = None
        self.finished = True
        self.version += 1
