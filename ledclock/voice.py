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

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import intents
from .config import Config

log = logging.getLogger(__name__)

CHUNK_BYTES = 4000  # ~125 ms of 16 kHz mono S16_LE


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

        phrase = str(c.get("wake_phrase", "clock clock clock")).lower().split()
        self.wake_word = phrase[0] if phrase else "clock"
        self.wake_repeats = max(1, min(len(phrase) or 1, int(c.get("wake_min_repeats", 2))))

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
        if self.use_grammar:
            grammar = json.dumps(intents.vocabulary(self.wake_word))
            rec = vosk.KaldiRecognizer(model, self.rate, grammar)
        else:
            rec = vosk.KaldiRecognizer(model, self.rate)
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
        log.info("voice model loaded; wake phrase = %s", " ".join([self.wake_word] * self.wake_repeats))

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
        """Index just past the wake run, or None if it isn't there."""
        run = 0
        for i, word in enumerate(words):
            if word == self.wake_word:
                run += 1
                if run >= self.wake_repeats:
                    # Consume any further repeats.
                    j = i + 1
                    while j < len(words) and words[j] == self.wake_word:
                        j += 1
                    return j
            else:
                run = 0
        return None

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
