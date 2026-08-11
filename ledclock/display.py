"""Layout and rendering.

The main clock is drawn with a parametric seven-segment renderer rather than a
bitmap font.  That keeps the digits razor-sharp at any size — the whole point
being readability across a room — and lets the same code drive both the
full-screen clock and the quarter-height compact clock without needing a
separate font for every size.

Entries below the clock use the BDF fonts shipped with rpi-rgb-led-matrix,
picking the largest one from the ladder that fits the number of rows.

All drawing goes through a :class:`~ledclock.backends.Backend`, so this module
never imports the matrix library and can render to a PNG for design work.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .backends import RGB, Backend
from .config import Config
from .entries import Entry, EntryState

log = logging.getLogger(__name__)

# Which of the seven segments light up for each character.
#   a=top  b=top-right  c=bottom-right  d=bottom  e=bottom-left
#   f=top-left  g=middle
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcfgd",
    " ": "", "-": "g",
}


@dataclass
class SegMetrics:
    """Geometry for one run of seven-segment glyphs."""

    digit_w: int
    digit_h: int
    thickness: int
    colon_w: int
    gap: int


def seg_metrics(digit_h: int, aspect: float = 0.55) -> SegMetrics:
    # ~1/8 of the height is what real seven-segment displays use; heavier than
    # that and the bars stop reading as strokes and start reading as blocks.
    thickness = max(1, round(digit_h * 0.13))
    digit_w = max(thickness * 3 + 2, round(digit_h * aspect))
    return SegMetrics(
        digit_w=digit_w,
        digit_h=digit_h,
        thickness=thickness,
        colon_w=max(thickness, round(digit_w * 0.34)),
        gap=max(1, round(digit_w * 0.18)),
    )


def seg_width(text: str, m: SegMetrics) -> int:
    if not text:
        return 0
    total = 0
    for ch in text:
        total += m.colon_w if ch == ":" else m.digit_w
        total += m.gap
    return total - m.gap


def fit_seven_segment(text: str, max_w: int, max_h: int) -> SegMetrics:
    """Largest glyph size for which ``text`` fits inside ``max_w`` x ``max_h``."""
    for height in range(max(5, max_h), 4, -1):
        m = seg_metrics(height)
        if seg_width(text, m) <= max_w:
            return m
    return seg_metrics(5)


class Renderer:
    """Owns the fonts and composes each frame."""

    def __init__(self, cfg: Config, backend: Backend):
        self.cfg = cfg
        self.backend = backend
        self.width = backend.width
        self.height = backend.height

        font_dir = cfg.expand("display.font_dir")
        self.entry_fonts = [
            f for f in (backend.load_font(font_dir / n)
                        for n in cfg.get("display.entry_fonts", []))
            if f is not None
        ]
        self.small_font = backend.load_font(font_dir / cfg.get("display.small_font", "4x6.bdf"))
        if self.small_font is None and self.entry_fonts:
            self.small_font = self.entry_fonts[-1]
        if not self.entry_fonts and self.small_font is None:
            log.warning("no fonts loaded from %s; only the clock digits will draw", font_dir)

        # Outline fonts for the time itself; empty falls back to the
        # seven-segment renderer.  The compact clock gets its own face because
        # it is capped at a quarter of the panel height and so has width to
        # spare — a wider face reads better there than the full-screen choice.
        self._clock_font_path = self._resolve_font(cfg, "display.clock_font")
        self._compact_font_path = (
            self._resolve_font(cfg, "display.compact_clock_font") or self._clock_font_path
        )
        self._clock_antialias = bool(cfg.get("display.clock_antialias", True))
        self._compact_tracking = int(cfg.get("display.compact_clock_tracking", 0))

        self.brightness = int(cfg.get("matrix.brightness", 60))

    # ---------------- primitives ----------------

    def set_brightness(self, value: int) -> None:
        self.brightness = max(1, min(100, int(value)))
        self.backend.set_brightness(self.brightness)

    def _fill(self, x: int, y: int, w: int, h: int, rgb: RGB) -> None:
        if w <= 0 or h <= 0:
            return
        for row in range(y, y + h):
            if 0 <= row < self.height:
                self.backend.line(x, row, x + w - 1, row, rgb)

    def text_width(self, font, text: str) -> int:
        if font is None:
            return 0
        return sum(self.backend.char_width(font, ord(c)) for c in text)

    def draw_text(self, font, x: int, baseline: int, rgb: RGB, text: str) -> int:
        if font is None:
            return 0
        return self.backend.text(font, x, baseline, rgb, text)

    # ---------------- seven segment ----------------

    def draw_digit(self, ch: str, x: int, y: int, m: SegMetrics, rgb: RGB) -> None:
        on = SEGMENTS.get(ch)
        if not on:
            return
        t, w, h = m.thickness, m.digit_w, m.digit_h
        mid = (h - t) // 2
        # The verticals run the full corner-to-corner span and the horizontals
        # bridge between them, so adjacent segments overlap at the corners and
        # the glyph reads as one connected shape rather than loose bars.
        if "a" in on:
            self._fill(x + t, y, w - 2 * t, t, rgb)
        if "g" in on:
            self._fill(x + t, y + mid, w - 2 * t, t, rgb)
        if "d" in on:
            self._fill(x + t, y + h - t, w - 2 * t, t, rgb)
        if "f" in on:
            self._fill(x, y, t, mid + t, rgb)
        if "b" in on:
            self._fill(x + w - t, y, t, mid + t, rgb)
        if "e" in on:
            self._fill(x, y + mid, t, h - mid, rgb)
        if "c" in on:
            self._fill(x + w - t, y + mid, t, h - mid, rgb)

    def draw_seg_text(
        self, text: str, x: int, y: int, m: SegMetrics, rgb: RGB, colon_on: bool = True
    ) -> int:
        """Draw a seven-segment string; returns the x just past its last glyph."""
        for ch in text:
            if ch == ":":
                if colon_on:
                    size = m.thickness
                    cx = x + (m.colon_w - size) // 2
                    self._fill(cx, y + m.digit_h // 3 - size // 2, size, size, rgb)
                    self._fill(cx, y + 2 * m.digit_h // 3 - size // 2, size, size, rgb)
                x += m.colon_w + m.gap
            else:
                self.draw_digit(ch, x, y, m, rgb)
                x += m.digit_w + m.gap
        return x - m.gap

    # ---------------- frame ----------------

    def render(self, entries: list[Entry], status: dict) -> None:
        cfg = self.cfg
        self.backend.clear()
        now = datetime.now()

        visible = entries[: int(cfg.get("entries.max_visible", 8))]
        toast = status.get("toast")
        toast_h = (self.backend.font_height(self.small_font) + 1) if (toast and self.small_font) else 0
        usable_h = self.height - toast_h

        if visible:
            clock_h = max(10, int(usable_h * float(cfg.get("display.compact_clock_fraction", 0.25))))
            self._draw_clock(now, 0, 0, self.width, clock_h, compact=True)
            self.backend.line(0, clock_h, self.width - 1, clock_h, (28, 28, 28))
            self._draw_entries(visible, now, clock_h + 2, usable_h - clock_h - 2)
        else:
            self._draw_clock(now, 0, 0, self.width, usable_h, compact=False)

        if toast:
            self._draw_toast(toast, status.get("toast_color", (200, 120, 255)))
        if status.get("listening"):
            self._fill(self.width - 3, 0, 3, 3, cfg.color("display.heard_color"))

        self.backend.present()

    def _draw_clock(self, now: datetime, x: int, y: int, w: int, h: int, compact: bool) -> None:
        cfg = self.cfg
        hour_format = int(cfg.get("clock.hour_format", 12))
        show_seconds = bool(cfg.get("clock.show_seconds", False))
        show_date = bool(cfg.get("clock.show_date", True)) and not compact
        colon_on = (not cfg.get("clock.blink_colon", True)) or (now.microsecond < 500_000)

        hour = now.hour if hour_format == 24 else (now.hour % 12 or 12)
        if hour_format == 24 or cfg.get("clock.leading_zero", False):
            text = f"{hour:02d}:{now.minute:02d}"
        else:
            text = f"{hour}:{now.minute:02d}"
        if show_seconds:
            text += f":{now.second:02d}"

        suffix = "" if hour_format == 24 else ("AM" if now.hour < 12 else "PM")
        clock_rgb = cfg.color("display.clock_color")
        date_rgb = cfg.color("display.date_color")

        small_h = self.backend.font_height(self.small_font) if self.small_font else 0
        date_h = (small_h + 1) if show_date else 0

        # A bar under the time showing how much of the minute is left. It
        # replaces the blinking colon: it carries strictly more information
        # and, unlike a blink, nothing on screen moves.
        bar_on = bool(cfg.get("clock.seconds_bar", True))
        bar_h = max(1, int(cfg.get("display.seconds_bar_height", 2)))
        if compact:
            bar_h = min(bar_h, 1)
        bar_gap = max(0, int(cfg.get("display.seconds_bar_gap", 2)))
        bar_total = (bar_h + bar_gap) if bar_on else 0

        box_h = h - date_h - bar_total

        # In compact mode AM/PM sits inline beside the digits, so its width has
        # to come out of the clock's budget.  Full screen it drops to the date
        # row instead, which buys the digits the full panel width — and width,
        # not height, is what limits digit size on a 128x64 panel.
        inline_suffix = compact and bool(suffix) and self.small_font is not None
        suffix_w = (self.text_width(self.small_font, suffix) + 3) if inline_suffix else 0

        # Size against the widest time this format can ever produce, not the
        # current one — otherwise the digits visibly resize at 9:59 -> 10:00
        # and again at 12:59 -> 1:00.
        reference = "88:88" + (":88" if show_seconds else "")
        avail_w = w - 2 - suffix_w
        tracking = self._compact_tracking if compact else 0
        clock_font = self._clock_font(reference, avail_w, box_h - 2, compact)
        if clock_font is not None:
            digit_h = clock_font.digit_height
            tw = clock_font.measure(text, tracking)
            tx = x + max(0, (w - (tw + suffix_w)) // 2)
            ty = y + max(0, (box_h - digit_h) // 2)
            # Hiding the colon keeps its cell, so the minutes never shift.
            hide = "" if colon_on else ":"
            self.backend.blit(clock_font.render(text, clock_rgb, tracking, hide), tx, ty)
            end_x = tx + tw
        else:
            m = fit_seven_segment(reference, avail_w, box_h - 2)
            digit_h = m.digit_h
            tw = seg_width(text, m)
            tx = x + max(0, (w - (tw + suffix_w)) // 2)
            ty = y + max(0, (box_h - digit_h) // 2)
            end_x = self.draw_seg_text(text, tx, ty, m, clock_rgb, colon_on)

        if bar_on and tw > 0:
            bar_y = min(ty + digit_h + bar_gap, y + h - date_h - bar_h)
            self._draw_seconds_dots(tx + tw // 2, bar_y, bar_h, now.second, compact)

        if inline_suffix:
            baseline = min(ty + digit_h, y + box_h - 1)
            self.draw_text(self.small_font, end_x + 3, baseline, clock_rgb, suffix)

        if self.small_font and not compact:
            baseline = y + h - 1
            if show_date:
                label = now.strftime("%a %b %-d")
                dw = self.text_width(self.small_font, label)
                self.draw_text(self.small_font, x + (w - dw) // 2, baseline, date_rgb, label)
            if suffix:
                sw = self.text_width(self.small_font, suffix)
                self.draw_text(self.small_font, x + w - sw - 1, baseline, clock_rgb, suffix)

    def _draw_seconds_dots(
        self, center_x: int, y: int, height: int, second: int, compact: bool
    ) -> None:
        """One dot per second, in six groups of ten.

        Grouping is what makes it readable as a count rather than a vague
        gauge: you read the lit groups in tens and the remainder in ones,
        instead of estimating a fraction of a bar.  Dots therefore step once a
        second rather than creeping — a dot means a whole second.
        """
        if height <= 0:
            return
        cfg = self.cfg
        per_group = int(cfg.get("display.seconds_bar_group_size", 10))
        groups = max(1, 60 // max(1, per_group))
        dot_w = max(1, int(cfg.get("display.seconds_bar_dot_width", 1)))
        dot_gap = max(0, int(cfg.get("display.seconds_bar_dot_gap", 0)))
        group_gap = max(1, int(cfg.get("display.seconds_bar_group_gap", 2)))
        if compact:
            group_gap = max(1, group_gap - 1)

        group_w = per_group * dot_w + (per_group - 1) * dot_gap
        total_w = groups * group_w + (groups - 1) * group_gap
        x0 = max(0, center_x - total_w // 2)
        if x0 + total_w > self.width:
            x0 = max(0, self.width - total_w)

        second = max(0, min(59, int(second)))
        if str(cfg.get("display.seconds_bar_style", "deplete")) == "fill":
            lit = second  # grows from empty as the minute runs
        else:
            lit = 60 - second  # 60 dots at :00, one left during the last second

        on_rgb = cfg.color("display.seconds_bar_color")
        off_rgb = cfg.color("display.seconds_bar_track_color")
        for i in range(groups * per_group):
            g, k = divmod(i, per_group)
            x = x0 + g * (group_w + group_gap) + k * (dot_w + dot_gap)
            self._fill(x, y, dot_w, height, on_rgb if i < lit else off_rgb)

    @staticmethod
    def _resolve_font(cfg: Config, key: str) -> str:
        raw = str(cfg.get(key, "") or "")
        if not raw:
            return ""
        path = Path(raw).expanduser() if raw.startswith(("/", "~")) else cfg.resolve(raw)
        if path.is_file():
            return str(path)
        log.warning("%s: %s not found; falling back", key, path)
        return ""

    def _clock_font(self, reference: str, max_w: int, max_h: int, compact: bool):
        """The outline font for the time, or None to use seven-segment."""
        path = self._compact_font_path if compact else self._clock_font_path
        if not path:
            return None
        from .textrender import fit

        tracking = self._compact_tracking if compact else 0
        return fit(path, reference, max_w, max_h, self._clock_antialias, tracking)

    def _pick_entry_font(self, rows: list[tuple[str, str]], avail_h: int):
        """Biggest font from the ladder that fits the rows both ways.

        Height alone is not enough: "Timer1" plus "00:03:00" in a 10x20 font
        needs 140px on a 128px panel, so the name would run straight into the
        countdown.  Each candidate has to clear the widest row too.
        """
        widest = 0
        for name, detail in rows:
            widest = max(widest, len(name), len(detail))

        for font in self.entry_fonts:
            row_h = self.backend.font_height(font) + 1
            if row_h * len(rows) > avail_h:
                continue
            too_wide = any(
                self.text_width(font, name) + self.text_width(font, detail) + 4 > self.width
                for name, detail in rows
            )
            if not too_wide:
                return font, row_h
        if self.entry_fonts:
            font = self.entry_fonts[-1]
            return font, self.backend.font_height(font) + 1
        return None, 0

    def _draw_entries(self, entries: list[Entry], now: datetime, top: int, avail_h: int) -> None:
        cfg = self.cfg
        hour_format = int(cfg.get("clock.hour_format", 12))
        timer_format = cfg.get("display.timer_format", "hms")
        flash_hz = float(cfg.get("display.ring_flash_hz", 2.0))
        flash_on = (time.monotonic() * flash_hz) % 1.0 < 0.5

        alarm_rgb = cfg.color("display.alarm_color")
        timer_rgb = cfg.color("display.timer_color")
        watch_rgb = cfg.color("display.stopwatch_color")
        ring_rgb = cfg.color("display.ringing_color")
        exp_rgb = cfg.color("display.expired_color")
        by_kind = {"alarm": alarm_rgb, "stopwatch": watch_rgb}

        rows: list[tuple[str, str, RGB]] = []
        for entry in entries:
            if entry.state is EntryState.RINGING:
                rgb = ring_rgb if flash_on else (40, 0, 0)
            elif entry.state is EntryState.EXPIRED:
                rgb = exp_rgb
            else:
                rgb = by_kind.get(entry.kind, timer_rgb)
                if entry.state is EntryState.PAUSED:
                    rgb = tuple(v // 2 for v in rgb)
            detail = entry.detail(now, hour_format, timer_format)
            if entry.state is EntryState.PAUSED:
                detail = "|| " + detail
            rows.append((entry.name, detail, rgb))

        font, row_h = self._pick_entry_font([(n, d) for n, d, _ in rows], avail_h)
        if font is None or row_h <= 0:
            return

        max_rows = max(1, avail_h // row_h)
        shown = rows
        overflow = len(rows) > max_rows
        if overflow:
            # Keep the last row free for the "+n more" marker.
            shown = rows[: max_rows - 1] if max_rows > 1 else rows[:1]

        baseline_off = self.backend.font_baseline(font)
        for i, (name, detail, rgb) in enumerate(shown):
            baseline = top + i * row_h + baseline_off
            if baseline >= self.height:
                break
            self.draw_text(font, 1, baseline, rgb, name)
            dw = self.text_width(font, detail)
            self.draw_text(font, max(0, self.width - dw - 1), baseline, rgb, detail)

        if overflow and self.small_font:
            more = f"+{len(rows) - len(shown)} more"
            baseline = top + len(shown) * row_h + self.backend.font_baseline(self.small_font)
            if baseline < self.height:
                self.draw_text(self.small_font, 1, baseline, (110, 110, 110), more)

    def _draw_toast(self, text: str, rgb: RGB) -> None:
        if not self.small_font:
            return
        h = self.backend.font_height(self.small_font)
        self._fill(0, self.height - h - 1, self.width, h + 1, (0, 0, 0))
        self.draw_text(self.small_font, 1, self.height - 1, rgb, text[:40])

    def clear(self) -> None:
        self.backend.close()
