"""Alarms and timers: the data model plus the store that owns them.

Lifecycle of an entry::

    PENDING ──fires──> RINGING ──ring_seconds──> EXPIRED ──linger_seconds──> gone
       │                  │                         │
       └──── cancel ──────┴──── dismiss ────────────┘ (removed immediately)

Timers may additionally sit in PAUSED.  Everything is driven off wall-clock
timestamps rather than tick counters so that a restart (or an NTP step) does
not drift the countdown.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path


class EntryState(str, Enum):
    PENDING = "pending"
    PAUSED = "paused"
    RINGING = "ringing"
    EXPIRED = "expired"


@dataclass
class Entry:
    """Common behaviour for alarms and timers."""

    index: int
    created_at: datetime
    state: EntryState = EntryState.PENDING
    fired_at: datetime | None = None
    kind: str = "entry"

    @property
    def name(self) -> str:
        return f"{self.kind.capitalize()}{self.index}"

    @property
    def key(self) -> str:
        """Normalised lookup key, e.g. ``timer1``."""
        return f"{self.kind}{self.index}"

    def remaining(self, now: datetime) -> timedelta:
        raise NotImplementedError

    def detail(self, now: datetime, hour_format: int, timer_format: str) -> str:
        raise NotImplementedError

    def is_ringing(self) -> bool:
        return self.state is EntryState.RINGING

    def tick(self, now: datetime, ring_seconds: float) -> bool:
        """Advance the state machine.  Returns True if the entry just fired."""
        if self.state in (EntryState.PAUSED, EntryState.EXPIRED):
            return False
        if self.state is EntryState.PENDING:
            if self.remaining(now).total_seconds() <= 0:
                self.state = EntryState.RINGING
                self.fired_at = now
                return True
            return False
        if self.state is EntryState.RINGING and self.fired_at is not None:
            if (now - self.fired_at).total_seconds() >= ring_seconds:
                self.state = EntryState.EXPIRED
        return False

    def should_remove(self, now: datetime, linger_seconds: float) -> bool:
        if self.fired_at is None:
            return False
        return (now - self.fired_at).total_seconds() >= linger_seconds

    def dismiss(self) -> None:
        """Silence a ringing entry but leave it on screen to linger."""
        if self.state is EntryState.RINGING:
            self.state = EntryState.EXPIRED

    def add(self, delta: timedelta, now: datetime | None = None) -> None:
        """Extend (or shorten) the entry, reviving it if that puts it back in
        the future."""
        raise NotImplementedError

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "index": self.index,
            "created_at": self.created_at.isoformat(),
            "state": self.state.value,
            "fired_at": self.fired_at.isoformat() if self.fired_at else None,
        }


@dataclass
class Alarm(Entry):
    """Fires at an absolute wall-clock time."""

    target: datetime = field(default_factory=datetime.now)
    kind: str = "alarm"

    def remaining(self, now: datetime) -> timedelta:
        return self.target - now

    def detail(self, now: datetime, hour_format: int, timer_format: str) -> str:
        return format_clock_time(self.target, hour_format)

    def add(self, delta: timedelta, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self.target += delta
        if self.state in (EntryState.RINGING, EntryState.EXPIRED) and self.target > now:
            self.state = EntryState.PENDING
            self.fired_at = None

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["target"] = self.target.isoformat()
        return d


@dataclass
class Timer(Entry):
    """Counts down a duration from the moment it was started."""

    duration: timedelta = field(default_factory=timedelta)
    started_at: datetime = field(default_factory=datetime.now)
    # Time already burned before the most recent pause.
    elapsed_before_pause: timedelta = field(default_factory=timedelta)
    kind: str = "timer"

    def elapsed(self, now: datetime) -> timedelta:
        if self.state is EntryState.PAUSED:
            return self.elapsed_before_pause
        return self.elapsed_before_pause + (now - self.started_at)

    def remaining(self, now: datetime) -> timedelta:
        return self.duration - self.elapsed(now)

    def detail(self, now: datetime, hour_format: int, timer_format: str) -> str:
        if self.state in (EntryState.RINGING, EntryState.EXPIRED):
            secs = 0.0
        else:
            secs = max(0.0, self.remaining(now).total_seconds())
        return format_duration(secs, timer_format)

    def add(self, delta: timedelta, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self.duration += delta
        if self.state in (EntryState.RINGING, EntryState.EXPIRED) and self.duration > self.elapsed(now):
            self.state = EntryState.PENDING
            self.fired_at = None

    def pause(self, now: datetime) -> None:
        if self.state is EntryState.PENDING:
            self.elapsed_before_pause = self.elapsed(now)
            self.state = EntryState.PAUSED

    def resume(self, now: datetime) -> None:
        if self.state is EntryState.PAUSED:
            self.started_at = now
            self.state = EntryState.PENDING

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            duration=self.duration.total_seconds(),
            started_at=self.started_at.isoformat(),
            elapsed_before_pause=self.elapsed_before_pause.total_seconds(),
        )
        return d


def format_clock_time(when: datetime, hour_format: int) -> str:
    """``4:00pm`` in 12-hour mode, ``16:00`` in 24-hour mode."""
    if hour_format == 24:
        return when.strftime("%H:%M")
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d}{'am' if when.hour < 12 else 'pm'}"


def format_duration(seconds: float, style: str = "hms") -> str:
    """``hms`` always renders HH:MM:SS; ``auto`` drops an all-zero hour field."""
    total = int(max(0.0, seconds) + 0.5)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if style == "auto" and hours == 0:
        return f"{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class EntryStore:
    """Thread-safe collection of alarms and timers.

    The voice thread and the button thread both mutate this while the render
    loop reads it, so every public method takes the lock.
    """

    def __init__(self, linger_seconds: float = 300.0, ring_seconds: float = 30.0):
        self.linger_seconds = linger_seconds
        self.ring_seconds = ring_seconds
        self._entries: list[Entry] = []
        self._lock = threading.RLock()

    # ---------------- creation ----------------

    def _next_index(self, kind: str) -> int:
        used = {e.index for e in self._entries if e.kind == kind}
        i = 1
        while i in used:
            i += 1
        return i

    def add_alarm(self, target: datetime) -> Alarm:
        with self._lock:
            alarm = Alarm(
                index=self._next_index("alarm"),
                created_at=datetime.now(),
                target=target,
            )
            self._entries.append(alarm)
            return alarm

    def add_timer(self, duration: timedelta) -> Timer:
        with self._lock:
            now = datetime.now()
            timer = Timer(
                index=self._next_index("timer"),
                created_at=now,
                duration=duration,
                started_at=now,
            )
            self._entries.append(timer)
            return timer

    # ---------------- lookup ----------------

    def find(self, key: str | None) -> Entry | None:
        """Resolve ``timer1`` / ``alarm2``, or bare ``timer`` = most recent."""
        if not key:
            return None
        key = key.replace(" ", "").replace("#", "").lower()
        with self._lock:
            for entry in self._entries:
                if entry.key == key:
                    return entry
            # Bare kind: newest of that kind still on screen.
            if key in ("timer", "alarm"):
                matches = [e for e in self._entries if e.kind == key]
                if matches:
                    return max(matches, key=lambda e: e.created_at)
            return None

    def all(self) -> list[Entry]:
        """Snapshot sorted for display: ringing first, then soonest to fire."""
        with self._lock:
            def sort_key(e: Entry):
                return (
                    0 if e.state is EntryState.RINGING else 1,
                    2 if e.state is EntryState.EXPIRED else 0,
                    e.remaining(datetime.now()).total_seconds(),
                    e.kind,
                    e.index,
                )

            return sorted(self._entries, key=sort_key)

    def ringing(self) -> list[Entry]:
        with self._lock:
            return [e for e in self._entries if e.state is EntryState.RINGING]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ---------------- mutation ----------------

    def remove(self, entry: Entry) -> bool:
        with self._lock:
            if entry in self._entries:
                self._entries.remove(entry)
                return True
            return False

    def clear(self, kind: str | None = None) -> int:
        """Remove every entry, or every entry of one kind."""
        with self._lock:
            keep = [] if kind is None else [e for e in self._entries if e.kind != kind]
            removed = len(self._entries) - len(keep)
            self._entries = keep
            return removed

    def dismiss_all(self) -> int:
        with self._lock:
            count = 0
            for entry in self._entries:
                if entry.state is EntryState.RINGING:
                    entry.dismiss()
                    count += 1
            return count

    def tick(self, now: datetime | None = None) -> list[Entry]:
        """Advance every entry.  Returns those that fired on this tick."""
        now = now or datetime.now()
        fired: list[Entry] = []
        with self._lock:
            for entry in list(self._entries):
                if entry.tick(now, self.ring_seconds):
                    fired.append(entry)
            self._entries = [
                e for e in self._entries if not e.should_remove(now, self.linger_seconds)
            ]
        return fired

    # ---------------- persistence ----------------

    def save(self, path: Path) -> None:
        with self._lock:
            payload = {"entries": [e.to_dict() for e in self._entries]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(path)
        except OSError:
            pass

    def load(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        now = datetime.now()
        restored: list[Entry] = []
        for raw in payload.get("entries", []):
            try:
                entry = _entry_from_dict(raw)
            except (KeyError, ValueError, TypeError):
                continue
            # Drop anything that would already have lingered away while we
            # were down, so a restart after a long outage comes up clean.
            if entry.should_remove(now, self.linger_seconds):
                continue
            restored.append(entry)
        with self._lock:
            self._entries = restored


def _entry_from_dict(raw: dict) -> Entry:
    kind = raw["kind"]
    common = dict(
        index=int(raw["index"]),
        created_at=datetime.fromisoformat(raw["created_at"]),
        state=EntryState(raw["state"]),
        fired_at=datetime.fromisoformat(raw["fired_at"]) if raw.get("fired_at") else None,
    )
    if kind == "alarm":
        return Alarm(target=datetime.fromisoformat(raw["target"]), **common)
    if kind == "timer":
        return Timer(
            duration=timedelta(seconds=float(raw["duration"])),
            started_at=datetime.fromisoformat(raw["started_at"]),
            elapsed_before_pause=timedelta(seconds=float(raw.get("elapsed_before_pause", 0))),
            **common,
        )
    raise ValueError(f"unknown entry kind {kind!r}")
