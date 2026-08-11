"""Preferences loading.

All tunables live in a TOML file (default ``~/ledclock/config.toml``) so the
panel wiring, GPIO pins and voice settings can be changed without touching
code.  Missing keys fall back to :data:`DEFAULTS`, so a partial config file is
always valid.
"""

from __future__ import annotations

import copy
import os
import pwd
import tomllib
from pathlib import Path
from typing import Any


def user_home() -> Path:
    """Home of the invoking user.

    The matrix needs root, so the clock normally runs under sudo — where a
    bare ``Path.home()`` would resolve ``~`` to ``/root`` and lose the fonts,
    the model and the state file.  Honour SUDO_USER instead.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()

DEFAULT_PATH = Path(os.environ.get("LEDCLOCK_CONFIG", user_home() / "ledclock" / "config.toml"))

DEFAULTS: dict[str, Any] = {
    "matrix": {
        # Panel geometry.  A single P2 128x64 panel is one 64-row chain.
        "rows": 64,
        "cols": 128,
        "chain_length": 1,
        "parallel": 1,
        # 'regular' = HUB75 ribbon jumpered straight onto the Pi header.
        # Other values: 'adafruit-hat', 'adafruit-hat-pwm', 'regular-pi1'.
        "hardware_mapping": "regular",
        "brightness": 60,
        "pwm_bits": 11,
        "pwm_lsb_nanoseconds": 130,
        "gpio_slowdown": 4,  # Pi 4 needs 3-5; raise if the image glitches.
        "scan_mode": 0,
        "row_address_type": 0,
        "multiplexing": 0,
        "pixel_mapper": "",
        "disable_hardware_pulsing": False,
        "limit_refresh_rate_hz": 0,
        "show_refresh_rate": False,
        "pwm_dither_bits": 0,
        "inverse_colors": False,
        "led_rgb_sequence": "RGB",
        # Some 128x64 P2 panels need an init sequence, e.g. "FM6126A".
        "panel_type": "",
        # Stay root so buttons, buzzer and the state file still work after
        # the matrix initialises.
        "drop_privileges": False,
    },
    "clock": {
        "hour_format": 12,  # 12 or 24
        "show_seconds": False,
        "show_date": True,
        # Superseded by the seconds bar; the colon now stays lit.
        "blink_colon": False,
        # Bar under the time showing how much of the minute is left.
        "seconds_bar": True,
        "leading_zero": False,  # 24h mode always pads.
    },
    "display": {
        # Fraction of screen height the clock keeps once entries are listed.
        "compact_clock_fraction": 0.25,
        "font_dir": "rpi-rgb-led-matrix/fonts",
        # Outline font for the time itself. "" = seven-segment digits.
        "clock_font": "",
        # Separate face for the quarter-height clock; "" reuses clock_font.
        "compact_clock_font": "",
        # Extra pixels between glyphs in the compact clock, to spread it out.
        "compact_clock_tracking": 0,
        "clock_antialias": True,
        "seconds_bar_height": 2,
        "seconds_bar_gap": 2,
        "seconds_bar_color": [255, 176, 0],
        "seconds_bar_track_color": [46, 32, 0],
        # 'deplete' empties as the minute runs out; 'fill' grows instead.
        "seconds_bar_style": "deplete",
        # 60 dots, read as six groups of ten.
        "seconds_bar_group_size": 10,
        "seconds_bar_dot_width": 1,
        "seconds_bar_dot_gap": 0,
        "seconds_bar_group_gap": 2,
        # Largest-first ladder; the biggest font that fits the rows is used.
        "entry_fonts": ["10x20.bdf", "9x18B.bdf", "7x13B.bdf", "6x10.bdf", "5x7.bdf", "4x6.bdf"],
        "small_font": "4x6.bdf",
        # 'hms' always shows HH:MM:SS; 'auto' hides a zero hours field.
        "timer_format": "hms",
        # Seconds a spoken-command confirmation stays on screen.
        "toast_seconds": 3.0,
        "clock_color": [255, 176, 0],
        "date_color": [90, 90, 90],
        "alarm_color": [0, 190, 255],
        "timer_color": [0, 235, 120],
        "stopwatch_color": [170, 110, 255],
        "ringing_color": [255, 40, 40],
        "expired_color": [110, 110, 110],
        "heard_color": [200, 120, 255],
        "fps": 20,
        # Entries flash while ringing.
        "ring_flash_hz": 2.0,
    },
    "entries": {
        # How long an entry stays on screen after it fires, in seconds.
        "linger_seconds": 300,
        # How long the buzzer/flash stays active after firing.
        "ring_seconds": 30,
        "max_visible": 8,
    },
    "buzzer": {
        "enabled": False,
        "pin": 13,
        # 'passive' drives a square wave via PWM; 'active' just switches on.
        "type": "passive",
        "frequency_hz": 2730,
        "duty_cycle": 50,
        # (on_seconds, off_seconds) repeated for the duration of the ring.
        "beat_on": 0.15,
        "beat_off": 0.35,
        "active_high": True,
    },
    "buttons": {
        "enabled": True,
        "pull_up": True,
        "bounce_seconds": 0.05,
        "hold_seconds": 1.0,
        # GPIO (BCM) number -> action name.  Set to {} to disable all.
        # Actions: dismiss, dismiss_all, snooze, cycle_brightness,
        #          toggle_hour_format, add_minute, pause_resume, cancel_last
        "pins": {
            "5": "dismiss",
            "6": "snooze",
            "16": "cycle_brightness",
            "26": "cancel_last",
        },
        "brightness_levels": [20, 40, 60, 85, 100],
        "snooze_minutes": 9,
    },
    "voice": {
        "enabled": True,
        # One word or several.  Check any new phrase against the speech
        # model first: python -m ledclock --check-wake "<phrase>"
        "wake_phrase": "timekeeper",
        # Only meaningful when wake_phrase is one word repeated ("clock clock
        # clock"): how many must land in a row.  Distinct phrases are always
        # matched in full.
        "wake_min_repeats": 2,
        # Seconds to keep listening for a command after waking.
        "command_timeout": 8.0,
        "model_path": "models/vosk-model-small-en-us-0.15",
        # ALSA capture device.  'plughw:' lets ALSA resample 48k -> 16k.
        "device": "plughw:1,0",
        "sample_rate": 16000,
        # Restrict the recognizer to our command vocabulary.  Hugely improves
        # accuracy on a small model; set false to allow free-form speech.
        "use_grammar": True,
        "arecord": "arecord",
        # Software boost applied to the captured samples before recognition,
        # for when the ALSA capture control is already at its ceiling and the
        # speaker is still across the room.  1.0 = untouched.  Check the
        # headroom first with `--mic-level`: this clips, it cannot invent it.
        "gain": 1.0,
    },
    "power": {
        # Voice-controlled shutdown and reboot.
        "enabled": True,
        # Require a spoken "yes" before acting.  This is what makes the
        # feature safe to leave on: a misheard "shut down" only ever puts a
        # question on the panel.  Turning it off costs a walk to the plug the
        # first time the mic mishears something.
        "confirm": True,
        "confirm_seconds": 10.0,
        "shutdown_command": ["systemctl", "poweroff"],
        "reboot_command": ["systemctl", "reboot"],
    },
    "control": {
        # Local Unix socket for `--send`, so the clock can be driven without
        # speaking.  Local-only; there is no network listener.
        "enabled": True,
        "socket": "/run/ledclock.sock",
        # Group given access to the socket, so `--send` works without sudo.
        "group": "gpio",
    },
    "state": {
        "path": "state.json",
        "restore_on_start": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Dotted-path read-only view over the merged preferences."""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        p = Path(path).expanduser() if path else DEFAULT_PATH
        if p.is_file():
            with p.open("rb") as fh:
                user = tomllib.load(fh)
            return cls(_merge(DEFAULTS, user), p)
        return cls(copy.deepcopy(DEFAULTS), None)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return self.get(name, {}) or {}

    @property
    def base_dir(self) -> Path:
        """Directory relative paths are resolved against."""
        if self.path is not None:
            return self.path.parent.resolve()
        return Path(__file__).resolve().parent.parent

    def resolve(self, raw: str | os.PathLike) -> Path:
        """Expand ``~`` for the real user and anchor relative paths.

        Relative paths hang off the config file's directory, which keeps the
        whole project relocatable — move the folder and nothing breaks.
        """
        text = str(raw)
        if text == "~":
            return user_home()
        if text.startswith("~/"):
            return user_home() / text[2:]
        path = Path(text)
        return path if path.is_absolute() else self.base_dir / path

    def expand(self, dotted: str) -> Path:
        return self.resolve(str(self.get(dotted)))

    def color(self, dotted: str) -> tuple[int, int, int]:
        raw = self.get(dotted, [255, 255, 255])
        return (int(raw[0]), int(raw[1]), int(raw[2]))
