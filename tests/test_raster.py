"""Tests for the InkML rasterizer, especially the max_px safety clamp."""
import io

import pytest
from PIL import Image

from inkml_raster import rasterize_inkml, MAX_RENDER_PX, MIN_RENDER_PX

SIMPLE_INKML = """
<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML">
  <inkml:definitions>
    <inkml:traceFormat>
      <inkml:channel name="X" type="integer"/>
      <inkml:channel name="Y" type="integer"/>
      <inkml:channel name="F" type="integer"/>
    </inkml:traceFormat>
  </inkml:definitions>
  <inkml:trace>0 0 100, 500 500 100, 1000 300 100</inkml:trace>
  <inkml:trace>200 800 100, 900 900 100</inkml:trace>
</inkml:ink>
"""


def _decode(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def test_rasterize_basic_roundtrip():
    png = rasterize_inkml(SIMPLE_INKML, max_px=800)
    img = _decode(png)
    assert img.format == "PNG"
    assert max(img.size) <= 800


def test_rasterize_clamps_absurd_max_px():
    """A model-supplied max_px of a billion must not allocate a giant canvas."""
    png = rasterize_inkml(SIMPLE_INKML, max_px=10**9)
    img = _decode(png)
    assert max(img.size) <= MAX_RENDER_PX


def test_rasterize_clamps_nonpositive_max_px():
    """Zero/negative max_px must not flip the scaling math negative."""
    png = rasterize_inkml(SIMPLE_INKML, max_px=-100)
    img = _decode(png)
    assert MIN_RENDER_PX // 2 <= max(img.size) <= MIN_RENDER_PX


def test_rasterize_no_traces_raises():
    with pytest.raises(ValueError):
        rasterize_inkml("<ink></ink>")
