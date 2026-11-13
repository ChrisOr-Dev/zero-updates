"""Screen size, colors and fonts shared by all screens."""
import os

from PIL import ImageFont

W, H = 320, 170
FONT_DIRS = ['/usr/share/APPLaunch/share/font', os.path.dirname(os.path.abspath(__file__))]

BG = (12, 15, 22)
FG = (236, 238, 242)
DIM = (130, 140, 158)
FAINT = (70, 80, 98)
LINE = (36, 44, 58)
SEL = (30, 40, 58)
ACCENT = (110, 180, 255)
WARN = (232, 178, 70)
GREEN = (38, 186, 120)
RED = (232, 72, 72)


def font(name, size):
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            # BASIC layout: ASCII-only text renders faster than with raqm, and no ligatures
            return ImageFont.truetype(p, size, layout_engine=ImageFont.Layout.BASIC)
    return ImageFont.load_default(size)


F_TITLE = font('JetBrainsMono-Bold.ttf', 13)
F_TEXT = font('JetBrainsMono-Regular.ttf', 12)
F_BOLD = font('JetBrainsMono-Bold.ttf', 12)
F_SMALL = font('JetBrainsMono-Regular.ttf', 11)
F_TINY = font('JetBrainsMono-Regular.ttf', 10)
F_BOLD_SMALL = font('JetBrainsMono-Bold.ttf', 11)


def clip(d, text, fnt, width):
    if d.textlength(text, font=fnt) <= width:
        return text
    while text and d.textlength(text + '...', font=fnt) > width:
        text = text[:-1]
    return text + '...'
