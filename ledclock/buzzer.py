"""Piezo buzzer on a GPIO pin.

Supports both common parts:

* ``passive`` — a bare piezo element that needs an AC waveform.  Driven with
  lgpio's software PWM at ``frequency_hz``.
* ``active`` — a module with its own oscillator.  Just switched on and off.

The beat pattern runs on its own thread so the render loop never blocks.  If
lgpio is unavailable or the pin can't be claimed the buzzer degrades to a
no-op rather than taking the clock down with it.
"""

from __future__ import annotations

import logging
import threading
import time

from .config import Config

log = logging.getLogger(__name__)

try:
    import lgpio
except ImportError:  # pragma: no cover - depends on host
    lgpio = None


class Buzzer:
    def __init__(self, cfg: Config):
        c = cfg.section("buzzer")
        self.enabled = bool(c.get("enabled", False))
        self.pin = int(c.get("pin", 13))
        self.kind = str(c.get("type", "passive"))
        self.frequency = float(c.get("frequency_hz", 2730))
        self.duty = float(c.get("duty_cycle", 50))
        self.beat_on = float(c.get("beat_on", 0.15))
        self.beat_off = float(c.get("beat_off", 0.35))
        self.active_high = bool(c.get("active_high", True))

        self._chip = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        if not self.enabled:
            return
        if lgpio is None:
            log.warning("buzzer enabled but python3-lgpio is not installed; disabling")
            self.enabled = False
            return
        try:
            self._chip = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(self._chip, self.pin, 0 if self.active_high else 1)
        except Exception as exc:
            log.warning("could not claim buzzer pin %s: %s; disabling", self.pin, exc)
            self.enabled = False
            self._chip = None

    # ---------------- low level ----------------

    def _tone_on(self) -> None:
        if self._chip is None:
            return
        try:
            if self.kind == "passive":
                lgpio.tx_pwm(self._chip, self.pin, self.frequency, self.duty)
            else:
                lgpio.gpio_write(self._chip, self.pin, 1 if self.active_high else 0)
        except Exception as exc:
            log.debug("buzzer on failed: %s", exc)

    def _tone_off(self) -> None:
        if self._chip is None:
            return
        try:
            if self.kind == "passive":
                lgpio.tx_pwm(self._chip, self.pin, 0, 0)
            lgpio.gpio_write(self._chip, self.pin, 0 if self.active_high else 1)
        except Exception as exc:
            log.debug("buzzer off failed: %s", exc)

    # ---------------- public ----------------

    def start(self) -> None:
        """Begin the repeating beat.  Idempotent while already sounding."""
        if not self.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="buzzer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None and self._stop.is_set():
                # Already silent.  The run loop calls this every frame, so
                # bailing here keeps us from hammering lgpio 20 times a second.
                return
            self._stop.set()
            self._thread = None
        if thread:
            thread.join(timeout=1.0)
        self._tone_off()

    @property
    def sounding(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._tone_on()
                if self._stop.wait(self.beat_on):
                    break
                self._tone_off()
                if self._stop.wait(self.beat_off):
                    break
        finally:
            self._tone_off()

    def chirp(self, duration: float = 0.06) -> None:
        """One short blip — feedback for a button press or accepted command."""
        if not self.enabled or self.sounding:
            return

        def _blip() -> None:
            self._tone_on()
            time.sleep(duration)
            self._tone_off()

        threading.Thread(target=_blip, name="chirp", daemon=True).start()

    def close(self) -> None:
        self.stop()
        if self._chip is not None:
            try:
                lgpio.gpiochip_close(self._chip)
            except Exception:
                pass
            self._chip = None
