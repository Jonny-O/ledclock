"""Drawing backends.

The layout code in :mod:`ledclock.display` is pure geometry and talks only to
this small interface, so the identical layout can be sent either to the real
HUB75 panel or to a PNG for design work over SSH.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]


class Backend:
    """Pixel operations the renderer needs.  Coordinates are pixel-exact."""

    width: int
    height: int

    def load_font(self, path: Path) -> Any | None: ...
    def font_height(self, font: Any) -> int: ...
    def font_baseline(self, font: Any) -> int: ...
    def char_width(self, font: Any, code: int) -> int: ...
    def clear(self) -> None: ...
    def line(self, x0: int, y0: int, x1: int, y1: int, rgb: RGB) -> None: ...
    def text(self, font: Any, x: int, baseline: int, rgb: RGB, s: str) -> int: ...
    def blit(self, image: Any, x: int, y: int) -> None:
        """Paste an RGB image. Overwrites the rectangle, so draw it first."""
        ...
    def present(self) -> None: ...
    def set_brightness(self, value: int) -> None: ...
    def close(self) -> None: ...


def _crop_to_canvas(image, x: int, y: int, width: int, height: int):
    """Clip an image to the canvas.  SetImage rejects out-of-bounds writes."""
    left = max(0, -x)
    top = max(0, -y)
    right = min(image.width, width - x)
    bottom = min(image.height, height - y)
    if right <= left or bottom <= top:
        return None, 0, 0
    return image.crop((left, top, right, bottom)), x + left, y + top


class MatrixBackend(Backend):
    """Drives the physical panel through rpi-rgb-led-matrix."""

    def __init__(self, cfg: Config):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

        self._graphics = graphics
        self.matrix = RGBMatrix(options=self._options(cfg, RGBMatrixOptions))
        self.canvas = self.matrix.CreateFrameCanvas()
        self.width = self.canvas.width
        self.height = self.canvas.height
        self._colors: dict[RGB, Any] = {}

    @staticmethod
    def _options(cfg: Config, RGBMatrixOptions):
        m = cfg.section("matrix")
        o = RGBMatrixOptions()
        o.rows = int(m.get("rows", 64))
        o.cols = int(m.get("cols", 128))
        o.chain_length = int(m.get("chain_length", 1))
        o.parallel = int(m.get("parallel", 1))
        o.hardware_mapping = m.get("hardware_mapping", "regular")
        o.brightness = int(m.get("brightness", 60))
        o.pwm_bits = int(m.get("pwm_bits", 11))
        o.pwm_lsb_nanoseconds = int(m.get("pwm_lsb_nanoseconds", 130))
        o.pwm_dither_bits = int(m.get("pwm_dither_bits", 0))
        o.gpio_slowdown = int(m.get("gpio_slowdown", 4))
        o.scan_mode = int(m.get("scan_mode", 0))
        o.row_address_type = int(m.get("row_address_type", 0))
        o.multiplexing = int(m.get("multiplexing", 0))
        o.pixel_mapper_config = m.get("pixel_mapper", "")
        o.disable_hardware_pulsing = bool(m.get("disable_hardware_pulsing", False))
        o.show_refresh_rate = bool(m.get("show_refresh_rate", False))
        o.limit_refresh_rate_hz = int(m.get("limit_refresh_rate_hz", 0))
        o.inverse_colors = bool(m.get("inverse_colors", False))
        o.led_rgb_sequence = m.get("led_rgb_sequence", "RGB")
        o.panel_type = m.get("panel_type", "")
        # Kept false so the button/buzzer GPIO claims and the state-file write
        # still work once the matrix has initialised.
        o.drop_privileges = bool(m.get("drop_privileges", False))
        return o

    def _color(self, rgb: RGB):
        key = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        c = self._colors.get(key)
        if c is None:
            c = self._graphics.Color(*key)
            self._colors[key] = c
        return c

    def load_font(self, path: Path):
        try:
            font = self._graphics.Font()
            font.LoadFont(str(path))
            return font
        except Exception as exc:
            log.warning("could not load font %s: %s", path, exc)
            return None

    def font_height(self, font) -> int:
        return font.height

    def font_baseline(self, font) -> int:
        return font.baseline

    def char_width(self, font, code: int) -> int:
        return font.CharacterWidth(code)

    def clear(self) -> None:
        self.canvas.Clear()

    def line(self, x0, y0, x1, y1, rgb) -> None:
        self._graphics.DrawLine(self.canvas, x0, y0, x1, y1, self._color(rgb))

    def text(self, font, x, baseline, rgb, s) -> int:
        return self._graphics.DrawText(self.canvas, font, x, baseline, self._color(rgb), s)

    def blit(self, image, x: int, y: int) -> None:
        # SetImage is a C-level bulk copy — orders of magnitude faster than
        # per-pixel calls for the several thousand pixels a large clock covers.
        if x < 0 or y < 0 or x + image.width > self.width or y + image.height > self.height:
            image, x, y = _crop_to_canvas(image, x, y, self.width, self.height)
            if image is None:
                return
        self.canvas.SetImage(image, x, y)

    def present(self) -> None:
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def set_brightness(self, value: int) -> None:
        self.matrix.brightness = max(1, min(100, int(value)))

    def close(self) -> None:
        self.matrix.Clear()


class PreviewBackend(Backend):
    """Renders to a PIL image instead of hardware.

    Used by ``--preview`` so the layout can be checked, and colours tuned,
    without the panel connected.
    """

    def __init__(self, cfg: Config, scale: int = 6, grid: bool = True):
        from PIL import Image

        self.width = int(cfg.get("matrix.cols", 128)) * int(cfg.get("matrix.chain_length", 1))
        self.height = int(cfg.get("matrix.rows", 64)) * int(cfg.get("matrix.parallel", 1))
        self.scale = scale
        self.grid = grid
        self._Image = Image
        self.image = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        self.pixels = self.image.load()
        self.brightness = int(cfg.get("matrix.brightness", 60))

    def load_font(self, path: Path):
        from .bdf import BDFFont

        try:
            return BDFFont(path)
        except Exception as exc:
            log.warning("could not load font %s: %s", path, exc)
            return None

    def font_height(self, font) -> int:
        return font.height

    def font_baseline(self, font) -> int:
        return font.baseline

    def char_width(self, font, code: int) -> int:
        return font.CharacterWidth(code)

    def clear(self) -> None:
        self.image.paste((0, 0, 0), (0, 0, self.width, self.height))
        self.pixels = self.image.load()

    def _set(self, x: int, y: int, rgb: RGB) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self.pixels[x, y] = (int(rgb[0]), int(rgb[1]), int(rgb[2]))

    def line(self, x0, y0, x1, y1, rgb) -> None:
        if y0 == y1:
            for x in range(min(x0, x1), max(x0, x1) + 1):
                self._set(x, y0, rgb)
        elif x0 == x1:
            for y in range(min(y0, y1), max(y0, y1) + 1):
                self._set(x0, y, rgb)
        else:  # Bresenham; the renderer only draws axis-aligned lines today.
            dx, dy = abs(x1 - x0), -abs(y1 - y0)
            sx = 1 if x0 < x1 else -1
            sy = 1 if y0 < y1 else -1
            err = dx + dy
            while True:
                self._set(x0, y0, rgb)
                if x0 == x1 and y0 == y1:
                    break
                e2 = 2 * err
                if e2 >= dy:
                    err += dy
                    x0 += sx
                if e2 <= dx:
                    err += dx
                    y0 += sy

    def text(self, font, x, baseline, rgb, s) -> int:
        cursor = x
        for ch in s:
            for px, py in font.pixels(ord(ch), cursor, baseline):
                self._set(px, py, rgb)
            cursor += font.CharacterWidth(ord(ch))
        return cursor - x

    def blit(self, image, x: int, y: int) -> None:
        cropped, x, y = _crop_to_canvas(image, x, y, self.width, self.height)
        if cropped is None:
            return
        self.image.paste(cropped, (x, y))
        self.pixels = self.image.load()

    def present(self) -> None:
        pass

    def set_brightness(self, value: int) -> None:
        self.brightness = max(1, min(100, int(value)))

    def save(self, path: str | Path) -> Path:
        """Write the frame out, upscaled with a subtle LED grid."""
        img = self.image.resize(
            (self.width * self.scale, self.height * self.scale), self._Image.NEAREST
        )
        if self.grid and self.scale >= 4:
            px = img.load()
            for y in range(img.height):
                for x in range(img.width):
                    if x % self.scale == 0 or y % self.scale == 0:
                        r, g, b = px[x, y]
                        px[x, y] = (r // 3, g // 3, b // 3)
        path = Path(path)
        img.save(path)
        return path

    def close(self) -> None:
        pass
