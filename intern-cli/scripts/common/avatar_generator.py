"""Intern 群头像生成器（task203）。

生成 240×240 PNG：品牌色底 + 上部矢量图标 + 下部类型文字。
飞书头像显示为圆形，图标和文字均在圆内安全区。

- Claude  = 橙色 #D97757 + robot
- Codex   = 绿色 #10A37F + rocket
- Copilot = 蓝色 #1F6FEB + plane

调用 `render_png_bytes(intern_type)` 即可拿到 PNG 二进制。
只在实际需要生成时才 import PIL。
"""
import hashlib
import io

SIZE = 240
CENTER = SIZE // 2
ICON_CY = 95
LABEL_CY = 192
LABEL_SIZE = 26
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

THEME = {
    "claude":  {"bg": "#D97757", "label": "CLAUDE"},
    "copilot": {"bg": "#1F6FEB", "label": "COPILOT"},
    "codex":   {"bg": "#10A37F", "label": "CODEX"},
}
FG = "#FFFFFF"
SUPPORTED_TYPES = tuple(THEME.keys())


def _cutouts(img, shapes):
    from PIL import ImageDraw
    bg = img.getpixel((0, 0))
    d = ImageDraw.Draw(img)
    for kind, *args in shapes:
        if kind == "ellipse":
            d.ellipse(args[0], fill=bg)
        elif kind == "rounded":
            d.rounded_rectangle(args[0], radius=args[1], fill=bg)


def _draw_claude(img):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    cx, cy = CENTER, ICON_CY
    d.line([(cx, cy - 58), (cx, cy - 42)], fill=FG, width=5)
    d.ellipse([cx - 8, cy - 68, cx + 8, cy - 52], fill=FG)
    d.rounded_rectangle([cx - 46, cy - 42, cx + 46, cy + 42], radius=16, fill=FG)
    d.rounded_rectangle([cx - 54, cy - 22, cx - 46, cy + 12], radius=4, fill=FG)
    d.rounded_rectangle([cx + 46, cy - 22, cx + 54, cy + 12], radius=4, fill=FG)
    _cutouts(img, [
        ("ellipse", [cx - 26, cy - 22, cx - 10, cy - 6]),
        ("ellipse", [cx + 10, cy - 22, cx + 26, cy - 6]),
        ("rounded", [cx - 22, cy + 6, cx + 22, cy + 22], 6),
    ])


def _draw_codex(img):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    cx, cy = CENTER, ICON_CY
    body_l, body_r = cx - 18, cx + 18
    body_t, body_b = cy - 34, cy + 34
    d.rounded_rectangle([body_l, body_t, body_r, body_b], radius=14, fill=FG)
    d.polygon([(cx, cy - 62), (body_l, cy - 28), (body_r, cy - 28)], fill=FG)
    d.polygon([(body_l, cy + 12), (body_l - 22, cy + 42), (body_l, cy + 42)], fill=FG)
    d.polygon([(body_r, cy + 12), (body_r + 22, cy + 42), (body_r, cy + 42)], fill=FG)
    _cutouts(img, [("ellipse", [cx - 10, cy - 20, cx + 10, cy])])
    d.polygon([(cx - 11, cy + 36), (cx, cy + 62), (cx + 11, cy + 36)], fill=FG)


def _draw_copilot(img):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    cx, cy = CENTER, ICON_CY
    d.rounded_rectangle([cx - 11, cy - 42, cx + 11, cy + 30], radius=11, fill=FG)
    d.ellipse([cx - 11, cy - 52, cx + 11, cy - 32], fill=FG)
    d.polygon([(cx - 11, cy - 10), (cx - 62, cy + 16),
               (cx - 62, cy + 26), (cx - 11, cy + 10)], fill=FG)
    d.polygon([(cx + 11, cy - 10), (cx + 62, cy + 16),
               (cx + 62, cy + 26), (cx + 11, cy + 10)], fill=FG)
    d.polygon([(cx - 10, cy + 22), (cx - 30, cy + 38),
               (cx - 30, cy + 44), (cx - 10, cy + 32)], fill=FG)
    d.polygon([(cx + 10, cy + 22), (cx + 30, cy + 38),
               (cx + 30, cy + 44), (cx + 10, cy + 32)], fill=FG)


_ICON_FNS = {"claude": _draw_claude, "codex": _draw_codex, "copilot": _draw_copilot}


def _draw_label(img, text):
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT_BOLD, LABEL_SIZE)
    bbox = f.getbbox(text)
    tw = bbox[2] - bbox[0]
    x = (SIZE - tw) // 2
    d.text((x + 1, LABEL_CY + 1), text, font=f, fill="#00000040")
    d.text((x, LABEL_CY), text, font=f, fill=FG)


def render_image(intern_type):
    from PIL import Image
    if intern_type not in _ICON_FNS:
        raise ValueError(f"unsupported intern_type: {intern_type!r}; want one of {SUPPORTED_TYPES}")
    theme = THEME[intern_type]
    img = Image.new("RGB", (SIZE, SIZE), theme["bg"])
    _ICON_FNS[intern_type](img)
    _draw_label(img, theme["label"])
    return img


def render_png_bytes(intern_type):
    """Render to PNG bytes. Returns (bytes, sha256_hex)."""
    img = render_image(intern_type)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    import os
    out = os.environ.get("OUT_DIR", "/tmp/task203_avatars")
    os.makedirs(out, exist_ok=True)
    for t in SUPPORTED_TYPES:
        data, sha = render_png_bytes(t)
        p = os.path.join(out, f"{t}.png")
        with open(p, "wb") as f:
            f.write(data)
        print(f"{t:8s} {p}  sha={sha[:12]}  {len(data)} bytes")
