"""Offline checks for the intent parser.

Run with ``python -m ledclock --check-intents``.  No hardware is touched, so
this works over SSH with the panel unplugged.  The phrasings below are written
the way Vosk actually emits them: lowercase, no punctuation, numbers spelled
out, "4pm" arriving as "four p m".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .intents import parse

NOW = datetime(2026, 8, 10, 14, 30, 0)  # a Monday afternoon


def _hhmm(intent) -> str | None:
    return intent.when.strftime("%Y-%m-%d %H:%M") if intent.when else None


def _mins(intent) -> float | None:
    return intent.duration.total_seconds() / 60 if intent.duration else None


# (phrase, expected action, checker(intent) -> bool, description)
CASES = [
    # --- the phrasings from the brief ---------------------------------
    ("set an alarm for four p m", "set_alarm", lambda i: _hhmm(i) == "2026-08-10 16:00", "4pm today"),
    ("set a timer for three minutes", "set_timer", lambda i: _mins(i) == 3, "3 min"),
    ("cancel alarm one", "cancel", lambda i: i.target == "alarm1", "cancel alarm1"),
    ("add ten minutes to timer one", "add_time", lambda i: i.target == "timer1" and _mins(i) == 10, "+10m timer1"),

    # --- alarm time variants ------------------------------------------
    ("set an alarm for four thirty p m", "set_alarm", lambda i: _hhmm(i) == "2026-08-10 16:30", "4:30pm"),
    ("set an alarm for seven a m", "set_alarm", lambda i: _hhmm(i) == "2026-08-11 07:00", "7am rolls to tomorrow"),
    ("set an alarm for six thirty in the morning", "set_alarm", lambda i: _hhmm(i) == "2026-08-11 06:30", "6:30am"),
    ("set an alarm for noon", "set_alarm", lambda i: _hhmm(i) == "2026-08-11 12:00", "noon rolls over"),
    ("set an alarm for midnight", "set_alarm", lambda i: _hhmm(i) == "2026-08-11 00:00", "midnight"),
    ("set an alarm for quarter past four", "set_alarm", lambda i: _hhmm(i) == "2026-08-10 16:15", "quarter past 4"),
    ("set an alarm for half past nine", "set_alarm", lambda i: _hhmm(i) == "2026-08-10 21:30", "half past 9"),
    ("set an alarm for five", "set_alarm", lambda i: _hhmm(i) == "2026-08-10 17:00", "bare hour picks soonest"),
    ("set an alarm for eleven fifteen p m", "set_alarm", lambda i: _hhmm(i) == "2026-08-10 23:15", "11:15pm"),
    ("wake me up at six a m", "set_alarm", lambda i: _hhmm(i) == "2026-08-11 06:00", "wake me up phrasing"),

    # --- timer duration variants --------------------------------------
    ("set a timer for ninety seconds", "set_timer", lambda i: _mins(i) == 1.5, "90 sec"),
    ("set a timer for one hour", "set_timer", lambda i: _mins(i) == 60, "1 hour"),
    ("set a timer for two hours thirty minutes", "set_timer", lambda i: _mins(i) == 150, "2h30m"),
    ("set a timer for twenty five minutes", "set_timer", lambda i: _mins(i) == 25, "25 min"),
    ("set a timer for half an hour", "set_timer", lambda i: _mins(i) == 30, "half an hour"),
    ("timer for five", "set_timer", lambda i: _mins(i) == 5, "bare number means minutes"),
    ("start a timer for forty five seconds", "set_timer", lambda i: _mins(i) == 0.75, "45 sec"),

    # --- cancelling ----------------------------------------------------
    ("cancel timer two", "cancel", lambda i: i.target == "timer2", "cancel timer2"),
    ("delete alarm three", "cancel", lambda i: i.target == "alarm3", "delete alarm3"),
    ("cancel all alarms", "clear", lambda i: i.kind == "alarm", "clear alarms"),
    ("cancel all timers", "clear", lambda i: i.kind == "timer", "clear timers"),

    # --- adjusting ------------------------------------------------------
    ("add five minutes to timer two", "add_time", lambda i: i.target == "timer2" and _mins(i) == 5, "+5m timer2"),
    ("add one hour to alarm one", "add_time", lambda i: i.target == "alarm1" and _mins(i) == 60, "+1h alarm1"),
    ("extend timer two by five minutes", "add_time", lambda i: i.target == "timer2" and _mins(i) == 5, "extend by"),
    ("add ten minutes", "add_time", lambda i: _mins(i) == 10, "bare add hits newest"),
    # "add" also starts things — this must NOT be read as an adjustment.
    ("add a timer for three minutes", "set_timer", lambda i: _mins(i) == 3, "add a timer creates"),
    ("add an alarm for six p m", "set_alarm", lambda i: _hhmm(i) == "2026-08-10 18:00", "add an alarm creates"),

    # --- control --------------------------------------------------------
    ("pause timer one", "pause", lambda i: i.target == "timer1", "pause"),
    ("resume timer one", "resume", lambda i: i.target == "timer1", "resume"),
    ("snooze", "snooze", lambda i: True, "snooze"),
    ("dismiss", "dismiss", lambda i: True, "dismiss"),

    # --- stopwatch ------------------------------------------------------
    ("start a stopwatch", "set_stopwatch", lambda i: True, "start a stopwatch"),
    ("start a watch", "set_stopwatch", lambda i: True, "'watch' works too"),
    ("count up", "set_stopwatch", lambda i: True, "count up"),
    ("start counting", "set_stopwatch", lambda i: True, "start counting"),
    ("cancel stopwatch one", "cancel", lambda i: i.target == "stopwatch1", "cancel stopwatch1"),
    # The panel says "Watch1", so that is what people will say back to it.
    ("cancel watch one", "cancel", lambda i: i.target == "stopwatch1", "'watch one' resolves"),
    ("cancel watch two", "cancel", lambda i: i.target == "stopwatch2", "'watch two' resolves"),
    ("pause the stopwatch", "pause", lambda i: i.target == "stopwatch", "pause stopwatch"),
    ("resume watch one", "resume", lambda i: i.target == "stopwatch1", "resume watch1"),
    ("cancel all stopwatches", "clear", lambda i: i.kind == "stopwatch", "clear stopwatches"),
    ("add five minutes to watch one", "add_time",
     lambda i: i.target == "stopwatch1" and _mins(i) == 5, "+5m watch1"),
    # A stopwatch must not swallow the timer phrasings.
    ("set a timer for three minutes", "set_timer", lambda i: _mins(i) == 3, "timer still wins"),

    # --- power ----------------------------------------------------------
    ("shut down", "power", lambda i: i.extras["mode"] == "shutdown", "shut down"),
    ("shutdown", "power", lambda i: i.extras["mode"] == "shutdown", "one word"),
    ("power off", "power", lambda i: i.extras["mode"] == "shutdown", "power off"),
    ("power down", "power", lambda i: i.extras["mode"] == "shutdown", "power down"),
    ("reboot", "power", lambda i: i.extras["mode"] == "reboot", "reboot"),
    ("restart", "power", lambda i: i.extras["mode"] == "reboot", "restart"),
    ("yes", "confirm", lambda i: True, "confirm"),
    ("no", "deny", lambda i: True, "deny"),
    # Half a power phrase must not cut the power.  These words all overlap
    # with real commands, and each must keep the meaning it already had.
    ("shut up", "dismiss", lambda i: True, "'shut up' still dismisses"),
    ("turn it off", "dismiss", lambda i: True, "'off' alone still dismisses"),
    ("stop timer one", "cancel", lambda i: i.target == "timer1", "'stop' is not shutdown"),
    ("restart timer one", "unknown", lambda i: True, "'restart timer' is not a reboot"),

    # --- garbage should not become a command ----------------------------
    ("the quick brown fox", "unknown", lambda i: True, "nonsense rejected"),
    ("", "unknown", lambda i: True, "empty rejected"),
]


def run_lifecycle_checks(verbose: bool = True) -> bool:
    """Drive a timer and an alarm through fire -> ring -> linger -> removal."""
    from .entries import EntryState, EntryStore

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            if verbose:
                print(f"  ok    {label}")
        else:
            failures.append(label)
            print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")

    # 60s ring, 300s linger — the shipped defaults, driven with a fake clock.
    store = EntryStore(linger_seconds=300.0, ring_seconds=30.0)
    t0 = datetime(2026, 8, 10, 14, 30, 0)

    timer = store.add_timer(timedelta(minutes=3))
    timer.started_at = t0
    alarm = store.add_alarm(t0 + timedelta(minutes=5))

    check("names follow the brief", timer.name == "Timer1" and alarm.name == "Alarm1",
          f"{timer.name} {alarm.name}")
    check("timer renders as HH:MM:SS", timer.detail(t0, 12, "hms") == "00:03:00",
          timer.detail(t0, 12, "hms"))
    check("alarm renders as h:mmam/pm", alarm.detail(t0, 12, "hms") == "2:35pm",
          alarm.detail(t0, 12, "hms"))

    store.tick(t0 + timedelta(minutes=1))
    check("counts down", timer.detail(t0 + timedelta(minutes=1), 12, "hms") == "00:02:00",
          timer.detail(t0 + timedelta(minutes=1), 12, "hms"))

    fired = store.tick(t0 + timedelta(minutes=3, seconds=1))
    check("timer fires at zero", [e.name for e in fired] == ["Timer1"], str(fired))
    check("timer is ringing", timer.state is EntryState.RINGING, timer.state)

    store.tick(t0 + timedelta(minutes=3, seconds=45))
    check("ring stops after ring_seconds", timer.state is EntryState.EXPIRED, timer.state)
    check("but stays on screen", len(store) == 2, len(store))

    fired = store.tick(t0 + timedelta(minutes=5, seconds=1))
    check("alarm fires at its time", [e.name for e in fired] == ["Alarm1"], str(fired))

    # Timer fired at t0+3m, so it goes at t0+3m+5m = t0+8m.
    store.tick(t0 + timedelta(minutes=7, seconds=59))
    check("still there just before 5 min", len(store) == 2, len(store))
    store.tick(t0 + timedelta(minutes=8, seconds=1))
    check("timer gone 5 min after firing", [e.name for e in store.all()] == ["Alarm1"],
          str([e.name for e in store.all()]))
    store.tick(t0 + timedelta(minutes=10, seconds=2))
    check("alarm gone 5 min after firing", len(store) == 0, len(store))

    # Index reuse: with Timer1 gone, the next timer should be Timer1 again.
    again = store.add_timer(timedelta(minutes=1))
    check("indexes are reused once free", again.name == "Timer1", again.name)

    # Pause/resume must not lose time.
    store2 = EntryStore()
    paused = store2.add_timer(timedelta(minutes=10))
    paused.started_at = t0
    paused.pause(t0 + timedelta(minutes=4))
    check("pause freezes the countdown",
          paused.detail(t0 + timedelta(minutes=9), 12, "hms") == "00:06:00",
          paused.detail(t0 + timedelta(minutes=9), 12, "hms"))
    paused.resume(t0 + timedelta(minutes=9))
    check("resume continues from where it stopped",
          paused.detail(t0 + timedelta(minutes=10), 12, "hms") == "00:05:00",
          paused.detail(t0 + timedelta(minutes=10), 12, "hms"))

    # Adding time to a ringing entry should revive it.
    revived = store2.add_timer(timedelta(minutes=1))
    revived.started_at = t0
    store2.tick(t0 + timedelta(minutes=1, seconds=1))
    check("revive: ringing first", revived.state is EntryState.RINGING, revived.state)
    revived.add(timedelta(minutes=5), now=t0 + timedelta(minutes=1, seconds=1))
    check("adding time un-rings it", revived.state is EntryState.PENDING, revived.state)

    # --- stopwatch: counts up, never fires, never lingers away -------------
    store3 = EntryStore(linger_seconds=300.0, ring_seconds=30.0)
    watch = store3.add_stopwatch()
    watch.started_at = t0

    check("stopwatch is labelled Watch1", watch.name == "Watch1", watch.name)
    check("stopwatch starts at zero", watch.detail(t0, 12, "hms") == "00:00:00",
          watch.detail(t0, 12, "hms"))
    check("stopwatch counts up",
          watch.detail(t0 + timedelta(minutes=2, seconds=5), 12, "hms") == "00:02:05",
          watch.detail(t0 + timedelta(minutes=2, seconds=5), 12, "hms"))

    fired = store3.tick(t0 + timedelta(hours=2))
    check("stopwatch never fires", fired == [] and watch.state is EntryState.PENDING,
          f"{fired} {watch.state}")
    check("stopwatch never lingers away", len(store3) == 1, len(store3))
    check("stopwatch keeps running past an hour",
          watch.detail(t0 + timedelta(hours=2), 12, "hms") == "02:00:00",
          watch.detail(t0 + timedelta(hours=2), 12, "hms"))

    watch.pause(t0 + timedelta(minutes=3))
    check("stopwatch pauses",
          watch.detail(t0 + timedelta(minutes=9), 12, "hms") == "00:03:00",
          watch.detail(t0 + timedelta(minutes=9), 12, "hms"))
    watch.resume(t0 + timedelta(minutes=9))
    check("stopwatch resumes where it stopped",
          watch.detail(t0 + timedelta(minutes=11), 12, "hms") == "00:05:00",
          watch.detail(t0 + timedelta(minutes=11), 12, "hms"))

    # "add ten minutes to watch one" shifts the reading, not a deadline.
    watch.add(timedelta(minutes=10), now=t0 + timedelta(minutes=11))
    check("adding time advances the count",
          watch.detail(t0 + timedelta(minutes=11), 12, "hms") == "00:15:00",
          watch.detail(t0 + timedelta(minutes=11), 12, "hms"))
    watch.add(timedelta(minutes=-99), now=t0 + timedelta(minutes=11))
    check("the count cannot go negative",
          watch.detail(t0 + timedelta(minutes=11), 12, "hms") == "00:00:00",
          watch.detail(t0 + timedelta(minutes=11), 12, "hms"))

    # "watch one" has to reach it: the panel never says "stopwatch".
    check("found by its canonical key", store3.find("stopwatch1") is watch)
    check("found by the name on screen", store3.find("watch1") is watch)
    check("found by bare kind", store3.find("watch") is watch)

    # A stopwatch has no deadline, so it must not out-rank things that do.
    store3.add_timer(timedelta(hours=4))
    order = [e.name for e in store3.all()]
    check("counters sort below countdowns", order == ["Timer1", "Watch1"], str(order))

    # It must survive a restart mid-count.
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "state.json"
        store3.save(path)
        reloaded = EntryStore(linger_seconds=300.0, ring_seconds=30.0)
        reloaded.load(path)
        again_watch = reloaded.find("watch1")
        check("stopwatch survives a save/load",
              again_watch is not None and again_watch.name == "Watch1",
              str([e.name for e in reloaded.all()]))
        if again_watch is not None:
            check("and keeps its reading",
                  again_watch.elapsed(datetime.now()).total_seconds()
                  - watch.elapsed(datetime.now()).total_seconds() < 1.0)

    print(f"\n{len(failures)} lifecycle failure(s)")
    return not failures


def run_power_checks(verbose: bool = True) -> bool:
    """The confirmation gate in front of shutdown and reboot.

    This is the only code path that can switch the machine off, and the only
    one whose failure mode is a walk to the plug, so it is checked without
    hardware: the app object is built field by field rather than constructed,
    since a real one wants the panel.
    """
    import time as _time

    from .app import ClockApp
    from .config import Config
    from .entries import EntryStore
    from .intents import parse

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            if verbose:
                print(f"  ok    {label}")
        else:
            failures.append(label)
            print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")

    class _Buzzer:
        def chirp(self):
            pass

    def make_app(armed: str | None = None, window: float = 10.0) -> ClockApp:
        app = ClockApp.__new__(ClockApp)
        app.cfg = Config.load()
        app.store = EntryStore()
        app.buzzer = _Buzzer()
        app.toasts = []
        app.fired = []
        app.toast = lambda text, color=None, seconds=None: app.toasts.append(text)
        app._power_go = lambda: app.fired.append(app._power_pending)
        app._power_pending = armed
        app._power_until = _time.monotonic() + window if armed else 0.0
        return app

    # Saying it once only asks the question.
    app = make_app()
    app._on_intent(parse("shut down"))
    check("'shut down' only arms", app._power_pending == "shutdown" and not app.fired,
          f"pending={app._power_pending} fired={app.fired}")
    check("and puts the question on screen",
          any("yes" in t for t in app.toasts), str(app.toasts))

    # Saying it twice is not a confirmation — only "yes" is.
    app = make_app("shutdown")
    app._on_intent(parse("shut down"))
    check("repeating it does not confirm", not app.fired, str(app.fired))

    app = make_app("shutdown")
    app._on_intent(parse("yes"))
    check("'yes' confirms", app.fired == ["shutdown"], str(app.fired))

    app = make_app("reboot")
    app._on_intent(parse("yes"))
    check("reboot confirms as reboot", app.fired == ["reboot"], str(app.fired))

    # Anything else stands it down.
    for phrase in ("no", "set a timer for three minutes", "the quick brown fox"):
        app = make_app("shutdown")
        app._on_intent(parse(phrase))
        check(f"{phrase!r} cancels it", not app.fired and app._power_pending is None,
              f"pending={app._power_pending} fired={app.fired}")

    # A real command given while armed still has to be obeyed, not swallowed.
    app = make_app("shutdown")
    app._on_intent(parse("set a timer for three minutes"))
    check("the cancelling command still runs", len(app.store) == 1, len(app.store))

    # The window closes, so a "yes" overheard minutes later does nothing.
    app = make_app("shutdown", window=-1.0)
    app._on_intent(parse("yes"))
    check("confirmation expires", not app.fired, str(app.fired))

    # And an unprompted "yes" is not a licence to act.
    app = make_app()
    app._on_intent(parse("yes"))
    check("'yes' out of the blue does nothing", not app.fired, str(app.fired))

    # The whole feature can be switched off.
    app = make_app()
    app.cfg.data["power"]["enabled"] = False
    app._on_intent(parse("shut down"))
    check("power.enabled=false refuses", app._power_pending is None, app._power_pending)

    print(f"\n{len(failures)} power failure(s)")
    return not failures


def run_layout_checks(verbose: bool = True) -> bool:
    """Nothing on the clock face may move except the thing that changed."""
    from .textrender import fit

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            if verbose:
                print(f"  ok    {label}")
        else:
            failures.append(label)
            print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")

    faces = [p for _, p in _candidate_faces() if p]
    if not faces:
        print("  (no outline fonts installed; skipping)")
        return True

    def lit_columns(image) -> set[int]:
        px = image.load()
        return {
            x for x in range(image.width)
            for y in range(image.height) if any(px[x, y])
        }

    for path in faces:
        name = path.rsplit("/", 1)[-1]
        font = fit(path, "88:88", 120, 44, True)
        if font is None:
            continue

        # The colon blinking must not move the digits.
        on = font.render("7:54", (255, 255, 255))
        off = font.render("7:54", (255, 255, 255), hide=":")
        check(f"{name}: colon blink keeps width", on.width == off.width,
              f"{on.width} vs {off.width}")
        # Every lit column when the colon is off must also be lit when it is
        # on; the difference must be only the colon's own columns.
        moved = lit_columns(off) - lit_columns(on)
        check(f"{name}: colon blink moves no digits", not moved, f"columns {sorted(moved)[:6]}")

        # Digit pitch must be fixed, or the minutes twitch as they count.
        widths = {font.measure(f"7:5{d}") for d in "0123456789"}
        check(f"{name}: digits are fixed pitch", len(widths) == 1, str(sorted(widths)))
        check(f"{name}: 1 and 8 share a cell",
              font.cell_width("1") == font.cell_width("8"),
              f"{font.cell_width('1')} vs {font.cell_width('8')}")

    # The whole point of the seconds bar: across a full minute the only thing
    # allowed to change is the bar itself.  Nothing in the digits may move.
    from unittest.mock import patch

    from .backends import PreviewBackend
    from .config import Config
    from .display import Renderer

    cfg = Config.load()
    bar_h = int(cfg.get("display.seconds_bar_height", 2))
    base = datetime(2026, 8, 10, 8, 16, 0)

    from .entries import Timer

    def _sample_timer() -> Timer:
        return Timer(
            index=1, created_at=base, duration=timedelta(minutes=3), started_at=base,
        )

    # In compact mode only the clock band is examined: the timer rows below it
    # count down every second, which is the whole point of them.
    for label, entries, band in (
        ("full screen", [], None),
        ("compact", [_sample_timer()], 16),
    ):
        frames = {}
        for secs in range(0, 60, 7):
            backend = PreviewBackend(cfg, scale=1, grid=False)
            renderer = Renderer(cfg, backend)
            with patch("ledclock.display.datetime") as dt:
                dt.now.return_value = base.replace(second=secs)
                renderer.render(entries, {})
            px = backend.image.load()
            limit = band if band is not None else backend.height
            frames[secs] = [
                tuple(px[x, y] for x in range(backend.width))
                for y in range(limit)
            ]

        reference = frames[0]
        changed_rows = set()
        for secs, rows in frames.items():
            for y, row in enumerate(rows):
                if row != reference[y]:
                    changed_rows.add(y)
        check(
            f"{label}: only the bar changes over a minute",
            len(changed_rows) <= bar_h,
            f"{len(changed_rows)} rows changed: {sorted(changed_rows)}",
        )

    # The seconds dots must be a literal count, not a gauge: 60 dots in six
    # groups of ten, with exactly one going dark per second.
    on_rgb = tuple(cfg.color("display.seconds_bar_color"))
    off_rgb = tuple(cfg.color("display.seconds_bar_track_color"))

    def frame(secs: int, entries) -> list:
        backend = PreviewBackend(cfg, scale=1, grid=False)
        renderer = Renderer(cfg, backend)
        with patch("ledclock.display.datetime") as dt:
            dt.now.return_value = base.replace(second=secs)
            renderer.render(list(entries), {})
        px = backend.image.load()
        return [[px[x, y] for x in range(backend.width)] for y in range(backend.height)]

    for label, entries in (("full screen", []), ("compact", [_sample_timer()])):
        # Locate the bar by what changes across the minute rather than by
        # colour: antialiased digit edges can land on exactly the track
        # colour, and the lit colour is the clock's own.
        first, last = frame(0, entries), frame(59, entries)
        bar_rows = [y for y, row in enumerate(first) if row != last[y]]
        if not bar_rows:
            check(f"{label}: seconds dots found", False, "no row changed over the minute")
            continue
        y = bar_rows[0]

        counts_ok = totals_ok = True
        for secs in (0, 1, 17, 30, 45, 59):
            row = frame(secs, entries)[y]
            lit, unlit = row.count(on_rgb), row.count(off_rgb)
            counts_ok &= lit == 60 - secs
            totals_ok &= (lit + unlit) == 60
        check(f"{label}: one dot per second remaining", counts_ok)
        check(f"{label}: always 60 dots", totals_ok)

        runs, run = [], 0
        for cell in frame(0, entries)[y]:
            if cell in (on_rgb, off_rgb):
                run += 1
            elif run:
                runs.append(run)
                run = 0
        if run:
            runs.append(run)
        check(f"{label}: six groups of ten", runs == [10] * 6, str(runs))

    print(f"\n{len(failures)} layout failure(s)")
    return not failures


def _candidate_faces():
    from .preview import CANDIDATES

    from pathlib import Path
    return [(label, path) for label, path in CANDIDATES if path and Path(path).is_file()]


def run_intent_checks(verbose: bool = True) -> bool:
    passed = failed = 0
    for phrase, expect_action, check, label in CASES:
        intent = parse(phrase, now=NOW)
        ok = intent.action == expect_action
        detail = ""
        if ok:
            try:
                ok = bool(check(intent))
            except Exception as exc:
                ok, detail = False, f" ({exc})"
        if not ok:
            detail = detail or f" (got action={intent.action} when={_hhmm(intent)} dur={_mins(intent)} target={intent.target})"

        if ok:
            passed += 1
            if verbose:
                print(f"  ok    {label:<28} <- {phrase!r}")
        else:
            failed += 1
            print(f"  FAIL  {label:<28} <- {phrase!r}{detail}")

    print(f"\n{passed} passed, {failed} failed  (reference time {NOW:%Y-%m-%d %H:%M})")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run_intent_checks() else 1)
