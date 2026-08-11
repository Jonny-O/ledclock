"""Physical buttons on GPIO.

Pins and their actions come entirely from the preferences file, so wiring can
change without touching code::

    [buttons.pins]
    5  = "dismiss"
    6  = "snooze"

Pins are polled at 50 Hz with software debounce rather than using edge
callbacks — it costs almost nothing, gives us press-and-hold for free, and
avoids callback lifetime problems when the process is shutting down.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)

try:
    import lgpio
except ImportError:  # pragma: no cover - depends on host
    lgpio = None

POLL_INTERVAL = 0.02

ACTIONS = {
    "dismiss", "dismiss_all", "snooze", "cycle_brightness",
    "toggle_hour_format", "add_minute", "pause_resume", "cancel_last",
}


class Buttons:
    """Polls configured pins and reports press / hold events."""

    def __init__(self, cfg: Config, on_action: Callable[[str, bool], None]):
        c = cfg.section("buttons")
        self.on_action = on_action
        self.enabled = bool(c.get("enabled", True))
        self.pull_up = bool(c.get("pull_up", True))
        self.bounce = float(c.get("bounce_seconds", 0.05))
        self.hold_seconds = float(c.get("hold_seconds", 1.0))

        self.pins: dict[int, str] = {}
        for raw_pin, action in (c.get("pins") or {}).items():
            try:
                pin = int(raw_pin)
            except (TypeError, ValueError):
                log.warning("ignoring non-numeric button pin %r", raw_pin)
                continue
            if action not in ACTIONS:
                log.warning("ignoring unknown button action %r on pin %s", action, pin)
                continue
            self.pins[pin] = action

        self._chip = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # pin -> [pressed, last_change, hold_fired]
        self._state: dict[int, list] = {}

        if not self.enabled or not self.pins:
            self.enabled = False
            return
        if lgpio is None:
            log.warning("buttons enabled but python3-lgpio is not installed; disabling")
            self.enabled = False
            return
        self._claim()

    def _claim(self) -> None:
        try:
            self._chip = lgpio.gpiochip_open(0)
        except Exception as exc:
            log.warning("could not open gpiochip0: %s; buttons disabled", exc)
            self.enabled = False
            return
        flags = 0
        if self.pull_up:
            flags = getattr(lgpio, "SET_PULL_UP", 0)
        else:
            flags = getattr(lgpio, "SET_PULL_DOWN", 0)
        claimed = {}
        for pin, action in self.pins.items():
            try:
                lgpio.gpio_claim_input(self._chip, pin, flags)
                claimed[pin] = action
                self._state[pin] = [False, 0.0, False]
            except Exception as exc:
                log.warning("could not claim button pin %s (%s): %s", pin, action, exc)
        self.pins = claimed
        if not self.pins:
            self.enabled = False

    def _is_pressed(self, pin: int) -> bool:
        level = lgpio.gpio_read(self._chip, pin)
        return level == 0 if self.pull_up else level == 1

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="buttons", daemon=True)
        self._thread.start()
        log.info("buttons active: %s", ", ".join(f"{p}={a}" for p, a in sorted(self.pins.items())))

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            for pin, action in self.pins.items():
                try:
                    pressed = self._is_pressed(pin)
                except Exception:
                    continue
                state = self._state[pin]
                if pressed != state[0]:
                    if now - state[1] < self.bounce:
                        continue
                    state[0] = pressed
                    state[1] = now
                    if pressed:
                        state[2] = False
                    elif not state[2]:
                        # Fire on release, so a hold doesn't also send a press.
                        self._fire(action, held=False)
                elif pressed and not state[2] and (now - state[1]) >= self.hold_seconds:
                    state[2] = True
                    self._fire(action, held=True)
            self._stop.wait(POLL_INTERVAL)

    def _fire(self, action: str, held: bool) -> None:
        try:
            self.on_action(action, held)
        except Exception:
            log.exception("button action %r failed", action)

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._chip is not None:
            for pin in self.pins:
                try:
                    lgpio.gpio_free(self._chip, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(self._chip)
            except Exception:
                pass
            self._chip = None
