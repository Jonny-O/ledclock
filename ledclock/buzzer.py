"""Piezo buzzer on a GPIO pin.

Supports both common parts:

* ``passive`` — a bare piezo element that needs an AC waveform.  Driven with
  lgpio's software PWM at ``frequency_hz``.
* ``active`` — a module with its own oscillator.  Just switched on and off.

The beat pattern runs on its own thread so the render loop never blocks.  If
lgpio is unavailable or the pin can't be claimed the buzzer degrades to a
no-op rather than taking the clock down with it.

A passive element is only loud near its mechanical resonance, and that peak is
sharp — tens of dB are lost a few hundred Hz off it.  ``python -m ledclock
--buzzer-sweep`` finds the peak for the part you actually fitted, using the
microphone as the instrument.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from .config import Config

log = logging.getLogger(__name__)

try:
    import lgpio
except ImportError:  # pragma: no cover - depends on host
    lgpio = None


def tone_list(raw, fallback: float = 4400.0) -> list[float]:
    """Normalise ``frequency_hz`` into a list of tones to alternate between.

    A bare number stays a single tone; a list warbles through them, one per
    beat.  Junk and non-positive entries are dropped rather than reaching
    lgpio, which would raise mid-ring on a background thread.
    """
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    tones: list[float] = []
    for value in values:
        try:
            freq = float(value)
        except (TypeError, ValueError):
            continue
        # lgpio's software PWM accepts 0.1-10000 Hz; anything else is a typo.
        if 0.1 <= freq <= 10000:
            tones.append(freq)
    if not tones:
        log.warning("no usable buzzer frequency in %r; falling back to %g Hz",
                    raw, fallback)
        return [fallback]
    return tones


class Buzzer:
    def __init__(self, cfg: Config):
        c = cfg.section("buzzer")
        self.enabled = bool(c.get("enabled", False))
        self.pin = int(c.get("pin", 13))
        self.kind = str(c.get("type", "passive"))
        # One number, or a list to alternate between: frequency_hz = [4400, 3300]
        self.frequencies = tone_list(c.get("frequency_hz", 4400))
        self.frequency = self.frequencies[0]
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

    def _tone_on(self, freq: float | None = None) -> None:
        if self._chip is None:
            return
        try:
            if self.kind == "passive":
                lgpio.tx_pwm(self._chip, self.pin,
                             self.frequency if freq is None else freq, self.duty)
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
            step = 0
            while not self._stop.is_set():
                self._tone_on(self.frequencies[step % len(self.frequencies)])
                step += 1
                if self._stop.wait(self.beat_on):
                    break
                # beat_off = 0 runs the tones back to back, which is what makes
                # a two-tone list a continuous warble rather than two beeps.
                # Silencing the pin between them would put a gap in it.
                if self.beat_off > 0:
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


# ---------------- tuning aid ----------------
#
# Everything below is for `--buzzer-sweep` and never runs on the clock.

_SWEEP_RATE = 48000  # well above any piezo resonance, so nothing aliases


def _record(device: str, seconds: float = 1.0):
    """One mono capture, as float64 samples."""
    import numpy as np

    raw = subprocess.run(
        ["arecord", "-D", device, "-f", "S16_LE", "-r", str(_SWEEP_RATE),
         "-c", "1", "-d", str(max(1, int(round(seconds)))), "-t", "raw", "-q"],
        capture_output=True, timeout=30,
    ).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float64)


def _band_power(samples, freq: float, halfwidth: float = 25.0) -> float:
    """Energy in a narrow band around ``freq``.

    Narrowband beats broadband RMS here: a quiet tone is buried in room noise
    across the whole spectrum but stands well clear of it in its own bin.
    """
    import numpy as np

    if len(samples) < 1024:
        return 0.0
    mag = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1 / _SWEEP_RATE)
    lo = np.searchsorted(freqs, freq - halfwidth)
    hi = np.searchsorted(freqs, freq + halfwidth)
    return float(np.sum(mag[lo:hi])) if hi > lo else 0.0


def sweep(cfg: Config, lo: float = 1000.0, hi: float = 6000.0,
          step: float = 100.0) -> int:
    """Drive the buzzer across a frequency range and report what the mic hears.

    Prints dB relative to the same band recorded in silence, which cancels the
    room out and leaves only the buzzer's contribution.
    """
    try:
        import numpy as np
    except ImportError:
        print("--buzzer-sweep needs numpy: sudo apt install python3-numpy")
        return 1
    if lgpio is None:
        print("--buzzer-sweep needs python3-lgpio")
        return 1

    c = cfg.section("buzzer")
    pin = int(c.get("pin", 13))
    duty = float(c.get("duty_cycle", 50))
    # A high-side PNP driver conducts on a LOW, so idling low would hold the
    # buzzer on between steps and colour every reading after the first.
    idle = 0 if bool(c.get("active_high", True)) else 1
    device = str(cfg.get("voice.device", "plughw:1,0"))
    if str(c.get("type", "passive")) != "passive":
        print("this only makes sense for a passive buzzer; an active one has a "
              "fixed tone of its own")
        return 1

    tones = [lo + step * i for i in range(int((hi - lo) / step) + 1)]
    print(f"sweeping GPIO{pin} from {lo:.0f} to {hi:.0f} Hz in {step:.0f} Hz steps, "
          f"listening on {device}")
    print(f"{len(tones)} steps, roughly {len(tones) * 1.4 / 60:.0f} min\n")

    chip = lgpio.gpiochip_open(0)
    results: list[tuple[float, float]] = []
    try:
        lgpio.gpio_claim_output(chip, pin, idle)
        # The first capture after opening the device carries a startup click
        # that inflates every band, so discard it before taking the reference.
        _record(device)
        quiet = _record(device)
        if len(quiet) < 1024:
            print(f"got no audio from {device} — check `--list-audio`, and stop "
                  f"the clock first if it is holding the mic")
            return 1

        print(f"{'freq':>7}  {'level':>8}")
        print("-" * 46)
        for freq in tones:
            lgpio.tx_pwm(chip, pin, freq, duty)
            time.sleep(0.2)
            heard = _record(device)
            lgpio.tx_pwm(chip, pin, 0, 0)
            lgpio.gpio_write(chip, pin, idle)

            on = _band_power(heard, freq)
            off = _band_power(quiet, freq)
            db = 10 * np.log10(on / off) if on > 0 and off > 0 else float("nan")
            results.append((freq, db))
            bar = "#" * max(0, int(db / 2)) if db == db else ""
            print(f"{freq:7.0f}  {db:+8.1f}  {bar}")
            time.sleep(0.1)
    finally:
        try:
            lgpio.tx_pwm(chip, pin, 0, 0)
            lgpio.gpio_write(chip, pin, idle)
        except Exception:
            pass
        lgpio.gpiochip_close(chip)

    usable = [(f, d) for f, d in results if d == d]
    if not usable:
        print("\nheard nothing at any frequency — check the wiring and that the "
              "mic can hear the buzzer")
        return 1

    best_f, best_db = max(usable, key=lambda r: r[1])
    # frequency_hz may be a list of alternating tones; compare against the first.
    now = tone_list(c.get("frequency_hz", 4400))[0]
    print(f"\nloudest at {best_f:.0f} Hz ({best_db:+.1f} dB)")
    near = [d for f, d in usable if abs(f - now) < step / 2]
    gain = best_db - near[0] if near else 0.0
    if near and gain < 1.0:
        # Run-to-run spread is a few tenths of a dB, so anything under 1 dB is
        # noise, not a finding.
        print(f"currently set to {now:.0f} Hz, which is already at the peak")
    elif near:
        print(f"currently set to {now:.0f} Hz ({near[0]:+.1f} dB) — {gain:.0f} dB "
              f"quieter, about {10 ** (gain / 20):.0f}x less sound pressure")
    if gain >= 1.0 or not near:
        print(f"\nput this in config.toml under [buzzer]:\n    frequency_hz = {best_f:.0f}")
    return 0
