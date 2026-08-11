"""A minimal BDF bitmap-font reader.

Only used by the preview backend.  On the real panel the C++ library parses
these same files itself; this exists so the layout can be rendered to a PNG on
any machine, with pixel-identical metrics, for checking the design without the
panel in front of you.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Glyph:
    width: int          # DWIDTH: how far the cursor advances
    bbx: tuple[int, int, int, int]  # w, h, x-offset, y-offset
    rows: list[int]     # one int per bitmap row, MSB = leftmost pixel


class BDFFont:
    """Exposes the same surface the rgbmatrix ``graphics.Font`` does."""

    def __init__(self, path: str | Path):
        self.glyphs: dict[int, Glyph] = {}
        self.ascent = 0
        self.descent = 0
        self._default = 32
        self._parse(Path(path))
        self.height = self.ascent + self.descent
        self.baseline = self.ascent

    def _parse(self, path: Path) -> None:
        bbox_h = bbox_y = 0
        code = None
        dwidth = 0
        bbx = (0, 0, 0, 0)
        rows: list[int] = []
        in_bitmap = False

        with path.open("r", encoding="latin-1") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                if in_bitmap:
                    if line == "ENDCHAR":
                        if code is not None:
                            self.glyphs[code] = Glyph(dwidth, bbx, rows)
                        in_bitmap = False
                        rows = []
                        code = None
                    else:
                        try:
                            rows.append(int(line, 16))
                        except ValueError:
                            pass
                    continue

                parts = line.split()
                key = parts[0]
                if key == "FONTBOUNDINGBOX" and len(parts) >= 5:
                    bbox_h, bbox_y = int(parts[2]), int(parts[4])
                elif key == "FONT_ASCENT":
                    self.ascent = int(parts[1])
                elif key == "FONT_DESCENT":
                    self.descent = int(parts[1])
                elif key == "ENCODING":
                    code = int(parts[1])
                elif key == "DWIDTH" and len(parts) >= 2:
                    dwidth = int(parts[1])
                elif key == "BBX" and len(parts) >= 5:
                    bbx = (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
                elif key == "BITMAP":
                    in_bitmap = True
                    rows = []

        if not self.ascent and not self.descent:
            # Fall back to the bounding box when the properties are absent.
            self.descent = max(0, -bbox_y)
            self.ascent = max(0, bbox_h - self.descent)

    def CharacterWidth(self, code: int) -> int:  # noqa: N802 - mirrors the C++ API
        glyph = self.glyphs.get(code)
        return glyph.width if glyph else 0

    def glyph(self, code: int) -> Glyph | None:
        return self.glyphs.get(code) or self.glyphs.get(self._default)

    def pixels(self, code: int, x: int, baseline: int):
        """Yield absolute (px, py) for every lit pixel of one character."""
        glyph = self.glyph(code)
        if glyph is None:
            return
        bw, bh, bx, by = glyph.bbx
        # Each row is padded up to a whole number of bytes.
        pad = ((bw + 7) // 8) * 8
        top = baseline - by - bh
        for row_index, bits in enumerate(glyph.rows[:bh]):
            py = top + row_index
            for col in range(bw):
                if bits & (1 << (pad - 1 - col)):
                    yield x + bx + col, py
