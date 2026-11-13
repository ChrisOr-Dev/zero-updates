#!/usr/bin/python3
"""Updates for the M5Stack CardputerZero (320x170 LCD): lists the AppStore apps
installed on the device with the installed and the latest version, and updates
the picked ones, or all of them, from the official store registry.

Usage:
  zero_updates.py                 run on the LCD
  zero_updates.py --check         print installed store apps and their versions
  zero_updates.py --preview out.png [--confirm] [--progress]
  zero_updates.py --make-icon out.png
"""
import os
import sys
import threading
import time

import installer
import keys
import net
import registry
import views
from theme import DIM, FAINT, GREEN, H, RED, W, WARN, ACCENT, F_BOLD_SMALL

FB = '/dev/fb0'
KBD = '/dev/input/cardputer-zero-internal'
K_A, K_R = 30, 19


def fmt_ago(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return 'just now'
    if s < 3600:
        return f'{s // 60}m ago'
    if s < 86400:
        return f'{s // 3600}h ago'
    return f'{s // 86400}d ago'


class App:
    def __init__(self):
        self.checker = registry.Checker()
        self.checking = False
        self.view = 'list'        # list | confirm | progress
        self.sel = self.top = 0
        self.picked = set()
        self.targets = []
        self.need_password = False
        self.password = ''
        self.note = None          # (text, color) on the confirm screen
        self.logging_in = False
        self.job = None           # installer.Installer
        self.version = 0

    @property
    def rows(self):
        return self.checker.rows

    def check(self):
        """Reload the registry and installed versions in the background."""
        if self.checking:
            return

        def work():
            try:
                self.checker.check()
            except Exception as e:
                self.checker.error = str(e) or type(e).__name__
            names = {r['package'] for r in self.rows if r['update']}
            self.picked &= names
            self.checking = False
            self._scroll()
            self.version += 1

        self.checking = True
        self.version += 1
        threading.Thread(target=work, daemon=True).start()

    @property
    def items(self):
        return views.list_items(self.rows)

    def _scroll(self):
        n = len(self.items)
        self.sel = max(0, min(self.sel, n - 1))
        self.top = max(0, min(self.top, self.sel, n - views.ROWS))
        if self.sel >= self.top + views.ROWS:
            self.top = self.sel - views.ROWS + 1

    def handle(self, code, hold, now):
        """One key press. Returns True when the frame must be redrawn."""
        if self.view == 'confirm':
            return self._confirm_key(code)
        if self.view == 'progress':
            if self.job.finished and keys.nav(code) in (keys.K_ENTER, keys.K_ESC):
                self.view, self.job = 'list', None
                self.check()
                return True
            return False
        k = keys.nav(code)
        if k == keys.K_ESC:
            hold.press(now)
            return False
        items, n = self.items, len(self.items)
        cur = items[self.sel] if n else {}
        updates = [r for r in self.rows if r['update']]
        if k in (keys.K_UP, keys.K_DOWN) and n:
            self.sel = (self.sel + (-1 if k == keys.K_UP else 1)) % n
            self._scroll()
        elif k == keys.K_SPACE and cur.get('update'):
            self.picked ^= {cur['package']}
        elif k == keys.K_ENTER and n:
            if 'all' in cur:
                targets = updates
            else:
                targets = [r for r in updates if r['package'] in self.picked]
                if not targets and cur.get('update'):
                    targets = [cur]
            if not targets:
                return False
            self._open_confirm(targets)
        elif k == K_A and updates:
            self._open_confirm(updates)
        elif k == K_R:
            self.check()
        else:
            return False
        hold.cancel()
        return True

    def _open_confirm(self, targets):
        self.targets = targets
        self.need_password = not installer.sudo_ready()
        self.password, self.note = '', None
        self.view = 'confirm'

    def _confirm_key(self, code):
        if self.logging_in:
            return False
        base = code % keys.SHIFT
        if base == keys.K_ESC:
            self.view, self.password, self.note = 'list', '', None
        elif base == keys.K_ENTER:
            if not self.need_password:
                self._start(None)
            elif self.password:
                self._login()
        elif base == keys.K_BACKSPACE:
            self.password = self.password[:-1]
        elif self.need_password and keys.to_char(code):
            self.password += keys.to_char(code)
            self.note = None
        else:
            return False
        return True

    def _login(self):
        password = self.password
        self.logging_in, self.note = True, ('checking...', DIM)

        def work():
            ok = installer.sudo_login(password)
            self.logging_in = False
            if ok:
                self._start(password)
            else:
                self.password, self.note = '', ('wrong password', RED)
            self.version += 1

        threading.Thread(target=work, daemon=True).start()

    def _start(self, password):
        self.job = installer.Installer(self.targets, password)
        self.password, self.note, self.picked = '', None, set()
        self.view = 'progress'
        self.job.start()

    def status(self, now):
        c = self.checker
        if self.checking:
            return 'checking...', DIM
        if c.error:
            return c.error[:30], RED
        n = sum(r['update'] for r in self.rows)
        if c.offline:
            return f'offline, {n} new', WARN
        if n:
            return f"{n} update{'s' if n > 1 else ''}", ACCENT
        return ('up to date', GREEN) if c.checked_at else ('', FAINT)

    def render(self, now):
        if self.view == 'confirm':
            return views.render_confirm(self.targets, self.need_password, len(self.password),
                                        self.note)
        if self.view == 'progress':
            return views.render_progress(self.job.jobs, self.job.finished, now)
        c = self.checker
        check = (c.stage, c.pct) if self.checking else None
        empty = ('Could not reach the store - R to retry', RED) if c.error else None
        return views.render_list(self.rows, self.sel, self.top, self.picked, self.status(now),
                                 check, empty)

    def frame_key(self, now):
        """Changes whenever the screen may need redrawing."""
        job = self.job.version if self.job else 0
        tick = int(now) if self.view == 'progress' else int(now // 30)
        return self.version, self.checker.version, job, tick


def to_rgb565(img):
    import numpy as np
    a = np.asarray(img, dtype=np.uint16)
    v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    return v.astype('<u2').tobytes()


def run():
    import signal
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    from hold_exit import ExitHold, bar, BAR_H
    try:
        kbd = os.open(KBD, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        kbd = None
    app = App()
    app.check()
    hold = ExitHold()
    last_key, frame, written, shown_bar = None, None, 0, False
    with open(FB, 'r+b', buffering=0) as fb:
        try:
            while True:
                now = time.time()
                dirty = False
                for code in keys.read_keys(kbd):
                    dirty |= app.handle(code, hold, now)
                listing = app.view == 'list'
                progress = hold.progress(now, keys.is_down(keys.K_ESC)) if listing else None
                if progress is not None and progress >= 1:
                    break
                key = app.frame_key(now)
                changed = dirty or key != last_key or frame is None
                if changed:
                    frame = to_rgb565(app.render(now))
                    last_key = key
                if changed or progress is not None or shown_bar or now - written >= 1:
                    fb.seek(0)
                    if progress is None:
                        fb.write(frame)
                    else:  # the countdown bar replaces the bottom rows
                        fb.write(frame[:(H - BAR_H) * W * 2])
                        fb.write(to_rgb565(bar(progress, W, F_BOLD_SMALL)))
                    written, shown_bar = now, progress is not None
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        finally:
            fb.seek(0)
            fb.write(b'\x00' * (W * H * 2))


def make_icon(path, size=100):
    from PIL import Image, ImageDraw
    from theme import BG
    k = size / 100
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def s(*v):
        return tuple(round(x * k) for x in v)

    d.rounded_rectangle(s(4, 4, 96, 96), radius=round(20 * k), fill=BG + (255,))
    # a down arrow into a tray
    d.rounded_rectangle(s(44, 20, 56, 54), radius=round(3 * k), fill=ACCENT)
    d.polygon([s(30, 50)[:2], s(70, 50)[:2], s(50, 70)[:2]], fill=ACCENT)
    d.line([s(24, 64)[:2], s(24, 80)[:2], s(76, 80)[:2], s(76, 64)[:2]], fill=GREEN,
           width=round(7 * k), joint='curve')
    img.save(path)


def preview_rows():
    return [
        {'package': 'advancewebui', 'title': 'AdvanceWebUI', 'installed': '1.0.2',
         'version': '1.1.0', 'update': True},
        {'package': 'meshterm', 'title': 'MeshTerm', 'installed': '0.11.3-1',
         'version': '0.12.0-1', 'update': True},
        {'package': 'zero-news', 'title': 'News', 'installed': '1.0.0', 'version': '1.0.1',
         'update': True},
        {'package': 'geminilive', 'title': 'Gemini Live', 'installed': '0.1.0-1',
         'version': '0.1.0-1', 'update': False},
        {'package': 'zerosky', 'title': 'ZeroSky', 'installed': '1.0.0', 'version': '1.0.0',
         'update': False},
        {'package': 'muser', 'title': 'Muser', 'installed': '1.0.0', 'version': '1.0.0',
         'update': False},
    ]


def main(argv):
    net.prefer_ipv4()
    if '--check' in argv:
        c = registry.Checker()
        c.check()
        if c.error:
            print(c.error)
        for r in c.rows:
            mark = 'UPDATE' if r['update'] else 'ok'
            print(f"{r['package']:24} {r['installed']:>14} {r['version']:>14}  {mark}")
    elif '--make-icon' in argv:
        make_icon(argv[argv.index('--make-icon') + 1])
    elif '--preview' in argv:
        out = argv[argv.index('--preview') + 1]
        app = App()
        if '--live' in argv:
            app.checker.check()
        else:
            app.checker.rows, app.checker.checked_at = preview_rows(), time.time()
        app.picked = {'advancewebui', 'zero-news'}
        now = time.time()
        if '--checking' in argv:
            app.checking = True
            app.checker._downloaded(131072, 213323)
            if '--empty' in argv:
                app.checker.rows = []
        elif '--confirm' in argv:
            app.targets = [r for r in app.rows if r['update']]
            app.need_password, app.password = True, 'secret'
            app.view = 'confirm'
        elif '--progress' in argv:
            from installer import Installer
            app.job = Installer([r for r in app.rows if r['update']])
            j = app.job.jobs
            j[0].update(state='done', msg=j[0]['app']['version'])
            j[1].update(state='download', pct=0.42)
            if '--finished' in argv:
                j[1].update(state='failed', msg='would remove libcamera0.7')
                j[2].update(state='done', msg=j[2]['app']['version'])
                app.job.finished = True
            app.view = 'progress'
        app.render(now).save(out)
    else:
        run()


if __name__ == '__main__':
    main(sys.argv[1:])
