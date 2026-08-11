"""A local control socket.

Lets you drive the running clock without speaking::

    python -m ledclock --send "set a timer for 3 minutes"

Commands go through the identical parser and dispatch path the microphone
uses, so this exercises the real behaviour rather than a test-only shortcut —
handy when the panel is across the room, or the mic is misbehaving and you
want to know whether the fault is in recognition or in the clock.

The socket is a filesystem object owned by root with 0660 permissions and a
group the owning user is in, so it is reachable locally and nowhere else.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable

from . import intents

log = logging.getLogger(__name__)

DEFAULT_SOCKET = "/run/ledclock.sock"


class ControlServer:
    """Accepts one-line commands on a Unix domain socket."""

    def __init__(
        self,
        path: str | Path,
        on_intent: Callable[[intents.Intent], None],
        group: str | None = None,
    ):
        self.path = Path(path)
        self.on_intent = on_intent
        self.group = group
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self.path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(self.path))
            sock.listen(4)
            sock.settimeout(0.5)
            os.chmod(self.path, 0o660)
            if self.group:
                import grp

                try:
                    os.chown(self.path, 0, grp.getgrnam(self.group).gr_gid)
                except (KeyError, PermissionError) as exc:
                    log.debug("could not set socket group %s: %s", self.group, exc)
            self._sock = sock
        except OSError as exc:
            log.warning("control socket unavailable at %s: %s", self.path, exc)
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="control", daemon=True)
        self._thread.start()
        log.info("control socket listening on %s", self.path)
        return True

    def _run(self) -> None:
        # Hold our own reference: stop() clears self._sock, and reading it
        # mid-shutdown would raise AttributeError instead of exiting cleanly.
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2.0)
                    data = conn.recv(4096).decode("utf-8", "replace").strip()
                except (OSError, UnicodeError):
                    continue
                if not data:
                    continue
                log.info("control command: %r", data)
                intent = intents.parse(data)
                try:
                    self.on_intent(intent)
                    reply = f"{intent.action}"
                    if intent.target:
                        reply += f" {intent.target}"
                    if intent.duration:
                        reply += f" {intent.duration}"
                    if intent.when:
                        reply += f" {intent.when:%Y-%m-%d %H:%M}"
                except Exception as exc:
                    log.exception("control dispatch failed")
                    reply = f"error: {exc}"
                try:
                    conn.sendall((reply + "\n").encode())
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.path.unlink()
        except OSError:
            pass


def send(text: str, path: str | Path = DEFAULT_SOCKET, timeout: float = 3.0) -> str:
    """Send one command to a running clock and return its reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(text.encode())
        sock.shutdown(socket.SHUT_WR)
        return sock.recv(4096).decode("utf-8", "replace").strip()
