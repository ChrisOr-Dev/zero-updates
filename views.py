"""The app list, the confirm/password screen and the progress screen."""
from PIL import Image, ImageDraw

from theme import (ACCENT, BG, DIM, F_BOLD, F_BOLD_SMALL, F_SMALL, F_TEXT, F_TINY, F_TITLE,
                   FAINT, FG, GREEN, H, LINE, RED, SEL, W, WARN, clip)

TOP = 27
ROW_H = 22
ROWS = (H - TOP - 18) // ROW_H
JOB_H = 26
JOB_ROWS = (H - TOP - 18) // JOB_H


def frame(title, right=None, right_color=DIM):
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((8, 5), title, font=F_TITLE, fill=FG)
    if right:
        d.text((W - 8, 6), right, font=F_SMALL, fill=right_color, anchor='ra')
    d.line((0, 23, W, 23), fill=LINE)
    return img, d


def footer(d, text, color=FAINT):
    d.line((0, H - 17, W, H - 17), fill=LINE)
    d.text((W / 2, H - 8), text, font=F_TINY, fill=color, anchor='mm')


def check_mark(d, x, y, color):
    d.line([(x, y + 5), (x + 3, y + 8), (x + 9, y + 1)], fill=color, width=2)


def progress_bar(d, box, pct, color=ACCENT):
    d.rectangle(box, fill=LINE)
    if pct > 0:
        d.rectangle((box[0], box[1], box[0] + (box[2] - box[0]) * min(1, pct), box[3]),
                    fill=color)


def render_list(rows, sel, top, picked, status, check=None, empty=None):
    """rows from registry.scan(); picked: packages ticked for update;
    status: (text, color) for the header's right side; check: (stage, pct) while
    checking; empty: (text, color) for an empty list."""
    img, d = frame('Updates', *status)
    mid = (TOP + H - 17) / 2
    if check and not rows:
        d.text((W / 2, mid - 14), 'Checking for updates', font=F_BOLD, fill=FG, anchor='mm')
        progress_bar(d, (24, mid - 2, W - 24, mid + 3), check[1])
        d.text((W / 2, mid + 16), clip(d, check[0], F_SMALL, W - 24), font=F_SMALL, fill=DIM,
               anchor='mm')
    elif check:
        progress_bar(d, (0, 22, W, 24), check[1])
    elif not rows:
        text, col = empty or ('No store apps installed', DIM)
        d.text((W / 2, mid), clip(d, text, F_TEXT, W - 16), font=F_TEXT, fill=col,
               anchor='mm')
    for i, row in enumerate(rows[top:top + ROWS]):
        y = TOP + i * ROW_H
        if top + i == sel:
            d.rectangle((0, y, W, y + ROW_H - 2), fill=SEL)
        cy = y + ROW_H // 2 - 1
        if row['update']:
            box = (8, cy - 5, 18, cy + 5)
            if row['package'] in picked:
                d.rectangle(box, fill=ACCENT)
                check_mark(d, 9, cy - 5, BG)
            else:
                d.rectangle(box, outline=DIM)
            new = row['version']
            old = row['installed'] + ' > '
            x = W - 8 - d.textlength(new, font=F_BOLD)
            d.text((x, cy), new, font=F_BOLD, fill=ACCENT, anchor='lm')
            x -= d.textlength(old, font=F_SMALL)
            d.text((x, cy), old, font=F_SMALL, fill=DIM, anchor='lm')
        else:
            check_mark(d, 8, cy - 5, GREEN)
            x = W - 8 - d.textlength(row['installed'], font=F_SMALL)
            d.text((x, cy), row['installed'], font=F_SMALL, fill=FAINT, anchor='lm')
        title_col = FG if row['update'] else DIM
        d.text((26, cy), clip(d, row['title'], F_TEXT, x - 34), font=F_TEXT, fill=title_col,
               anchor='lm')
    if len(rows) > ROWS:
        h = (H - TOP - 18) * ROWS / len(rows)
        y0 = TOP + (H - TOP - 18 - h) * top / max(1, len(rows) - ROWS)
        d.rectangle((W - 2, y0, W, y0 + h), fill=FAINT)
    n = sum(r['update'] for r in rows)
    footer(d, 'SPACE pick  ENTER update  A all  R check' if n else
           'Please wait' if check else 'R check again')
    return img


def render_confirm(apps, need_password, typed, note):
    """Before updating: what will change, and the sudo password when needed.
    note: (text, color) or None."""
    img, d = frame(f"Update {len(apps)} app{'s' if len(apps) != 1 else ''}")
    shown = apps[:3] if len(apps) > 4 else apps
    y = TOP + 2
    for a in shown:
        new = a['version']
        x = W - 8 - d.textlength(new, font=F_BOLD_SMALL)
        d.text((x, y), new, font=F_BOLD_SMALL, fill=ACCENT)
        x -= d.textlength(a['installed'] + ' > ', font=F_SMALL)
        d.text((x, y), a['installed'] + ' > ', font=F_SMALL, fill=DIM)
        d.text((8, y), clip(d, a['title'], F_SMALL, x - 16), font=F_SMALL, fill=FG)
        y += 15
    if len(shown) < len(apps):
        d.text((8, y), f'+ {len(apps) - len(shown)} more', font=F_SMALL, fill=DIM)
        y += 15
    y = max(y + 4, 98)
    if need_password:
        d.text((8, y), 'sudo password for pi', font=F_SMALL, fill=DIM)
        box = (8, y + 15, W - 8, y + 34)
        d.rectangle(box, outline=ACCENT, fill=SEL)
        stars = clip(d, '*' * typed, F_TEXT, W - 30)
        d.text((14, (box[1] + box[3]) / 2 + 1), stars, font=F_TEXT, fill=FG, anchor='lm')
        cx = 15 + d.textlength(stars, font=F_TEXT)
        d.line((cx, box[1] + 4, cx, box[3] - 4), fill=ACCENT)
    if note:
        d.text((W - 8, y), note[0], font=F_SMALL, fill=note[1], anchor='ra')
    footer(d, 'ENTER start   ESC cancel')
    return img


def job_status(job, now):
    s = job['state']
    if s == 'waiting':
        return 'waiting', FAINT
    if s == 'download':
        return f"{int(job['pct'] * 100)}%", ACCENT
    if s == 'check':
        return 'checking', ACCENT
    if s == 'install':
        return f"installing {int(now - job['since'])}s", WARN
    if s == 'done':
        return job['msg'], GREEN
    return 'failed', RED


def render_progress(jobs, finished, now):
    done = sum(j['state'] == 'done' for j in jobs)
    failed = sum(j['state'] == 'failed' for j in jobs)
    if finished:
        title = 'Done' if not failed else 'Finished'
        right = (f'{done} updated' + (f', {failed} failed' if failed else ''),
                 RED if failed else GREEN)
    else:
        current = next((i for i, j in enumerate(jobs) if j['state'] not in ('done', 'failed')),
                       len(jobs) - 1)
        title, right = f'Updating {current + 1}/{len(jobs)}', None
    img, d = frame(title, *(right or (None,)))
    active = next((i for i, j in enumerate(jobs) if j['state'] not in ('done', 'failed',
                                                                        'waiting')), 0)
    top = max(0, min(active - JOB_ROWS + 1, len(jobs) - JOB_ROWS)) if not finished else 0
    for i, job in enumerate(jobs[top:top + JOB_ROWS]):
        y = TOP + i * JOB_H
        text, col = job_status(job, now)
        x = W - 8 - d.textlength(text, font=F_BOLD_SMALL)
        if job['state'] == 'done':
            check_mark(d, x - 15, y + 4, GREEN)
            x -= 15
        d.text((W - 8, y + 2), text, font=F_BOLD_SMALL, fill=col, anchor='ra')
        d.text((8, y + 2), clip(d, job['app']['title'], F_TEXT, x - 16), font=F_TEXT,
               fill=FG if job['state'] != 'waiting' else DIM)
        sub_y = y + 17
        if job['state'] == 'download':
            d.rectangle((8, sub_y + 1, W - 8, sub_y + 4), fill=LINE)
            d.rectangle((8, sub_y + 1, 8 + (W - 16) * job['pct'], sub_y + 4), fill=ACCENT)
        elif job['state'] == 'failed':
            d.text((8, sub_y - 2), clip(d, job['msg'], F_TINY, W - 16), font=F_TINY, fill=RED)
        elif job['state'] in ('waiting', 'check', 'install', 'done'):
            a = job['app']
            d.text((8, sub_y - 2), f"{a['installed']} > {a['version']}", font=F_TINY,
                   fill=FAINT)
    footer(d, 'ENTER back' if finished else 'Please wait - keep the power on',
           FAINT if finished else WARN)
    return img
