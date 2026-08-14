"""Entry point: ``python -m ledclock``."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import DEFAULT_PATH, Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledclock", description="LED matrix clock")
    parser.add_argument("-c", "--config", default=None, help=f"preferences file (default {DEFAULT_PATH})")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--check-intents", action="store_true",
                        help="parse sample phrases and exit; touches no hardware")
    parser.add_argument("--say", metavar="TEXT",
                        help="parse one phrase as if spoken, print the intent, and exit")
    parser.add_argument("--send", metavar="TEXT",
                        help="send a command to the running clock and exit")
    parser.add_argument("--list-audio", action="store_true",
                        help="show capture devices and the configured mic, then exit")
    parser.add_argument("--mic-level", metavar="SECONDS", nargs="?", const=15.0, type=float,
                        help="meter the mic and report peak/rms dBFS (default 15s)")
    parser.add_argument("--check-wake", metavar="PHRASE", nargs="?", const="",
                        help="check a wake phrase against the speech model, then exit")
    parser.add_argument("--buzzer-sweep", metavar="LO:HI:STEP", nargs="?", const="1000:6000:100",
                        help="find the passive buzzer's loudest frequency using the mic")
    parser.add_argument("--preview", metavar="DIR", nargs="?", const="preview",
                        help="render sample frames to PNGs instead of the panel")
    parser.add_argument("--compare-fonts", metavar="DIR", nargs="?", const="preview",
                        help="render every candidate clock face to contact sheets")
    parser.add_argument("--preview-scale", type=int, default=6,
                        help="pixels per LED in preview images (default 6)")
    args = parser.parse_args(argv)

    # --say and --send print one line and exit; keep their output clean.
    quiet = args.say is not None or args.send is not None
    logging.basicConfig(
        level=logging.ERROR if quiet else getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load(args.config)
    if not quiet:
        if cfg.path:
            logging.info("loaded preferences from %s", cfg.path)
        else:
            logging.warning("no preferences file found; using built-in defaults")

    if args.say is not None:
        from .intents import parse
        print(parse(args.say))
        return 0

    if args.send is not None:
        from .control import DEFAULT_SOCKET, send

        sock = cfg.get("control.socket", DEFAULT_SOCKET)
        try:
            print(send(args.send, sock))
        except OSError as exc:
            print(f"could not reach the clock on {sock}: {exc}")
            print("is the service running?  sudo systemctl status ledclock")
            return 1
        return 0

    if args.compare_fonts:
        from pathlib import Path

        from .preview import compare_compact, compare_fonts

        out = Path(args.compare_fonts).expanduser()
        print(f"rendering font comparison to {out.resolve()}")
        compare_fonts(cfg, out)
        compare_compact(cfg, out)
        return 0

    if args.preview:
        from pathlib import Path

        from .preview import render_previews

        out = Path(args.preview).expanduser()
        print(f"rendering preview frames to {out.resolve()}")
        render_previews(cfg, out, scale=args.preview_scale)
        from .preview import seconds_sweep
        seconds_sweep(cfg, out)
        return 0

    if args.check_intents:
        from .selftest import (
            run_buzzer_checks, run_intent_checks, run_layout_checks,
            run_lifecycle_checks, run_power_checks, run_wake_checks,
        )
        print("intent parsing")
        ok = run_intent_checks()
        print("\nalarm/timer/stopwatch lifecycle")
        ok = run_lifecycle_checks() and ok
        print("\nwake phrase matching")
        ok = run_wake_checks() and ok
        print("\nshutdown confirmation gate")
        ok = run_power_checks() and ok
        print("\nbuzzer tones and beat pattern")
        ok = run_buzzer_checks() and ok
        print("\nclock face layout stability")
        ok = run_layout_checks() and ok
        return 0 if ok else 1

    if args.list_audio:
        import subprocess
        subprocess.run(["arecord", "-l"], check=False)
        print(f"\nconfigured capture device: {cfg.get('voice.device')}")
        print(f"vosk model: {cfg.expand('voice.model_path')}")
        return 0

    if args.mic_level is not None:
        from .voice import meter
        return meter(cfg, args.mic_level)

    if args.check_wake is not None:
        from .voice import check_wake
        # Bare --check-wake tests whatever the config already says.
        return check_wake(cfg, args.check_wake or None)

    if args.buzzer_sweep is not None:
        from .buzzer import sweep
        try:
            lo, hi, step = (float(p) for p in args.buzzer_sweep.split(":"))
        except ValueError:
            print("--buzzer-sweep wants LO:HI:STEP in Hz, e.g. 1000:6000:100")
            return 2
        return sweep(cfg, lo, hi, step)

    from .app import ClockApp, install_signal_handlers

    try:
        app = ClockApp(cfg)
    except Exception:
        logging.exception("failed to start; is the matrix wired up and are you root?")
        return 1

    install_signal_handlers(app)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
