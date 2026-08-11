"""Offline speech control via Vosk.

Audio comes from ``arecord`` on a pipe rather than PortAudio: the USB dongle
only offers 44.1/48 kHz, and ``plughw:`` makes ALSA resample to the 16 kHz
Vosk wants for free.  It also means one less native dependency to build.

Recognition runs continuously.  Each final utterance is checked for the wake
run ("clock clock clock"); anything after the last wake word is treated as the
command, so both of these work:

    "clock clock clock, set a timer for three minutes"    (one breath)
    "clock clock clock" ... "set a timer for three minutes" (two breaths)
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

from . import intents
from .config import Config

log = logging.getLogger(__name__)

CHUNK_BYTES = 4000  # ~125 ms of 16 kHz mono S16_LE
FULL_SCALE = 32768.0

# Vosk reports grammar words it has no pronunciation for like this, on stderr
# from C++, and then drops them silently.
_MISSING_RE = re.compile(r"Ignoring word missing in vocabulary: '([^']+)'")


def wake_tokens(phrase: str, min_repeats: int = 2) -> list[str]:
    """The word sequence that must land, in order, to wake the clock.

    One word repeated ("clock clock clock") is a tolerance setting rather
    than a literal: ``wake_min_repeats`` says how many of them actually have
    to arrive, because a mic that clips the first one should not cost you the
    command.  Any other phrase ("timekeeper", "hey timekeeper") is matched in
    full — there is no repetition to be tolerant about.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", str(phrase).lower()) if w]
    if not words:
        return ["clock"]
    if len(set(words)) == 1:
        return words[: max(1, min(len(words), int(min_repeats)))]
    return words


def wake_index(tokens: list[str], words: list[str]) -> int | None:
    """Index just past the wake phrase in ``words``, or None if absent."""
    n = len(tokens)
    if not n:
        return None
    for i in range(len(words) - n + 1):
        if words[i:i + n] == tokens:
            j = i + n
            # Swallow further repeats of the final word, so a fourth "clock"
            # is not mistaken for the start of the command.
            while j < len(words) and words[j] == tokens[-1]:
                j += 1
            return j
    return None


def _grammar_recognizer(model, rate: int, words: list[str]):
    """Build a grammar recognizer, and report which words Vosk threw away.

    A wake word the lexicon has never heard fails totally and silently: the
    clock simply never wakes, with nothing in the log to say why.  The only
    way to see it from Python is to catch the C++ log at the file descriptor.
    """
    import vosk

    with tempfile.TemporaryFile() as tmp:
        saved = os.dup(2)
        os.dup2(tmp.fileno(), 2)
        try:
            vosk.SetLogLevel(0)
            rec = vosk.KaldiRecognizer(model, rate, json.dumps(words))
        finally:
            vosk.SetLogLevel(-1)
            os.dup2(saved, 2)
            os.close(saved)
        tmp.seek(0)
        log = tmp.read().decode(errors="replace")
    return rec, set(_MISSING_RE.findall(log))


def amplify(data: bytes, gain: float) -> bytes:
    """Scale 16-bit samples, clamping instead of wrapping.

    Wrapping would turn a loud syllable into white noise, which is far worse
    for recognition than the flat top clamping gives you.
    """
    samples = array.array("h")
    samples.frombytes(data)
    for i, sample in enumerate(samples):
        scaled = int(sample * gain)
        samples[i] = 32767 if scaled > 32767 else -32768 if scaled < -32768 else scaled
    return samples.tobytes()


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(value / FULL_SCALE) if value >= 1.0 else -90.0


class VoiceListener:
    """Background wake-word + command recogniser."""

    def __init__(
        self,
        cfg: Config,
        on_intent: Callable[[intents.Intent], None],
        on_state: Callable[[bool, str], None] | None = None,
    ):
        c = cfg.section("voice")
        self.cfg = cfg
        self.on_intent = on_intent
        self.on_state = on_state or (lambda awake, text: None)

        self.enabled = bool(c.get("enabled", True))
        self.model_path = cfg.resolve(c.get("model_path"))
        self.device = str(c.get("device", "plughw:1,0"))
        self.rate = int(c.get("sample_rate", 16000))
        self.command_timeout = float(c.get("command_timeout", 8.0))
        self.use_grammar = bool(c.get("use_grammar", True))
        self.arecord = str(c.get("arecord", "arecord"))
        self.gain = max(0.1, float(c.get("gain", 1.0)))

        self.wake_tokens = wake_tokens(
            c.get("wake_phrase", "clock clock clock"),
            c.get("wake_min_repeats", 2),
        )

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._awake_until = 0.0
        self._proc: subprocess.Popen | None = None

    # ---------------- lifecycle ----------------

    def preflight(self) -> str | None:
        """Return a human-readable reason the listener can't run, or None."""
        if not self.enabled:
            return "disabled in config"
        if shutil.which(self.arecord) is None:
            return f"{self.arecord} not found (install alsa-utils)"
        if not self.model_path.is_dir():
            return f"vosk model missing at {self.model_path}"
        try:
            import vosk  # noqa: F401
        except ImportError:
            return "vosk not installed in this environment"
        return None

    def start(self) -> bool:
        reason = self.preflight()
        if reason:
            log.warning("voice control unavailable: %s", reason)
            self.enabled = False
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voice", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def awake(self) -> bool:
        return time.monotonic() < self._awake_until

    # ---------------- recognition ----------------

    def _build_recognizer(self):
        import vosk

        vosk.SetLogLevel(-1)
        model = vosk.Model(str(self.model_path))
        if not self.use_grammar:
            rec = vosk.KaldiRecognizer(model, self.rate)
            rec.SetWords(False)
            return model, rec

        rec, dropped = _grammar_recognizer(
            model, self.rate, intents.vocabulary(self.wake_tokens)
        )
        if dropped:
            log.warning("no pronunciation in the speech model for: %s",
                        ", ".join(sorted(dropped)))
        unheard = dropped & set(self.wake_tokens)
        if unheard:
            log.error(
                "wake word(s) %s are not in the model's lexicon, so the clock can "
                "never wake.  Choose another word, or split it up "
                "(\"time keeper\" rather than \"timekeeper\").  Check candidates "
                "with: python -m ledclock --check-wake \"<phrase>\"",
                ", ".join(sorted(unheard)),
            )
        rec.SetWords(False)
        return model, rec

    def _spawn_arecord(self) -> subprocess.Popen:
        cmd = [
            self.arecord, "-q", "-D", self.device,
            "-f", "S16_LE", "-r", str(self.rate), "-c", "1", "-t", "raw",
        ]
        log.info("capturing audio: %s", " ".join(cmd))
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )

    def _run(self) -> None:
        try:
            model, rec = self._build_recognizer()
        except Exception:
            log.exception("could not load the vosk model; voice control off")
            self.enabled = False
            return
        log.info("voice model loaded; wake phrase = %r", " ".join(self.wake_tokens))

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._proc = self._spawn_arecord()
                backoff = 1.0
                self._pump(rec)
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("audio capture failed; retrying in %.0fs", backoff)
            finally:
                proc, self._proc = self._proc, None
                if proc and proc.poll() is None:
                    proc.terminate()
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def _pump(self, rec) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        while not self._stop.is_set():
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                if self._stop.is_set():
                    return  # We terminated arecord ourselves; not an error.
                err = b""
                if proc.stderr is not None:
                    err = proc.stderr.read() or b""
                raise RuntimeError(
                    f"arecord ended (rc={proc.poll()}): {err.decode(errors='replace').strip()}"
                )

            if self.gain != 1.0:
                data = amplify(data, self.gain)

            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "").strip()
                if text:
                    self._handle_utterance(text)
            else:
                partial = json.loads(rec.PartialResult()).get("partial", "").strip()
                if partial and not self.awake and self._wake_index(partial.split()) is not None:
                    # Light the indicator as soon as we hear it, without
                    # waiting for the utterance to end.
                    self._wake()

            if self.awake is False and self._awake_until:
                self._awake_until = 0.0
                self.on_state(False, "")

    # ---------------- wake handling ----------------

    def _wake_index(self, words: list[str]) -> int | None:
        return wake_index(self.wake_tokens, words)

    def _wake(self) -> None:
        self._awake_until = time.monotonic() + self.command_timeout
        self.on_state(True, "")

    def _handle_utterance(self, text: str) -> None:
        words = text.split()
        after = self._wake_index(words)

        if after is not None:
            remainder = " ".join(words[after:]).strip()
            if remainder:
                self._awake_until = 0.0
                self._dispatch(remainder)
            else:
                self._wake()
            return

        if self.awake:
            self._awake_until = 0.0
            self._dispatch(text)

    def _dispatch(self, text: str) -> None:
        log.info("heard command: %r", text)
        intent = intents.parse(text)
        self.on_state(False, text)
        try:
            self.on_intent(intent)
        except Exception:
            log.exception("intent dispatch failed for %r", text)


def check_wake(cfg: Config, phrase: str | None = None) -> int:
    """Report whether a wake phrase can actually be recognised.

    Worth running before editing the config: a word the acoustic model has no
    pronunciation for is dropped from the grammar without complaint, and the
    clock then never wakes at all.
    """
    c = cfg.section("voice")
    raw = phrase if phrase is not None else c.get("wake_phrase", "clock clock clock")
    tokens = wake_tokens(raw, c.get("wake_min_repeats", 2))

    print(f"phrase:  {raw!r}")
    print(f"must hear: {' '.join(tokens)}")
    if len(set(tokens)) == 1 and len(tokens) > 1:
        print(f"           (one word repeated, so wake_min_repeats={len(tokens)} applies)")
    elif len(tokens) > 1:
        print("           (distinct words, matched in full — wake_min_repeats is ignored)")

    model_path = cfg.resolve(c.get("model_path"))
    if not model_path.is_dir():
        print(f"\ncannot check the lexicon: no model at {model_path}")
        return 1
    try:
        import vosk
    except ImportError:
        print("\ncannot check the lexicon: vosk is not installed here")
        return 1

    vosk.SetLogLevel(-1)
    model = vosk.Model(str(model_path))
    _, dropped = _grammar_recognizer(
        model, int(c.get("sample_rate", 16000)), intents.vocabulary(tokens)
    )
    print()
    for word in tokens:
        print(f"  {'NOT IN LEXICON' if word in dropped else 'ok':>14}  {word}")

    unheard = dropped & set(tokens)
    if unheard:
        print(f"\nthis phrase can never wake the clock: {', '.join(sorted(unheard))}")
        print("try splitting the word up (\"time keeper\"), or pick another.")
        return 1
    print("\nevery word is in the lexicon; this phrase will work")
    if len(tokens) == 1:
        print("note: a single word wakes on one hit.  If it triggers by itself, say it")
        print("      twice in wake_phrase and set wake_min_repeats = 2.")
    return 0


def meter(cfg: Config, seconds: float = 15.0) -> int:
    """Live capture-level meter, for setting mic gain from across the room.

    Speech that recognises reliably peaks somewhere around -12 dBFS.  Much
    quieter and Vosk is working from the noise floor; touching 0.0 means the
    samples are clipping, which sounds loud and recognises badly.  Walk to
    where you normally stand and watch the peak column.
    """
    listener = VoiceListener(cfg, lambda intent: None)
    if shutil.which(listener.arecord) is None:
        print(f"{listener.arecord} not found (install alsa-utils)")
        return 1

    print(f"device: {listener.device}   gain: x{listener.gain:g}")
    print(f"speak normally from where you use the clock, for {seconds:.0f}s\n")
    try:
        proc = listener._spawn_arecord()
    except OSError as exc:
        print(f"could not start capture: {exc}")
        return 1

    loudest = 0.0
    quietest = FULL_SCALE
    end = time.monotonic() + seconds
    # Redrawing one line is right at a terminal and unreadable in a pipe or a
    # log, where every update would land as another wall of text.
    live = sys.stdout.isatty()
    next_line = 0.0
    try:
        while time.monotonic() < end:
            data = proc.stdout.read(CHUNK_BYTES) if proc.stdout else b""
            if not data:
                err = (proc.stderr.read() if proc.stderr else b"") or b""
                print(f"\ncapture stopped: {err.decode(errors='replace').strip()}")
                print("if it says the device is busy, the clock already has the mic:")
                print("  sudo systemctl stop ledclock   # then re-run, and start it after")
                return 1
            if listener.gain != 1.0:
                data = amplify(data, listener.gain)
            samples = array.array("h")
            samples.frombytes(data)
            peak = float(max(abs(s) for s in samples))
            rms = math.sqrt(sum(float(s) * s for s in samples) / len(samples))
            loudest = max(loudest, peak)
            quietest = min(quietest, rms)
            now = time.monotonic()
            if not live and now < next_line:
                continue
            next_line = now + 1.0
            bar = "#" * max(0, min(40, round((_dbfs(peak) + 60.0) / 60.0 * 40)))
            line = (f"peak {_dbfs(peak):6.1f} dBFS  rms {_dbfs(rms):6.1f} dBFS "
                    f"|{bar:<40}|")
            print(f"\r{line}" if live else line, end="" if live else "\n", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()

    peak_db, floor_db = _dbfs(loudest), _dbfs(quietest)
    print(f"\n\nloudest peak {peak_db:.1f} dBFS, quietest rms {floor_db:.1f} dBFS "
          f"(headroom over the floor: {peak_db - floor_db:.0f} dB)")
    if peak_db > -1.0:
        print("clipping — lower the capture control or voice.gain")
    elif peak_db < -24.0:
        card = re.search(r"CARD=([^,]+)", listener.device)
        print("too quiet for reliable recognition.  Raise the ALSA capture control")
        print("first — it has a real preamp behind it — and only then voice.gain:")
        print(f"  amixer -c {card.group(1) if card else 0} sset Mic 100% cap")
        print(f"  voice.gain = {min(8.0, 10 ** ((-12.0 - peak_db) / 20.0)):.1f}  # if still quiet")
    else:
        print("healthy level")
    if peak_db - floor_db < 20.0:
        print("note: little separation between speech and the room — the limit here is")
        print("      the microphone, not the gain.  See 'Hearing you further away'.")
    return 0
