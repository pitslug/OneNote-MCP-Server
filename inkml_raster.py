"""InkML -> PNG rasterizer for OneNote ink.

Renders InkML <trace> stroke data to a PNG so a vision model can read handwriting.
OneNote InkML uses absolute integer coordinates in himetric units with channel
order X, Y, F (pressure); this renderer uses X and Y and ignores pressure.

Output is grayscale (line art compresses far smaller than RGB) and capped to a byte
budget (max_bytes): if the encoded PNG exceeds it, the image is progressively
downscaled until it fits, so results stay under MCP's 1MB tool-result limit.
"""
import io
import re
from typing import List, Tuple, Optional

from PIL import Image, ImageDraw

Point = Tuple[float, float]

_TRACE_RE = re.compile(r"<(?:inkml:)?trace\b[^>]*>(.*?)</(?:inkml:)?trace>", re.DOTALL)
_CHANNEL_RE = re.compile(r'<(?:inkml:)?channel\b[^>]*\bname="([^"]+)"', re.IGNORECASE)
_BRUSH_COLOR_RE = re.compile(r'brushProperty\b[^>]*\bname="color"[^>]*\bvalue="(#[0-9A-Fa-f]{6})"')
_BRUSH_WIDTH_RE = re.compile(r'brushProperty\b[^>]*\bname="width"[^>]*\bvalue="([0-9.]+)"')


def _channel_indices(inkml: str) -> Tuple[int, int]:
    names = _CHANNEL_RE.findall(inkml)
    xi = names.index("X") if "X" in names else 0
    yi = names.index("Y") if "Y" in names else 1
    return xi, yi


def parse_traces(inkml: str) -> List[List[Point]]:
    """Extract each trace as a list of (x, y) points."""
    xi, yi = _channel_indices(inkml)
    need = max(xi, yi)
    traces: List[List[Point]] = []
    for body in _TRACE_RE.findall(inkml):
        pts: List[Point] = []
        for chunk in body.split(","):
            vals = chunk.split()
            if len(vals) <= need:
                continue
            try:
                x = float(vals[xi]); y = float(vals[yi])
            except ValueError:
                continue
            pts.append((x, y))
        if pts:
            traces.append(pts)
    return traces


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def rasterize_inkml(
    inkml: str,
    max_px: int = 1200,
    padding: int = 24,
    supersample: int = 2,
    bg: str = "white",
    ink: Optional[str] = None,
    grayscale: bool = True,
    max_bytes: int = 900_000,
) -> bytes:
    """Render InkML to PNG bytes (<= max_bytes). Raises ValueError if there are no strokes."""
    traces = parse_traces(inkml)
    if not traces:
        raise ValueError("no ink traces to rasterize")

    xs = [p[0] for t in traces for p in t]
    ys = [p[1] for t in traces for p in t]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    w = max(max_x - min_x, 1.0)
    h = max(max_y - min_y, 1.0)

    ss = max(1, supersample)
    draw_max = (max_px - 2 * padding) * ss
    scale = draw_max / max(w, h)
    pad = padding * ss
    img_w = int(w * scale + 2 * pad)
    img_h = int(h * scale + 2 * pad)

    if ink is None:
        m = _BRUSH_COLOR_RE.search(inkml)
        ink = m.group(1) if m else "#201F1E"
    mw = _BRUSH_WIDTH_RE.search(inkml)
    brush_himetric = float(mw.group(1)) if mw else 35.0
    lw = max(1, round(brush_himetric * scale))

    img = Image.new("RGB", (img_w, img_h), bg)
    d = ImageDraw.Draw(img)

    def to_px(p: Point) -> Point:
        return ((p[0] - min_x) * scale + pad, (p[1] - min_y) * scale + pad)

    for t in traces:
        px = [to_px(p) for p in t]
        if len(px) == 1:
            x, y = px[0]
            r = lw / 2
            d.ellipse([x - r, y - r, x + r, y + r], fill=ink)
        else:
            d.line(px, fill=ink, width=lw, joint="curve")
            r = lw / 2
            for x, y in (px[0], px[-1]):
                d.ellipse([x - r, y - r, x + r, y + r], fill=ink)

    if ss > 1:
        img = img.resize((max(1, img_w // ss), max(1, img_h // ss)), Image.LANCZOS)
    if grayscale:
        img = img.convert("L")

    png = _encode_png(img)
    # Shrink until under the byte budget (handles dense full pages).
    cw, ch = img.size
    while len(png) > max_bytes and max(cw, ch) > 200:
        cw = int(cw * 0.8); ch = int(ch * 0.8)
        png = _encode_png(img.resize((max(1, cw), max(1, ch)), Image.LANCZOS))
    return png
