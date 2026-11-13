"""The official AppStore registry and the versions installed on this device.

scan() returns one entry per store app that dpkg has installed, with the
installed and the latest version, so the caller can list what needs updating.
"""
import json
import os
import subprocess
import time
import urllib.request

REGISTRY_URL = 'https://cardputerzero.github.io/generated/registry.json'
CACHE = os.path.expanduser('~/.cache/zero-updates/registry.json')
UA = 'zero-updates/1.0'
TIMEOUT = 20


def fetch_registry(url=REGISTRY_URL):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
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


def scan(registry, installed=None):
    """Installed store apps, updates first then by title. Each entry adds
    'installed' and 'update' (bool) to the store_apps() fields."""
    installed = installed_versions() if installed is None else installed
    rows = []
    for app in store_apps(registry):
        have = installed.get(app['package'])
        if have is None:
            continue
        rows.append(dict(app, installed=have, update=newer(app['version'], have)))
    rows.sort(key=lambda r: (not r['update'], r['title'].lower()))
    return rows


class Checker:
    """Loads the registry (falling back to the cache when offline) and scans."""

    def __init__(self):
        self.rows = []
        self.error = None
        self.checked_at = 0
        self.offline = False

    def check(self):
        try:
            registry, at, self.offline = fetch_registry(), time.time(), False
            self.error = None
        except Exception as e:
            registry, at = cached_registry()
            self.offline = True
            self.error = f'offline: {e}' if registry is None else None
            if registry is None:
                return
        self.rows = scan(registry)
        self.checked_at = at
