# ledclock

A voice-controlled LED matrix clock for a Raspberry Pi 4 driving a 128×64 P2
HUB75 panel.

The time is drawn large enough to read across a room. When you add alarms or
timers the clock shrinks to the top quarter of the panel and the entries list
below it, in the largest font that still fits. Entries disappear five minutes
after they fire.

```
   ┌───────────────────────────────┐   ┌───────────────────────────────┐
   │                               │   │            7:29 PM            │
   │            7:29               │   │ ▪▪▪▪ ▪▪▪▪ ▪▪▪▪ ▪▪·· ···· ···· │
   │ ▪▪▪▪ ▪▪▪▪ ▪▪▪▪ ▪▪·· ···· ···· │   │───────────────────────────────│
   │       Mon Aug 10          PM  │   │  Alarm1              8:59pm   │
   └───────────────────────────────┘   │  Timer1            00:03:00   │
              idle                     └───────────────────────────────┘
                                                 with entries
```

Under the time is a row of 60 dots — one per second — in six groups of ten,
draining as the minute runs out. It replaces a blinking colon: it says how far
through the minute you are, and nothing on screen moves.

## Install

On a fresh Raspberry Pi OS (64-bit, Bookworm or Trixie):

```bash
git clone https://github.com/YOUR_USER/ledclock.git
cd ledclock
./setup.sh
```

`setup.sh` installs the packages, clones and compiles
[hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix),
builds the Python binding into a venv, fetches the Vosk model, runs the
self-tests, and offers to install the systemd service. It prompts before
anything that touches boot config, and is safe to re-run.

Then reboot, for the two boot-level changes it makes (see
[Setup notes](#setup-notes) for why each is needed).

### Hardware

| | |
| --- | --- |
| Pi | Raspberry Pi 4 (2 GB is plenty) |
| Panel | 128×64 P2 HUB75, wired straight to the GPIO header |
| Mic | Any USB audio input (optional — voice can be disabled) |
| Buttons | Optional, any GPIO pins, mapped in `config.toml` |
| Buzzer | Optional piezo on a GPIO pin |

If you use an Adafruit RGB Matrix HAT or Bonnet instead of direct wiring, set
`hardware_mapping = "adafruit-hat"` in `config.toml`.

## Talking to it

Say the wake phrase, then a command. Both of these work — the command can ride
in the same breath as the wake phrase or follow it:

```
"clock clock clock, set a timer for three minutes"
"clock clock clock" ... "set an alarm for four p.m."
```

| What you say | What happens |
| --- | --- |
| `set an alarm for 4pm` | `Alarm1  4:00pm` |
| `set a timer for 3 minutes` | `Timer1  00:03:00` |
| `set an alarm for quarter past seven` | next 7:15 |
| `set a timer for one hour thirty minutes` | `01:30:00` |
| `cancel alarm one` / `delete timer two` | removes it |
| `cancel all timers` | clears every timer |
| `add ten minutes to timer one` | extends it |
| `pause timer one` / `resume timer one` | holds the countdown |
| `snooze` | pushes anything ringing back 9 minutes |
| `dismiss` | silences what's ringing |

Times with no am/pm ("set an alarm for five") resolve to whichever reading
comes soonest. An alarm for a time already past today rolls to tomorrow.

Recognition is **fully offline** — Vosk with a small English model. Nothing
leaves the Pi, and it keeps working when the WiFi drops into autoAP mode.

Accuracy comes from restricting the recogniser to the command vocabulary
(`voice.use_grammar`). Set it `false` to experiment with free-form phrasing,
at a noticeable cost in reliability.

## Driving it without speaking

The clock listens on a local Unix socket, which is the fastest way to check
display behaviour or work out whether a problem is recognition or logic:

```bash
python -m ledclock --send "set a timer for 3 minutes"
python -m ledclock --send "cancel all timers"
```

Other useful commands:

```bash
# How would this phrase be parsed?  (No hardware, no running service.)
python -m ledclock --say "add ten minutes to timer one"

# Parser + alarm/timer lifecycle self-tests.
python -m ledclock --check-intents

# Render sample frames to PNGs to judge layout and colours over SSH.
python -m ledclock --preview out/

# Contact sheets of every candidate clock face, antialiased and hard-edged.
python -m ledclock --compare-fonts out/

# Which capture devices exist, and which are we using?
python -m ledclock --list-audio
```

## Preferences

Everything is in [`config.toml`](config.toml) — panel wiring, colours, GPIO
pins, voice settings. Any key you delete falls back to the default in
`ledclock/config.py`, so a partial file is always valid.

```bash
sudo systemctl restart ledclock   # after editing
```

Relative paths in the config resolve against the config file's own directory,
so the whole folder can be moved without breaking.

### Buttons

Map GPIO pins (BCM numbering) to actions:

```toml
[buttons.pins]
5  = "dismiss"            # hold to dismiss everything
6  = "snooze"
16 = "cycle_brightness"
26 = "cancel_last"
```

Wire each button between its pin and ground (`pull_up = true` is the default).
Other actions: `dismiss_all`, `toggle_hour_format`, `add_minute`,
`pause_resume`.

**Free pins.** With `parallel = 1` the matrix uses GPIO 4, 7, 8, 9, 10, 11,
15, 17, 18, 22, 23, 24, 25 and 27. That leaves **5, 6, 12, 13, 14, 16, 19, 20,
21 and 26** for buttons and the buzzer. Raising `parallel` claims most of
those, so check before rewiring.

### Buzzer

Not yet wired, so it ships disabled. When the piezo goes on:

```toml
[buzzer]
enabled = true
pin = 13
type = "passive"     # bare piezo disc, driven with PWM
# type = "active"    # module with its own oscillator — just switched on/off
frequency_hz = 2730  # most piezo discs are loudest around here
```

Drive anything louder than a bare disc through a transistor rather than off
the GPIO pin directly.

## How it fits together

| File | Role |
| --- | --- |
| `entries.py` | `Alarm` / `Timer` and the thread-safe store; the whole lifecycle state machine |
| `intents.py` | Speech text → structured commands; also defines the recogniser vocabulary |
| `display.py` | Layout only — clock face sizing, font ladder for the entry rows |
| `textrender.py` | Outline-font rasterising with fixed digit cells, cached |
| `backends.py` | Where pixels go: the real panel, or a PNG for previews |
| `voice.py` | `arecord` → Vosk → wake-phrase state machine |
| `buttons.py` / `buzzer.py` | GPIO in and out, both degrading to no-ops if unavailable |
| `control.py` | The `--send` socket |
| `app.py` | Run loop; the only place display state is mutated |

Timing is all wall-clock based, so a restart or an NTP step never drifts a
countdown. Alarms and timers are saved to `state.json` and restored on start.

### The clock face

The time is drawn with a real outline font, rasterised by PIL and blitted to
the panel in one bulk `SetImage` call. Two settings, both in `[display]`:

```toml
clock_font         = ".../InterDisplay-Black.otf"   # the big full-screen clock
compact_clock_font = ".../DejaVuSans-Bold.ttf"      # the quarter-height clock
clock_antialias    = true
```

They're deliberately different faces. The full-screen clock is limited by
**width**, so a narrower face yields taller digits; the compact clock is capped
at a quarter of the panel **height** and has width to spare, so a wider face
buys legibility there. Set `clock_font = ""` to go back to the built-in
seven-segment digits, which need no font at all.

To see the options rendered at real panel resolution:

```bash
python -m ledclock --compare-fonts out/   # contact sheets of every candidate
```

Three details keep the face from twitching, all covered by
`--check-intents`:

- **Fixed digit cells.** Fonts draw `1` far narrower than `8`, so each digit is
  centred in a cell as wide as `8`. Without this the minutes shuffle sideways
  as they count.
- **The colon does not blink.** A blinking colon has to vacate its cell, and
  any width mismatch shows up as the minutes twitching sideways twice a
  second. The seconds bar below replaces it. (`clock.blink_colon = true`
  brings it back; the colon is then hidden at draw time while keeping its
  cell, so it still does not move.)
- **Sizing against `88:88`,** not the live time, so nothing resizes at 9:59.

The whole block is centred on the *current* time's width, so it does recentre
once when the hour goes from one digit to two. Set `clock.leading_zero = true`
for `07:54` if you'd rather it never move at all.

### The seconds bar

A bar under the digits, spanning exactly their width, showing how much of the
current minute is left. It replaces the blinking colon: it carries strictly
more information, and nothing on screen moves.

```toml
[clock]
seconds_bar = true

[display]
seconds_bar_height = 2          # forced to 1px in the compact clock
seconds_bar_gap = 2
seconds_bar_color = [255, 176, 0]
seconds_bar_track_color = [46, 32, 0]
seconds_bar_style = "deplete"   # 'deplete' empties as the minute runs out;
                                # 'fill' grows instead
```

The dim track stays lit so the remaining fraction is readable without needing
a reference point, and the bar updates with sub-second resolution so it creeps
rather than stepping. `--check-intents` asserts that across a whole minute the
bar rows are the *only* pixels that change.

Rendered faces are cached — the clock changes once a minute but the loop runs
at 20 fps, and rasterising every frame cost about a third of a core.

## Setup notes

What `setup.sh` does beyond installing packages, and why:

- **Onboard audio is disabled.** `dtparam=audio=off` in
  `/boot/firmware/config.txt` plus a `snd_bcm2835` blacklist in
  `/etc/modprobe.d/ledclock-blacklist-snd.conf`. The matrix library needs the
  same PWM hardware; leaving audio on makes it refuse to start. The 3.5 mm jack
  is gone as a result — the USB adapter still plays.
- **`isolcpus=3`** appended to `/boot/firmware/cmdline.txt` so the panel
  refresh thread gets a core to itself. Originals are backed up next to both
  files as `*.bak-ledclock`.
- **Mic gain** raised to 91% and saved with `alsactl store`; it was quiet
  enough at the default to hurt recognition.
- The capture device is named `plughw:CARD=Device,DEV=0` rather than
  `plughw:1,0` so it survives card renumbering. `plughw` (not `hw`) matters:
  the dongle only does 44.1/48 kHz and ALSA resamples to the 16 kHz Vosk wants.
- Python lives in `.venv` (PEP 668 means no system-wide pip on Trixie), built
  `--system-site-packages` so the apt-installed `lgpio` stays visible.

Originals of both boot files are backed up alongside them as `*.bak-ledclock`.

## Service

```bash
sudo systemctl status ledclock
sudo journalctl -u ledclock -f
```

It runs as root because the matrix library needs to map the GPIO peripheral,
and stays root so the button pins and state file remain writable.

## If something looks wrong

| Symptom | Try |
| --- | --- |
| Panel completely dark | `panel_type = "FM6126A"` — common on 128×64 P2 panels |
| Colours wrong | `led_rgb_sequence`, e.g. `"RBG"` |
| Sparkle, ghosting, flicker | raise `gpio_slowdown` (3–5 on a Pi 4) |
| Refuses to start, mentions sound | `snd_bcm2835` got loaded again |
| Bottom half wrong / rows doubled | `multiplexing` or `row_address_type` |
| Voice never triggers | `wake_min_repeats = 1`, and check `--list-audio` |
| Voice triggers by itself | `wake_min_repeats = 3` |

## License

MIT — see [LICENSE](LICENSE).

Bundles nothing, but depends on
[hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
(GPLv2, fetched by `setup.sh`) and the
[Vosk](https://alphacephei.com/vosk/) small English model (Apache 2.0).
