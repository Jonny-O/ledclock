"""The clock application: owns the run loop and dispatches every input.

Voice, buttons and the tick loop all funnel into :class:`ClockApp`, which is
the only place that mutates display state.  The render loop is the main
thread; voice and buttons run on their own threads and hand work back through
thread-safe calls on :class:`~ledclock.entries.EntryStore`.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta

from .buttons import Buttons
from .buzzer import Buzzer
from .config import Config
from .control import DEFAULT_SOCKET, ControlServer
from .entries import (
    Alarm, EntryState, EntryStore, Timer, format_clock_time, format_duration,
)
from .intents import Intent
from .voice import VoiceListener

log = logging.getLogger(__name__)

SAVE_INTERVAL = 10.0


class ClockApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.running = True

        self.store = EntryStore(
            linger_seconds=float(cfg.get("entries.linger_seconds", 300)),
            ring_seconds=float(cfg.get("entries.ring_seconds", 30)),
        )
        self.state_path = cfg.expand("state.path")
        if cfg.get("state.restore_on_start", True):
            self.store.load(self.state_path)

        # Imported here so --check-intents works on a machine with no panel.
        from .backends import MatrixBackend
        from .display import Renderer

        self.renderer = Renderer(cfg, MatrixBackend(cfg))
        self.buzzer = Buzzer(cfg)
        self.buttons = Buttons(cfg, self._on_button)
        self.voice = VoiceListener(cfg, self._on_intent, self._on_voice_state)

        self.control: ControlServer | None = None
        if cfg.get("control.enabled", True):
            self.control = ControlServer(
                cfg.get("control.socket", DEFAULT_SOCKET),
                self._on_intent,
                group=cfg.get("control.group") or None,
            )

        self._toast: str | None = None
        self._toast_color = cfg.color("display.heard_color")
        self._toast_until = 0.0
        self._toast_seconds = float(cfg.get("display.toast_seconds", 3.0))
        self._listening = False
        self._lock = threading.Lock()

        levels = cfg.get("buttons.brightness_levels", [20, 40, 60, 85, 100])
        self._brightness_levels = [int(v) for v in levels] or [60]
        self._brightness_idx = self._nearest_brightness(int(cfg.get("matrix.brightness", 60)))
        self._last_save = 0.0

    # ---------------- feedback ----------------

    def toast(self, text: str, color: tuple[int, int, int] | None = None) -> None:
        with self._lock:
            self._toast = text
            self._toast_color = color or self.cfg.color("display.heard_color")
            self._toast_until = time.monotonic() + self._toast_seconds

    def _current_toast(self) -> str | None:
        with self._lock:
            if self._toast and time.monotonic() < self._toast_until:
                return self._toast
            self._toast = None
            return None

    def _on_voice_state(self, awake: bool, heard: str) -> None:
        self._listening = awake
        if awake:
            self.buzzer.chirp()
        elif heard:
            self.toast(f'"{heard}"')

    # ---------------- intent dispatch ----------------

    def _on_intent(self, intent: Intent) -> None:
        handler = getattr(self, f"_do_{intent.action}", None)
        if handler is None:
            self.toast(f"? {intent.text}"[:40], (200, 80, 80))
            return
        handler(intent)

    def _do_unknown(self, intent: Intent) -> None:
        reason = intent.extras.get("reason")
        self.toast(f"? {reason or intent.text}"[:40], (200, 80, 80))

    def _do_set_alarm(self, intent: Intent) -> None:
        if not intent.when:
            return self._do_unknown(intent)
        alarm = self.store.add_alarm(intent.when)
        self.toast(
            f"+ {alarm.name} {format_clock_time(alarm.target, int(self.cfg.get('clock.hour_format', 12)))}",
            self.cfg.color("display.alarm_color"),
        )
        self.buzzer.chirp()

    def _do_set_timer(self, intent: Intent) -> None:
        if not intent.duration:
            return self._do_unknown(intent)
        timer = self.store.add_timer(intent.duration)
        self.toast(
            f"+ {timer.name} {format_duration(intent.duration.total_seconds(), self.cfg.get('display.timer_format', 'hms'))}",
            self.cfg.color("display.timer_color"),
        )
        self.buzzer.chirp()

    def _do_cancel(self, intent: Intent) -> None:
        entry = self.store.find(intent.target)
        if entry is None:
            self.toast(f"? no {intent.target or 'match'}", (200, 80, 80))
            return
        self.store.remove(entry)
        self._sync_buzzer()
        self.toast(f"- {entry.name}", (200, 140, 60))

    def _do_clear(self, intent: Intent) -> None:
        removed = self.store.clear(intent.kind)
        self._sync_buzzer()
        label = (intent.kind or "entry") + "s"
        self.toast(f"- {removed} {label}", (200, 140, 60))

    def _do_add_time(self, intent: Intent) -> None:
        entry = self.store.find(intent.target)
        if entry is None or intent.duration is None:
            self.toast(f"? no {intent.target or 'match'}", (200, 80, 80))
            return
        entry.add(intent.duration)
        self._sync_buzzer()
        sign = "+" if intent.duration.total_seconds() >= 0 else "-"
        mins = abs(intent.duration.total_seconds()) / 60
        self.toast(f"{entry.name} {sign}{mins:g}m")

    def _do_pause(self, intent: Intent) -> None:
        entry = self.store.find(intent.target)
        if isinstance(entry, Timer):
            entry.pause(datetime.now())
            self.toast(f"|| {entry.name}")
        else:
            self.toast("? no timer", (200, 80, 80))

    def _do_resume(self, intent: Intent) -> None:
        entry = self.store.find(intent.target)
        if isinstance(entry, Timer):
            entry.resume(datetime.now())
            self.toast(f"> {entry.name}")
        else:
            self.toast("? no timer", (200, 80, 80))

    def _do_dismiss(self, intent: Intent) -> None:
        entry = self.store.find(intent.target) if intent.target else None
        if entry is not None:
            entry.dismiss()
        else:
            self.store.dismiss_all()
        self._sync_buzzer()
        self.toast("dismissed")

    def _do_snooze(self, intent: Intent) -> None:
        minutes = int(self.cfg.get("buttons.snooze_minutes", 9))
        ringing = self.store.ringing()
        target = self.store.find(intent.target) if intent.target else None
        entries = [target] if target else ringing
        if not entries or entries == [None]:
            self.toast("? nothing ringing", (200, 80, 80))
            return
        for entry in entries:
            entry.add(timedelta(minutes=minutes))
        self._sync_buzzer()
        self.toast(f"snooze {minutes}m")

    def _do_status(self, intent: Intent) -> None:
        entries = self.store.all()
        alarms = sum(1 for e in entries if e.kind == "alarm")
        timers = len(entries) - alarms
        self.toast(f"{alarms} alarm {timers} timer")

    # ---------------- buttons ----------------

    def _on_button(self, action: str, held: bool) -> None:
        log.info("button: %s%s", action, " (held)" if held else "")
        if action == "dismiss":
            if held:
                self.store.dismiss_all()
                self.toast("dismissed all")
            else:
                ringing = self.store.ringing()
                if ringing:
                    ringing[0].dismiss()
                    self.toast(f"dismissed {ringing[0].name}")
            self._sync_buzzer()
        elif action == "dismiss_all":
            self.store.dismiss_all()
            self._sync_buzzer()
            self.toast("dismissed all")
        elif action == "snooze":
            self._do_snooze(Intent("snooze"))
        elif action == "cycle_brightness":
            self._brightness_idx = (self._brightness_idx + 1) % len(self._brightness_levels)
            value = self._brightness_levels[self._brightness_idx]
            self.renderer.set_brightness(value)
            self.toast(f"brightness {value}%")
        elif action == "toggle_hour_format":
            current = int(self.cfg.get("clock.hour_format", 12))
            self.cfg.data["clock"]["hour_format"] = 24 if current == 12 else 12
            self.toast(f"{self.cfg.get('clock.hour_format')}h")
        elif action == "add_minute":
            entries = self.store.all()
            if entries:
                entries[0].add(timedelta(minutes=1))
                self._sync_buzzer()
                self.toast(f"{entries[0].name} +1m")
        elif action == "pause_resume":
            for entry in self.store.all():
                if isinstance(entry, Timer):
                    if entry.state is EntryState.PAUSED:
                        entry.resume(datetime.now())
                        self.toast(f"> {entry.name}")
                    else:
                        entry.pause(datetime.now())
                        self.toast(f"|| {entry.name}")
                    break
        elif action == "cancel_last":
            entries = self.store.all()
            if entries:
                newest = max(entries, key=lambda e: e.created_at)
                self.store.remove(newest)
                self._sync_buzzer()
                self.toast(f"- {newest.name}", (200, 140, 60))

    def _nearest_brightness(self, value: int) -> int:
        return min(
            range(len(self._brightness_levels)),
            key=lambda i: abs(self._brightness_levels[i] - value),
        )

    # ---------------- run loop ----------------

    def _sync_buzzer(self) -> None:
        if self.store.ringing():
            self.buzzer.start()
        else:
            self.buzzer.stop()

    def run(self) -> None:
        fps = max(1, int(self.cfg.get("display.fps", 20)))
        frame = 1.0 / fps

        self.buttons.start()
        self.voice.start()
        if self.control:
            self.control.start()
        log.info("clock running at %d fps on %dx%d",
                 fps, self.renderer.width, self.renderer.height)

        try:
            while self.running:
                started = time.monotonic()

                fired = self.store.tick()
                for entry in fired:
                    log.info("%s fired", entry.name)
                self._sync_buzzer()

                self.renderer.render(
                    self.store.all(),
                    {
                        "toast": self._current_toast(),
                        "toast_color": self._toast_color,
                        "listening": self._listening or self.voice.awake,
                    },
                )

                if started - self._last_save > SAVE_INTERVAL:
                    self.store.save(self.state_path)
                    self._last_save = started

                elapsed = time.monotonic() - started
                if elapsed < frame:
                    time.sleep(frame - elapsed)
        finally:
            self.shutdown()

    def stop(self, *_args) -> None:
        self.running = False

    def shutdown(self) -> None:
        log.info("shutting down")
        self.store.save(self.state_path)
        if self.control:
            self.control.stop()
        self.voice.stop()
        self.buttons.close()
        self.buzzer.close()
        self.renderer.clear()


def install_signal_handlers(app: ClockApp) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, app.stop)
