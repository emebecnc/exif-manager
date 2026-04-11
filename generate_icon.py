"""generate_icon.py — Build icon.ico for EXIF Date Manager.

Run once:  python generate_icon.py
Outputs:   icon.ico  (multi-size: 16, 32, 48, 64, 128, 256 px)

Design:
  • Dark gradient background  #1a1a2e → #16213e
  • Calendar card with cyan header and grid
  • Camera-lens aperture in card centre
  • "EXIF" bold label at bottom of card
  • Accent colour: #00d4ff
"""
from __future__ import annotations

import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


# ── Colours ─────────────────────────────────────────────────────────────────
BG_TOP    = (26,  26,  46)      # #1a1a2e
BG_BOT    = (22,  33,  62)      # #16213e
CARD      = (15,  52,  96)      # #0f3460
ACCENT    = (0,  212, 255)      # #00d4ff
WHITE     = (255, 255, 255)
DARK_GREY = (120, 120, 140)
BLACK     = (0,   0,   0)


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _lerp_rgb(c1: tuple, c2: tuple, t: float) -> tuple:
    return (_lerp(c1[0], c2[0], t), _lerp(c1[1], c2[1], t), _lerp(c1[2], c2[2], t))


def _try_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "arialbd.ttf",          # Arial Bold  (Windows)
        "arial.ttf",
        "DejaVuSans-Bold.ttf",  # Linux fallback
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def draw_icon(size: int) -> Image.Image:
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s    = size / 256.0          # scale factor relative to 256-px master

    # ── Background gradient ────────────────────────────────────────────────
    for y in range(size):
        color = _lerp_rgb(BG_TOP, BG_BOT, y / max(size - 1, 1))
        draw.line([(0, y), (size - 1, y)], fill=color + (255,))

    # ── Calendar card ─────────────────────────────────────────────────────
    m     = int(28 * s)                      # outer margin
    r_cal = max(2, int(14 * s))             # corner radius
    cal   = [m, int(24 * s), size - m, size - int(24 * s)]
    draw.rounded_rectangle(cal, radius=r_cal, fill=CARD + (255,))

    # Header bar (cyan, covers top of card)
    hdr_bot = int(82 * s)
    hdr = [m, int(24 * s), size - m, hdr_bot]
    draw.rounded_rectangle(hdr, radius=r_cal, fill=ACCENT + (255,))
    # Square off the bottom of the header so it meets the card body cleanly
    draw.rectangle([m, hdr_bot - r_cal, size - m, hdr_bot], fill=ACCENT + (255,))

    # Calendar binder rings (two dark circles above the card top)
    ring_y = int(22 * s)
    ring_r = max(3, int(7 * s))
    for rx in (int(88 * s), size - int(88 * s)):
        draw.ellipse(
            [rx - ring_r, ring_y - ring_r, rx + ring_r, ring_y + ring_r],
            fill=DARK_GREY + (255,),
        )

    # Month label in header (thin white text, tiny sizes skip this)
    if size >= 48:
        lbl_font = _try_font(max(8, int(16 * s)))
        lbl = "EXIF"
        bb  = draw.textbbox((0, 0), lbl, font=lbl_font)
        lw  = bb[2] - bb[0]
        lh  = bb[3] - bb[1]
        lx  = (size - lw) // 2
        ly  = int(24 * s) + (hdr_bot - int(24 * s) - lh) // 2
        draw.text((lx, ly), lbl, font=lbl_font, fill=WHITE + (255,))

    # ── Subtle grid lines on calendar body ───────────────────────────────
    if size >= 32:
        grid_top  = hdr_bot + int(4 * s)
        grid_bot  = size - int(28 * s)
        grid_rows = 3
        grid_cols = 3
        row_h = (grid_bot - grid_top) // (grid_rows + 1)
        col_w = (size - 2 * m)       // (grid_cols + 1)
        gc    = ACCENT + (45,)        # very transparent accent
        lw_g  = max(1, int(1 * s))
        for r in range(1, grid_rows + 1):
            gy = grid_top + r * row_h
            draw.line([(m + 4, gy), (size - m - 4, gy)], fill=gc, width=lw_g)
        for c in range(1, grid_cols + 1):
            gx = m + c * col_w
            draw.line([(gx, grid_top), (gx, grid_bot)], fill=gc, width=lw_g)

    # ── Camera-lens aperture centred on card body ─────────────────────────
    cx  = size // 2
    cy  = int(148 * s)
    r_o = max(4, int(34 * s))    # outer ring radius
    r_i = max(2, int(20 * s))    # inner circle radius
    r_h = max(1, int(7 * s))     # highlight dot radius
    lw_lens = max(2, int(3 * s))

    # Outer ring
    draw.ellipse(
        [cx - r_o, cy - r_o, cx + r_o, cy + r_o],
        outline=ACCENT + (255,), width=lw_lens,
    )
    # Filled inner circle
    draw.ellipse(
        [cx - r_i, cy - r_i, cx + r_i, cy + r_i],
        fill=ACCENT + (210,),
    )
    # Specular highlight dot (upper-left quadrant)
    hx = cx - int(r_i * 0.35)
    hy = cy - int(r_i * 0.35)
    draw.ellipse(
        [hx - r_h, hy - r_h, hx + r_h, hy + r_h],
        fill=WHITE + (200,),
    )

    # ── Aperture blades (6-blade iris) ───────────────────────────────────
    if size >= 64:
        blade_color = DARK_GREY + (120,)
        blade_len   = int(r_o * 0.55)
        blade_w     = max(1, int(2 * s))
        for i in range(6):
            angle = math.radians(i * 60)
            x1 = cx + int(r_i * 0.6 * math.cos(angle))
            y1 = cy + int(r_i * 0.6 * math.sin(angle))
            x2 = cx + int(r_o * 0.82 * math.cos(angle))
            y2 = cy + int(r_o * 0.82 * math.sin(angle))
            draw.line([(x1, y1), (x2, y2)], fill=blade_color, width=blade_w)

    return img


def make_ico(out_path: Path) -> None:
    sizes  = [256, 128, 64, 48, 32, 16]
    # Pillow's ICO writer auto-downscales from the base image when given a
    # 'sizes' list.  We draw each size explicitly for crisp pixel-art at
    # small dimensions, then stitch them by converting each to RGBA first.
    base   = draw_icon(256)   # largest master

    # Build resized versions from the master for Pillow's ICO stitcher
    ico_imgs = []
    for sz in sizes:
        if sz == 256:
            ico_imgs.append(base.copy())
        else:
            ico_imgs.append(base.resize((sz, sz), Image.LANCZOS))

    # Save: Pillow stitches all frames when you pass the list via append_images
    # and set sizes to match.  The trick: save as RGBA PNG-inside-ICO.
    ico_imgs[0].save(
        out_path,
        format="ICO",
        sizes=[(sz, sz) for sz in sizes],
        append_images=ico_imgs[1:],
    )
    # Verify
    from PIL import IcoImagePlugin
    with open(out_path, "rb") as fh:
        ico = IcoImagePlugin.IcoFile(fh)
        found = sorted({s[0] for s in ico.sizes()}, reverse=True)
    print(f"Saved {out_path}  —  sizes in file: {found}")


if __name__ == "__main__":
    dest = Path(__file__).parent / "icon.ico"
    make_ico(dest)
