"""Turn recognised speech into structured commands.

Vosk's small English model emits lowercase words with no punctuation and
spells numbers out ("four p m", "twenty five"), so everything here works on a
normalised token list rather than raw text.

The vocabulary defined at the bottom doubles as the recognizer grammar: giving
Vosk a closed word list dramatically improves accuracy on a small model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

UNITS = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}

SECOND_WORDS = {"second", "seconds", "sec", "secs"}
MINUTE_WORDS = {"minute", "minutes", "min", "mins"}
HOUR_WORDS = {"hour", "hours", "hr", "hrs"}
UNIT_SECONDS = {**{w: 1 for w in SECOND_WORDS}, **{w: 60 for w in MINUTE_WORDS},
                **{w: 3600 for w in HOUR_WORDS}}

# Words that carry no meaning for us but that people naturally say.
FILLER = {
    "a", "an", "the", "please", "can", "you", "could", "would", "will",
    "me", "my", "i", "want", "need", "let's", "lets", "just", "go", "ahead",
    "up", "new", "another", "of", "there", "is", "it", "that", "this",
}

_CANCEL = {"cancel", "delete", "remove", "kill", "clear", "stop", "abort", "scrap"}
_CREATE = {"set", "start", "create", "make", "add", "begin", "put", "new"}


@dataclass
class Intent:
    """A parsed command.  ``action`` drives dispatch in :mod:`ledclock.app`."""

    action: str
    target: str | None = None          # e.g. "timer1", "alarm2", "timer", "alarm"
    when: datetime | None = None       # absolute time, for set_alarm
    duration: timedelta | None = None  # for set_timer / add_time / subtract_time
    kind: str | None = None            # "alarm" / "timer" for clear
    text: str = ""                     # what we think we heard
    confidence: float = 1.0
    extras: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.action != "unknown"


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

_MERIDIEM_FIXUPS = [
    (r"\bp\s+m\b", "pm"), (r"\ba\s+m\b", "am"),
    (r"\bp\.m\.?\b", "pm"), (r"\ba\.m\.?\b", "am"),
    (r"\bo\s*'?\s*clock\b", "oclock"),
    (r"\bin\s+the\s+morning\b", "am"),
    (r"\bin\s+the\s+(afternoon|evening)\b", "pm"),
    (r"\bat\s+night\b", "pm"),
    (r"\bhalf\s+past\b", "thirty past"),
    (r"\bquarter\s+past\b", "fifteen past"),
    (r"\bquarter\s+to\b", "fifteen to"),
    (r"\bhalf\s+an?\s+hour\b", "thirty minutes"),
    (r"\bhalf\s+hour\b", "thirty minutes"),
    (r"\ban?\s+hour\b", "one hour"),
    (r"\ban?\s+minute\b", "one minute"),
]


def normalise(text: str) -> list[str]:
    """Lowercase, expand spoken punctuation, split into tokens."""
    t = text.lower().strip()
    t = t.replace("-", " ")
    for pattern, repl in _MERIDIEM_FIXUPS:
        t = re.sub(pattern, repl, t)
    # "alarm one" and "alarm 1" and "alarm1" all become "alarm1"
    t = re.sub(r"\b(alarm|timer)s?\s+(?:number\s+)?(\d+)\b", r"\1\2", t)
    t = re.sub(r"[^a-z0-9:\s]", " ", t)
    return [w for w in t.split() if w]


def _strip_filler(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in FILLER]


def parse_number(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Read one spoken number starting at ``tokens[i]``.

    Returns ``(value, next_index)``.  Handles "twenty five", "1 5" (which Vosk
    sometimes produces for "fifteen"), plain digits and ordinals.
    """
    if i >= len(tokens):
        return None, i
    tok = tokens[i]

    if tok.isdigit():
        return int(tok), i + 1
    if tok in ORDINALS:
        return ORDINALS[tok], i + 1
    if tok in TENS:
        value = TENS[tok]
        # "twenty five" -> 25
        if i + 1 < len(tokens) and tokens[i + 1] in UNITS and 1 <= UNITS[tokens[i + 1]] <= 9:
            return value + UNITS[tokens[i + 1]], i + 2
        return value, i + 1
    if tok in UNITS:
        return UNITS[tok], i + 1
    return None, i


def parse_duration(tokens: list[str]) -> timedelta | None:
    """Sum every ``<number> <unit>`` pair found: "one hour thirty minutes"."""
    total = 0.0
    found = False
    pending: int | None = None
    i = 0
    while i < len(tokens):
        value, nxt = parse_number(tokens, i)
        if value is not None:
            # A number with no unit yet; the unit that follows consumes it.
            # "one hour thirty" leaves 30 pending for the fallback below.
            pending = value
            i = nxt
            continue
        tok = tokens[i]
        if tok in UNIT_SECONDS and pending is not None:
            total += pending * UNIT_SECONDS[tok]
            pending = None
            found = True
        i += 1

    if pending is not None and not found:
        # "set a timer for five" -> five minutes.
        total += pending * 60
        found = True
    elif pending is not None and found:
        # Trailing bare number after an hour: "one hour thirty" -> +30 min.
        total += pending * 60

    return timedelta(seconds=total) if found and total > 0 else None


def parse_time_of_day(tokens: list[str], now: datetime | None = None) -> datetime | None:
    """Resolve a spoken clock time to the next matching wall-clock instant."""
    now = now or datetime.now()

    if "noon" in tokens or "midday" in tokens:
        return _next_occurrence(now, 12, 0)
    if "midnight" in tokens:
        return _next_occurrence(now, 0, 0)

    meridiem = None
    if "pm" in tokens:
        meridiem = "pm"
    elif "am" in tokens:
        meridiem = "am"
    tomorrow = "tomorrow" in tokens

    # "fifteen past four" / "fifteen to five"
    for joiner, sign in (("past", 1), ("to", -1)):
        if joiner in tokens:
            j = tokens.index(joiner)
            mins, _ = parse_number(tokens, 0)
            hour, _ = parse_number(tokens, j + 1)
            if mins is not None and hour is not None:
                base_min = mins if sign > 0 else 60 - mins
                if sign < 0:
                    hour -= 1
                hour = _apply_meridiem(hour, meridiem, now, base_min)
                return _next_occurrence(now, hour % 24, base_min % 60, tomorrow)

    # Bare "16:30" style, if a digit token survived.
    for tok in tokens:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", tok)
        if m:
            return _next_occurrence(now, int(m.group(1)) % 24, int(m.group(2)) % 60, tomorrow)

    # "<hour> [<minute>] [am|pm]"
    i = 0
    while i < len(tokens):
        hour, nxt = parse_number(tokens, i)
        if hour is None or not (0 <= hour <= 24):
            i += 1
            continue
        minute = 0
        if nxt < len(tokens) and tokens[nxt] not in ("am", "pm", "oclock", "tomorrow"):
            cand, after = parse_number(tokens, nxt)
            if cand is not None and 0 <= cand < 60:
                minute, nxt = cand, after
        hour = _apply_meridiem(hour, meridiem, now, minute)
        return _next_occurrence(now, hour % 24, minute % 60, tomorrow)

    return None


def _apply_meridiem(hour: int, meridiem: str | None, now: datetime, minute: int) -> int:
    if meridiem == "pm":
        return hour + 12 if hour < 12 else hour
    if meridiem == "am":
        return 0 if hour == 12 else hour
    if hour > 12:
        return hour  # already 24-hour
    # No am/pm given: pick whichever of the two readings comes soonest.
    today = now.replace(second=0, microsecond=0)
    options = []
    for candidate in {hour % 24, (hour + 12) % 24}:
        when = today.replace(hour=candidate, minute=minute)
        if when <= now:
            when += timedelta(days=1)
        options.append((when, candidate))
    return min(options)[1]


def _next_occurrence(now: datetime, hour: int, minute: int, tomorrow: bool = False) -> datetime:
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow:
        when += timedelta(days=1)
    elif when <= now:
        when += timedelta(days=1)
    return when


def _find_target(tokens: list[str]) -> str | None:
    """Pull "timer1" / "alarm2" / a bare "timer" out of the tokens."""
    for tok in tokens:
        m = re.fullmatch(r"(alarm|timer)(\d+)", tok)
        if m:
            return f"{m.group(1)}{m.group(2)}"
    for i, tok in enumerate(tokens):
        if tok in ("alarm", "timer", "alarms", "timers"):
            kind = tok.rstrip("s")
            value, _ = parse_number(tokens, i + 1)
            return f"{kind}{value}" if value is not None else kind
    return None


# --------------------------------------------------------------------------
# top-level parse
# --------------------------------------------------------------------------

def parse(text: str, now: datetime | None = None) -> Intent:
    """Parse one command utterance into an :class:`Intent`."""
    now = now or datetime.now()
    raw_tokens = normalise(text)
    tokens = _strip_filler(raw_tokens)
    if not tokens:
        return Intent("unknown", text=text)

    words = set(tokens)
    target = _find_target(tokens)
    plural = any(t in ("alarms", "timers", "everything", "all") for t in tokens)

    # --- dismiss / snooze -------------------------------------------------
    if words & {"snooze"}:
        return Intent("snooze", target=target, text=text)
    if words & {"dismiss", "silence", "quiet", "shut", "hush", "off"} and not (words & _CREATE):
        return Intent("dismiss", target=target, text=text)

    # --- pause / resume ---------------------------------------------------
    if "pause" in words or "hold" in words:
        return Intent("pause", target=target or "timer", text=text)
    if words & {"resume", "unpause", "continue"}:
        return Intent("resume", target=target or "timer", text=text)

    # --- cancel -----------------------------------------------------------
    if words & _CANCEL:
        if plural and ("all" in words or "everything" in words or target in ("alarm", "timer", None)):
            kind = None
            if "alarms" in words or "alarm" in words:
                kind = "alarm"
            elif "timers" in words or "timer" in words:
                kind = "timer"
            return Intent("clear", kind=kind, text=text)
        return Intent("cancel", target=target, text=text)

    # --- add / subtract time ---------------------------------------------
    # "add" is ambiguous: "add ten minutes to timer one" adjusts an existing
    # entry, but "add a timer for three minutes" creates one.  Read it as an
    # adjustment only when the phrase points at something that already exists:
    # an explicit "to <entry>", an indexed name, or no entry noun at all.
    named = target not in (None, "timer", "alarm")
    adjusting = "to" in tokens or named or not (words & {"alarm", "timer", "alarms", "timers"})
    if words & {"add", "extend", "plus", "more"} and adjusting:
        delta = parse_duration(_duration_slice(tokens))
        if delta:
            return Intent("add_time", target=target or "timer", duration=delta, text=text)
    if words & {"subtract", "minus", "reduce", "shorten", "take"}:
        delta = parse_duration(_duration_slice(tokens))
        if delta:
            return Intent("add_time", target=target or "timer", duration=-delta, text=text)

    # --- create -----------------------------------------------------------
    wants_alarm = "alarm" in words or bool(words & {"wake", "remind"})
    wants_timer = "timer" in words or "countdown" in words

    if wants_timer and not wants_alarm:
        delta = parse_duration(_duration_slice(tokens))
        if delta:
            return Intent("set_timer", duration=delta, text=text)
        return Intent("unknown", text=text, extras={"reason": "no duration heard"})

    if wants_alarm:
        when = parse_time_of_day(_time_slice(tokens), now)
        if when:
            return Intent("set_alarm", when=when, text=text)
        # "set an alarm for ten minutes" is really a timer.
        delta = parse_duration(_duration_slice(tokens))
        if delta:
            return Intent("set_alarm", when=now + delta, text=text)
        return Intent("unknown", text=text, extras={"reason": "no time heard"})

    # Bare "for three minutes" with no noun -> timer; "for four pm" -> alarm.
    if words & _CREATE or "for" in raw_tokens or "at" in raw_tokens:
        if words & {"am", "pm", "oclock", "noon", "midnight"}:
            when = parse_time_of_day(_time_slice(tokens), now)
            if when:
                return Intent("set_alarm", when=when, text=text)
        delta = parse_duration(_duration_slice(tokens))
        if delta:
            return Intent("set_timer", duration=delta, text=text)

    if words & {"status", "what", "list", "show"}:
        return Intent("status", text=text)

    return Intent("unknown", text=text)


_ENTRY_NOUNS = ("alarm", "timer", "alarms", "timers")


def _duration_slice(tokens: list[str]) -> list[str]:
    """Tokens with the entry name removed, ready for duration parsing.

    The entry name may sit either side of the duration — "set a timer for
    three minutes" versus "add ten minutes to timer one" — so we strip the
    noun and the index that follows it rather than truncating at it.  Without
    this the trailing "one" in the second phrase reads as another minute.
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if re.fullmatch(r"(alarm|timer)\d+", tok):
            i += 1
            continue
        if tok in _ENTRY_NOUNS:
            i += 1
            # Swallow the index, if one was spoken.
            value, nxt = parse_number(tokens, i)
            if value is not None:
                i = nxt
            continue
        out.append(tok)
        i += 1
    return out


def _time_slice(tokens: list[str]) -> list[str]:
    """Everything after "for"/"at", if present — that's where the time lives."""
    for anchor in ("for", "at"):
        if anchor in tokens:
            return tokens[tokens.index(anchor) + 1:]
    return [t for t in tokens if t not in ("alarm", "timer", "set", "wake", "remind")]


# --------------------------------------------------------------------------
# recognizer grammar
# --------------------------------------------------------------------------

def vocabulary(wake_word: str = "clock") -> list[str]:
    """Closed word list handed to Vosk as a grammar.

    ``[unk]`` lets the recognizer report out-of-vocabulary audio instead of
    force-fitting it to a real word, which keeps false wake-ups down.
    """
    words: set[str] = set()
    words.update(UNITS, TENS, ORDINALS)
    words.update(SECOND_WORDS, MINUTE_WORDS, HOUR_WORDS)
    words.update(FILLER, _CANCEL, _CREATE)
    words.update({
        wake_word, "alarm", "alarms", "timer", "timers", "countdown",
        "am", "pm", "o", "clock", "noon", "midnight", "midday",
        "morning", "afternoon", "evening", "night", "tonight", "tomorrow",
        "past", "to", "for", "at", "in", "and", "half", "quarter",
        "snooze", "dismiss", "silence", "quiet", "hush", "off",
        "pause", "resume", "unpause", "continue", "hold",
        "extend", "plus", "more", "minus", "subtract", "reduce", "shorten",
        "wake", "remind", "all", "everything", "number",
        "status", "what", "list", "show",
    })
    words.discard("")
    # Spelling variants the parser tolerates but the acoustic model has never
    # heard.  Leaving them in makes Vosk log a warning per word at startup.
    words -= {"fourty", "unpause", "lets", "let's"}
    # Multi-word tokens can't go in a Vosk grammar; split them out.
    flat = {w for word in words for w in str(word).split()}
    return sorted(flat) + ["[unk]"]
