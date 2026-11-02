"""PnL card for sharing (STONKS / DTrade style): big character on the left, stats on the right.

Character pool: your own pictures from pnl_images/win and pnl_images/loss (transparent PNGs look best —
e.g. your coins' mascots) plus the built-in sticker characters from characters.py.
"""
from __future__ import annotations

import io
import math
import os
import random

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mpl"))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

import characters  # noqa: E402
import pnl_art  # noqa: E402

W, H = 1280, 853
FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
THEME = {
    True: dict(bg1=(12, 38, 74), bg2=(6, 18, 40), pill=(25, 240, 190), pill_txt=(255, 255, 255), accent=(0, 236, 151),
               logo=(255, 255, 255), pattern=(120, 170, 230)),
    False: dict(bg1=(58, 14, 40), bg2=(20, 8, 30), pill=(255, 77, 109), pill_txt=(255, 255, 255), accent=(255, 159, 28),
                logo=(255, 255, 255), pattern=(230, 120, 160)),
}


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)


def near_mark(d: ImageDraw.ImageDraw, x: float, y: float, s: float, fill, width: int):
    """Simplified NEAR 'N' mark (original drawing, not the official asset)."""
    pts = [(x, y + s), (x, y), (x + s, y + s), (x + s, y)]
    d.line(pts, fill=fill, width=width, joint="curve")


def background(win: bool) -> Image.Image:
    t = THEME[win]
    img = Image.new("RGB", (W, H))
    px = img.load()
    for yy in range(H):
        for xx in range(0, W, 4):
            k = min(1.0, (xx / W) * 0.5 + (yy / H) * 0.5)
            c = tuple(int(t["bg1"][i] * (1 - k) + t["bg2"][i] * k) for i in range(3))
            for dx in range(4):
                if xx + dx < W:
                    px[xx + dx, yy] = c
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    step = 150
    for row, y in enumerate(range(-60, H + 60, step // 1)):
        for x in range(-60 + (row % 2) * step // 2, W + 60, step):
            near_mark(d, x, y, 46, t["pattern"] + (34,), 9)
    img = Image.alpha_composite(img.convert("RGBA"), layer)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((60, 120, 720, 820), fill=t["accent"] + (55,))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    return Image.alpha_composite(img, glow)


def builtin_character(win: bool, rng: random.Random, size: int = 720) -> Image.Image:
    fn = rng.choice(characters.WIN_CHARS if win else characters.LOSS_CHARS)
    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.05, 1.05)
    ax.axis("off")
    ax.set_aspect("equal")
    fn(ax, pnl_art.WIN if win else pnl_art.LOSS)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def character(win: bool, rng: random.Random) -> Image.Image:
    pics = pnl_art.custom_images(win)
    # свои картинки в приоритете: если они есть, встроенные выпадают реже
    if pics and rng.random() < 0.8:
        try:
            return Image.open(rng.choice(pics)).convert("RGBA")
        except Exception:
            pass
    return builtin_character(win, rng)


def fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    k = min(box_w / img.width, box_h / img.height)
    return img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)


def text_c(d, cx, y, s, f, fill):
    w = d.textlength(s, font=f)
    d.text((cx - w / 2, y), s, font=f, fill=fill)


def fit_font(d, s: str, max_w: int, size: int, bold=True) -> ImageFont.FreeTypeFont:
    while size > 18:
        f = font(size, bold)
        if d.textlength(s, font=f) <= max_w:
            return f
        size -= 4
    return font(size, bold)


def pill(d, box, fill, r=34):
    d.rounded_rectangle(box, radius=r, fill=fill)


def coin(d, x, y, r, fill=(0, 0, 0), bg=(255, 255, 255)):
    d.ellipse((x - r, y - r, x + r, y + r), fill=bg)
    near_mark(d, x - r * 0.45, y - r * 0.45, r * 0.9, fill, max(2, int(r * 0.22)))


def fmt_amount(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


def render(symbol: str, pct: float, initial: float, worth: float, hold: str, ref_code: str,
           brand: str = "NDBOT", hide: bool = False, seed: int | None = None) -> bytes:
    rng = random.Random(seed)
    win = pct >= 0
    t = THEME[win]
    img = background(win)
    d = ImageDraw.Draw(img)

    # logo
    d.text((70, 52), brand[:2], font=font(64), fill=t["accent"])
    d.text((70 + d.textlength(brand[:2], font=font(64)), 52), brand[2:], font=font(64), fill=t["logo"])

    # character (left)
    ch = fit(character(win, rng), 600, 660)
    img.alpha_composite(ch, (int(360 - ch.width / 2), int(H - 40 - ch.height)))
    d = ImageDraw.Draw(img)

    # right column
    cx, left, right = 1010, 790, 1230
    tick = f"${symbol.upper()[:10]}"
    text_c(d, cx, 105, tick, fit_font(d, tick, right - left, 92), (255, 255, 255))
    pill(d, (left, 205, right, 310), t["pill"])
    big = f"{pct:+.2f}%" if abs(pct) < 10000 else f"{1 + pct / 100:,.0f}x"
    text_c(d, cx, 222, big, fit_font(d, big, right - left - 40, 70), t["pill_txt"])
    pill(d, (left, 330, right, 418), (255, 255, 255))
    pnl = worth - initial
    pnl_s = "*****" if hide else f"{'+' if pnl >= 0 else '−'}{fmt_amount(abs(pnl))}"
    f = fit_font(d, pnl_s + "  ", right - left - 110, 60)
    w = d.textlength(pnl_s, font=f) + 62
    x0 = cx - w / 2
    d.text((x0, 342), pnl_s, font=f, fill=(15, 20, 35))
    coin(d, x0 + w - 24, 374, 22, fill=(255, 255, 255), bg=(15, 20, 35))

    rows = [("INVESTED", "****" if hide else fmt_amount(initial)), ("WORTH", "****" if hide else fmt_amount(worth))]
    y = 448
    for label, val in rows:
        d.text((left, y), label, font=font(40), fill=(255, 255, 255))
        vw = d.textlength(val, font=font(40))
        d.text((right - 58 - vw, y), val, font=font(40), fill=(255, 255, 255))
        coin(d, right - 22, y + 24, 21, fill=(255, 255, 255), bg=(26, 140, 255))
        y += 62
    if hold:
        d.text((left, y + 4), "HOLD", font=font(28), fill=(200, 210, 230))
        hw = d.textlength(hold, font=font(28))
        d.text((right - hw, y + 4), hold, font=font(28), fill=(200, 210, 230))

    d.text((left, 640), "Referral Code", font=font(28), fill=(255, 255, 255))
    d.text((left, 676), ref_code[:12], font=fit_font(d, ref_code[:12], right - left, 56), fill=(255, 255, 255))
    pill(d, (left, 750, left + 230, 798), (26, 160, 255), r=14)
    text_c(d, left + 115, 758, "Trading Bot", font(28), (255, 255, 255))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()
