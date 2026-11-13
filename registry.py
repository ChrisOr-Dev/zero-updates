"""The official AppStore registry and the versions installed on this device.

scan() returns one entry per store app that dpkg has installed, with the
installed and the latest version (and the store apps installed by hand),
so the caller can list what needs updating.
"""
import json
import os
import re
import subprocess
import time
import urllib.request

REGISTRY_URL = 'https://cardputerzero.github.io/generated/registry.json'
LAUNCHER = '/usr/share/APPLaunch'
CACHE = os.path.expanduser('~/.cache/zero-updates/registry.json')
UA = 'zero-updates/1.0'
TIMEOUT = 20


def fetch_registry(url=REGISTRY_URL, progress=None):
    """Download the registry; progress(bytes_read, total_or_0) after each chunk."""
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    chunks, got = [], 0
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        total = int(r.headers.get('Content-Length') or 0)
        while True:
            chunk = r.read(16 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    data = json.loads(b''.join(chunks))
    if not isinstance(data.get('apps'), list):
        raise ValueError('registry has no app list')
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, CACHE)
    return data


def cached_registry():
    """(registry, fetched_at) from the last successful fetch, or (None, 0)."""
    try:
        with open(CACHE) as f:
            return json.load(f), os.path.getmtime(CACHE)
    except (OSError, ValueError):
        return None, 0


def store_apps(registry):
    """Approved .deb apps: [{'package', 'title', 'version', 'url', 'sha256', 'size'}]."""
    apps = []
    for a in registry.get('apps', []):
        dl = a.get('download') or {}
        if dl.get('type') != 'deb' or (a.get('review') or {}).get('status') != 'approved':
            continue
        if not (dl.get('package') and dl.get('url') and dl.get('sha256') and a.get('version')):
            continue
        apps.append({'package': dl['package'], 'title': a.get('title') or dl['package'],
                     'version': str(a['version']), 'url': dl['url'], 'sha256': dl['sha256'],
                     'size': dl.get('size') or 0})
    return apps


def installed_versions():
    """{package: version} for every package dpkg has fully installed."""
    out = subprocess.run(['dpkg-query', '-W', '-f=${Package}\t${db:Status-Abbrev}\t${Version}\n'],
                         capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) == 3 and parts[1].strip() == 'ii':
            found[parts[0].split(':')[0]] = parts[2]
    return found


def newer(latest, installed):
    """Whether Debian version latest is greater than installed."""
    return subprocess.run(['dpkg', '--compare-versions', latest, 'gt', installed]).returncode == 0


def _key(name):
    return re.sub(r'[^a-z0-9]', '', name.lower())


def dpkg_owned(paths):
    """Whether dpkg lists any of paths as part of an installed package."""
    r = subprocess.run(['dpkg', '-S'] + paths, capture_output=True, text=True)
    return bool(r.stdout.strip())


def sideloaded(packages, root=LAUNCHER):
    """The packages that have a launcher entry (applications/<name>.desktop or
    bin/<name>, compared ignoring case, '-' and '_') that no dpkg package owns:
    apps installed by copying files or by a script instead of from the store."""
    entries = {}
    for sub, ext in (('applications', '.desktop'), ('bin', '')):
        folder = os.path.join(root, sub)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if ext and not name.endswith(ext):
                continue
            stem = name[:-len(ext)] if ext else name
            entries.setdefault(_key(stem), []).append(os.path.join(folder, name))
    return {p for p in packages if entries.get(_key(p)) and not dpkg_owned(entries[_key(p)])}


def scan(registry, installed=None, progress=None):
    """Installed store apps, updates first then by title. Each entry adds
    'installed' and 'update' (bool) to the store_apps() fields. Store apps
    installed by hand (see sideloaded()) have no known version: they come back
    with installed 'manual', manual True and update True, so the store version
    can replace them. progress(done, total) after each version comparison."""
    installed = installed_versions() if installed is None else installed
    apps = store_apps(registry)
    manual = sideloaded([a['package'] for a in apps if a['package'] not in installed])
    mine = [a for a in apps if a['package'] in installed]
    rows = [dict(a, installed='manual', manual=True, update=True)
            for a in apps if a['package'] in manual]
    for i, app in enumerate(mine):
        have = installed[app['package']]
        rows.append(dict(app, installed=have, update=newer(app['version'], have)))
        if progress:
            progress(i + 1, len(mine))
    rows.sort(key=lambda r: (not r['update'], r['title'].lower()))
    return rows


class Checker:
    """Loads the registry (falling back to the cache when offline) and scans.
    While check() runs, stage and pct say how far it got (for a progress bar)."""

    # share of the bar per step: download, read installed packages, compare versions
    SPLIT = (0.6, 0.7)

    def __init__(self):
        self.rows = []
        self.error = None
        self.checked_at = 0
        self.offline = False
        self.stage = ''
        self.pct = 0.0
        self.version = 0

    def _at(self, stage, pct):
        self.stage, self.pct = stage, pct
        self.version += 1

    def _downloaded(self, got, total):
        kb = f'{got // 1024} KB' + (f' of {total // 1024} KB' if total else '')
        self._at(f'Downloading store list  {kb}',
                 self.SPLIT[0] * (got / total if total else 0.5))

    def _compared(self, done, total):
        a, b = self.SPLIT[1], 1.0
        self._at(f'Comparing versions  {done}/{total}', a + (b - a) * done / max(1, total))

    def check(self):
        self._at('Connecting to the store', 0.0)
        try:
            registry, at = fetch_registry(progress=self._downloaded), time.time()
            self.offline, self.error = False, None
        except Exception as e:
            registry, at = cached_registry()
            self.offline = True
            self.error = f'offline: {e}' if registry is None else None
            if registry is None:
                return
        self._at('Reading installed apps', self.SPLIT[0])
        installed = installed_versions()
        self._at('Comparing versions', self.SPLIT[1])
        self.rows = scan(registry, installed, self._compared)
        self.checked_at = at
