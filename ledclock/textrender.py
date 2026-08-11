"""Rendering the clock with a real outline font.

Seven-segment glyphs are unbeatable for sharpness but they look like a
calculator.  This renders the time with a proper typeface instead, rasterised
by PIL and blitted to the panel in one bulk operation.

Two details make a font behave like a clock rather than like running text:

*Fixed-width digit cells.*  Most fonts draw "1" much narrower than "8", so
laying the time out normally would make it twitch sideways every minute.  Each
digit is instead centred in a cell as wide as "8", and the colon gets a
narrower cell of its own.

*Antialiasing is optional.*  The panel has 11-bit PWM per channel, so shaded
edge pixels genuinely help curves and diagonals read cleanly from across a
room.  Hard-edged output is a threshold away for anyone who prefers it.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# Sizes we will consider when fitting text to a box.
MIN_SIZE = 6
MAX_SIZE = 200


class ClockFont:
    """An outline font at one pixel size, laid out on a fixed digit pitch."""

    def __init__(self, path: str | Path, size: int, antialias: bool = True):
        self.path = str(path)
        self.size = int(size)
        self.antialias = antialias
        self.font = ImageFont.truetype(self.path, self.size)

        self.ascent, self.descent = self.font.getmetrics()
        self._cell_cache: dict[str, int] = {}
        self._render_cache: dict[tuple, Image.Image] = {}
        self._render_cache_max = 64

        # Vertical extent of a digit, so we can crop tight and centre exactly.
        top, bottom = self._glyph_extent("0123456789")
        self.digit_top = top
        self.digit_height = max(1, bottom - top)

        self.digit_w = self._advance("8")
        self.colon_w = max(1, self._advance(":"))

    # ---------------- metrics ----------------

    def _advance(self, ch: str) -> int:
        cached = self._cell_cache.get(ch)
        if cached is None:
            cached = int(round(self.font.getlength(ch)))
            self._cell_cache[ch] = cached
        return cached

    def _glyph_extent(self, chars: str) -> tuple[int, int]:
        """Top and bottom row of ``chars``, relative to the ascender line."""
        tops, bottoms = [], []
        for ch in chars:
            box = self.font.getbbox(ch)
            if box[3] > box[1]:
                tops.append(box[1])
                bottoms.append(box[3])
        if not tops:
            return 0, self.ascent
        return min(tops), max(bottoms)

    def cell_width(self, ch: str) -> int:
        """Layout pitch for one character."""
        if ch == ":":
            return self.colon_w
        if ch.isdigit():
            return self.digit_w
        return self._advance(ch)

    def measure(self, text: str, tracking: int = 0) -> int:
        if not text:
            return 0
        return sum(self.cell_width(c) for c in text) + tracking * (len(text) - 1)

    # ---------------- rasterising ----------------

    def render(
        self, text: str, rgb: tuple[int, int, int], tracking: int = 0, hide: str = ""
    ) -> Image.Image:
        """Rasterise ``text`` to a tight RGB image, digit-height tall.

        Characters listed in ``hide`` still take up their cell but are not
        drawn.  That is how the colon blinks: substituting a space would use
        the space glyph's advance instead of the colon's, and the minutes
        would visibly shift sideways twice a second.

        Results are cached: the face changes once a minute (twice a second
        with a blinking colon) but the render loop runs at 20 fps, so
        rasterising every frame burned about a third of a core for nothing.
        """
        key = (text, tuple(int(c) for c in rgb), tracking, hide)
        hit = self._render_cache.get(key)
        if hit is not None:
            return hit
        image = self._render(text, rgb, tracking, hide)
        if len(self._render_cache) >= self._render_cache_max:
            # Times march forward, so the oldest entries are the stale ones.
            for old in list(self._render_cache)[: self._render_cache_max // 2]:
                del self._render_cache[old]
        self._render_cache[key] = image
        return image

    def _render(
        self, text: str, rgb: tuple[int, int, int], tracking: int, hide: str = ""
    ) -> Image.Image:
        width = max(1, self.measure(text, tracking))
        # Draw on a full ascent+descent canvas, then crop to the digit band.
        tall = Image.new("L", (width, self.ascent + self.descent), 0)
        draw = ImageDraw.Draw(tall)

        x = 0
        for ch in text:
            cell = self.cell_width(ch)
            if ch not in hide:
                glyph_w = int(round(self.font.getlength(ch)))
                # Centre each glyph in its cell so digits sit on a fixed pitch.
                draw.text((x + (cell - glyph_w) / 2, 0), ch, font=self.font, fill=255)
            x += cell + tracking

        band = tall.crop((0, self.digit_top, width, self.digit_top + self.digit_height))

        if not self.antialias:
            band = band.point(lambda v: 255 if v >= 128 else 0)

        # Tint the coverage mask with the requested colour.
        out = Image.new("RGB", band.size, (0, 0, 0))
        out.paste(tuple(int(c) for c in rgb), (0, 0), band)
        return out


@lru_cache(maxsize=64)
def _load(path: str, size: int, antialias: bool) -> ClockFont | None:
    try:
        return ClockFont(path, size, antialias)
    except Exception as exc:
        log.warning("could not load clock font %s at %dpx: %s", path, size, exc)
        return None


@lru_cache(maxsize=64)
def fit(
    path: str, reference: str, max_w: int, max_h: int,
    antialias: bool = True, tracking: int = 0,
) -> ClockFont | None:
    """Largest size of ``path`` for which ``reference`` fits the given box.

    Sizing against a reference string ("88:88") rather than the live time is
    what stops the clock resizing itself when the hour rolls 9 -> 10.
    """
    if max_w <= 0 or max_h <= 0:
        return None

    best: ClockFont | None = None
    lo, hi = MIN_SIZE, MAX_SIZE
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _load(path, mid, antialias)
        if candidate is None:
            return None
        if candidate.measure(reference, tracking) <= max_w and candidate.digit_height <= max_h:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def available_fonts() -> list[Path]:
    """Every outline font on the system, for `--list-fonts`."""
    roots = [Path("/usr/share/fonts"), Path.home() / ".fonts"]
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            for pattern in ("*.ttf", "*.otf"):
                found.extend(root.rglob(pattern))
    return sorted(set(found))
