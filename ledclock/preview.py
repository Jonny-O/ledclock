"""Render sample frames to PNGs.

``python -m ledclock --preview out/`` writes one image per scenario so the
layout, colours and font ladder can be judged without the panel connected.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .backends import PreviewBackend
from .config import Config
from .display import Renderer
from .entries import Alarm, EntryState, Stopwatch, Timer

# (filename, description, builder) — builder returns (entries, status)
SCENES: list[tuple[str, str, object]] = []


def _scene(name: str, description: str):
    def wrap(fn):
        SCENES.append((name, description, fn))
        return fn

    return wrap


def _alarm(index: int, minutes_ahead: float, state=EntryState.PENDING) -> Alarm:
    now = datetime.now()
    return Alarm(
        index=index, created_at=now, state=state,
        target=now + timedelta(minutes=minutes_ahead),
        fired_at=now if state in (EntryState.RINGING, EntryState.EXPIRED) else None,
    )


def _timer(index: int, seconds_left: float, state=EntryState.PENDING) -> Timer:
    now = datetime.now()
    return Timer(
        index=index, created_at=now, state=state,
        duration=timedelta(seconds=seconds_left), started_at=now,
        fired_at=now if state in (EntryState.RINGING, EntryState.EXPIRED) else None,
    )


def _watch(index: int, seconds_elapsed: float, state=EntryState.PENDING) -> Stopwatch:
    now = datetime.now()
    return Stopwatch(
        index=index, created_at=now, state=state,
        started_at=now - timedelta(seconds=seconds_elapsed),
    )


@_scene("01-clock-only", "Just the time, full screen")
def _s1():
    return [], {}


@_scene("02-one-timer", "One running timer")
def _s2():
    return [_timer(1, 3 * 60)], {}


@_scene("03-alarm-and-timer", "An alarm and a timer")
def _s3():
    return [_alarm(1, 90), _timer(1, 3 * 60)], {}


@_scene("04-four-entries", "Four entries — font ladder steps down")
def _s4():
    return [
        _alarm(1, 90), _alarm(2, 400),
        _timer(1, 3 * 60), _timer(2, 45),
    ], {}


@_scene("05-ringing", "A timer ringing, mid-flash")
def _s5():
    return [_timer(1, 0, EntryState.RINGING), _alarm(1, 120)], {}


@_scene("06-mixed-states", "Ringing, paused and expired together")
def _s6():
    return [
        _timer(1, 0, EntryState.RINGING),
        _timer(2, 600, EntryState.PAUSED),
        _alarm(1, 0, EntryState.EXPIRED),
        _alarm(2, 300),
    ], {}


@_scene("07-toast", "Confirmation after a spoken command")
def _s7():
    return [_timer(1, 180)], {"toast": "+ Timer1 00:03:00", "toast_color": (0, 235, 120),
                              "listening": False}


@_scene("08-listening", "Woken by the wake phrase")
def _s8():
    return [], {"listening": True}


@_scene("09-overflow", "More entries than fit")
def _s9():
    return [_timer(i, i * 90) for i in range(1, 5)] + [_alarm(i, i * 200) for i in range(1, 5)], {}


@_scene("10-stopwatch", "A stopwatch counting up on its own")
def _s10():
    return [_watch(1, 95)], {}


@_scene("11-all-three", "Alarm, timer and stopwatch together")
def _s11():
    # The stopwatch has no deadline, so it must sort below the two that do.
    return [_alarm(1, 90), _timer(1, 180), _watch(1, 3 * 3600 + 125)], {}


#: Candidate clock faces, as (label, font path).  Each is rendered both
#: antialiased and hard-edged by ``--compare-fonts``.
CANDIDATES: list[tuple[str, str]] = [
    ("seven-segment", ""),
    ("Inter Black", "/usr/share/fonts/opentype/inter/InterDisplay-Black.otf"),
    ("Inter Bold", "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf"),
    ("Montserrat Bold", "/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf"),
    ("Lato Black", "/usr/share/fonts/truetype/lato/Lato-Black.ttf"),
    ("DejaVu Sans Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("Liberation Narrow Bold",
     "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf"),
    ("JetBrains Mono Bold",
     "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Bold.ttf"),
]


def compare_fonts(cfg: Config, out_dir: Path, scale: int = 5) -> list[Path]:
    """Render every candidate face, with and without antialiasing.

    Produces one contact sheet per mode showing the full-screen clock and the
    compact clock, since a face can look great large and mushy small.
    """
    from PIL import Image, ImageDraw

    out_dir.mkdir(parents=True, exist_ok=True)
    cfg.data.setdefault("clock", {})["blink_colon"] = False
    entries_for_compact = [_alarm(1, 90), _timer(1, 3 * 60)]
    written: list[Path] = []

    for antialias in (True, False):
        tiles: list[tuple[str, Image.Image, Image.Image]] = []
        for label, path in CANDIDATES:
            if path and not Path(path).is_file():
                print(f"  skipping {label}: {path} not installed")
                continue
            cfg.data["display"]["clock_font"] = path
            cfg.data["display"]["clock_antialias"] = antialias

            shots = []
            for entries in ([], entries_for_compact):
                backend = PreviewBackend(cfg, scale=1, grid=False)
                Renderer(cfg, backend).render(entries, {})
                shots.append(backend.image.copy())
            tiles.append((label, shots[0], shots[1]))

        if not tiles:
            continue

        pad, label_h = 6, 12
        panel_w, panel_h = tiles[0][1].width, tiles[0][1].height
        cell_w = panel_w * 2 + pad
        sheet = Image.new(
            "RGB",
            (cell_w * scale + pad * 2, (panel_h + label_h + pad) * len(tiles) * scale + pad),
            (18, 18, 20),
        )
        y = pad
        for label, full, compact in tiles:
            row = Image.new("RGB", (cell_w, panel_h), (0, 0, 0))
            row.paste(full, (0, 0))
            row.paste(compact, (panel_w + pad, 0))
            row = row.resize((row.width * scale, row.height * scale), Image.NEAREST)
            sheet.paste(row, (pad, y))
            draw = ImageDraw.Draw(sheet)
            draw.text((pad + 2, y + row.height + 2), label, fill=(210, 210, 215))
            y += row.height + (label_h + pad) * scale // scale + label_h

        mode = "antialiased" if antialias else "hard-edged"
        path = out_dir / f"fonts-{mode}.png"
        sheet.crop((0, 0, sheet.width, min(y + pad, sheet.height))).save(path)
        written.append(path)
        print(f"  {path.name}  ({len(tiles)} faces, {mode})")
    return written


def compare_compact(cfg: Config, out_dir: Path, scale: int = 7) -> list[Path]:
    """Render candidate faces for the quarter-height clock only.

    The compact clock is capped by height and has width to spare, so faces are
    shown both at their natural width and spread out with tracking.
    """
    from PIL import Image, ImageDraw

    out_dir.mkdir(parents=True, exist_ok=True)
    cfg.data.setdefault("clock", {})["blink_colon"] = False
    entries = [_alarm(1, 90), _timer(1, 3 * 60)]
    cfg.data["display"]["clock_antialias"] = True

    tiles = []
    for label, path in CANDIDATES:
        if path and not Path(path).is_file():
            continue
        for tracking in (0, 3):
            cfg.data["display"]["compact_clock_font"] = path
            cfg.data["display"]["compact_clock_tracking"] = tracking
            backend = PreviewBackend(cfg, scale=1, grid=False)
            Renderer(cfg, backend).render(entries, {})
            crop = backend.image.crop((0, 0, backend.width, 18))
            tiles.append((f"{label}  tracking={tracking}", crop))

    pad = 4
    tw, th = tiles[0][1].width, tiles[0][1].height
    sheet = Image.new("RGB", (tw * scale + pad * 2, (th * scale + 14) * len(tiles) + pad),
                      (18, 18, 20))
    y = pad
    for label, img in tiles:
        img = img.resize((tw * scale, th * scale), Image.NEAREST)
        sheet.paste(img, (pad, y))
        ImageDraw.Draw(sheet).text((pad + 2, y + img.height + 1), label, fill=(210, 210, 215))
        y += img.height + 14
    path = out_dir / "compact-clock.png"
    sheet.save(path)
    print(f"  {path.name}  ({len(tiles)} variants)")
    return [path]


def seconds_sweep(cfg: Config, out_dir: Path, scale: int = 5) -> list[Path]:
    """The clock at several points through a minute, to check the bar."""
    from unittest.mock import patch

    from PIL import Image, ImageDraw

    out_dir.mkdir(parents=True, exist_ok=True)
    base = datetime.now().replace(second=0, microsecond=0)
    marks = [0, 10, 25, 40, 55, 59]
    entries = [_alarm(1, 90), _timer(1, 3 * 60)]

    tiles = []
    for secs in marks:
        when = base.replace(second=secs)
        for label, ents in (("full", []), ("compact", entries)):
            backend = PreviewBackend(cfg, scale=1, grid=False)
            renderer = Renderer(cfg, backend)
            with patch("ledclock.display.datetime") as dt:
                dt.now.return_value = when
                renderer.render(ents, {})
            tiles.append((f"{secs:02d}s {label}", backend.image.copy()))

    pad = 4
    tw, th = tiles[0][1].width, tiles[0][1].height
    cols = 2
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB",
                      (cols * (tw * scale + pad) + pad, rows * (th * scale + 14) + pad),
                      (18, 18, 20))
    for i, (label, img) in enumerate(tiles):
        cx = pad + (i % cols) * (tw * scale + pad)
        cy = pad + (i // cols) * (th * scale + 14)
        sheet.paste(img.resize((tw * scale, th * scale), Image.NEAREST), (cx, cy))
        ImageDraw.Draw(sheet).text((cx + 2, cy + th * scale + 1), label, fill=(210, 210, 215))
    path = out_dir / "seconds-bar.png"
    sheet.save(path)
    print(f"  {path.name}  ({len(tiles)} frames)")
    return [path]


def render_previews(cfg: Config, out_dir: Path, scale: int = 6) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Freeze the blink so successive runs are comparable frame to frame.
    cfg.data.setdefault("clock", {})["blink_colon"] = False
    written: list[Path] = []
    for name, description, builder in SCENES:
        backend = PreviewBackend(cfg, scale=scale)
        renderer = Renderer(cfg, backend)
        entries, status = builder()
        renderer.render(entries, status)
        path = backend.save(out_dir / f"{name}.png")
        written.append(path)
        print(f"  {path.name:<24} {description}")
    return written
