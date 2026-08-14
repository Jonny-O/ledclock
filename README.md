# ledclock

A voice-controlled LED matrix clock for a Raspberry Pi 4 driving a 128×64 P2
HUB75 panel.

The time is drawn large enough to read across a room. When you add alarms,
timers or stopwatches the clock shrinks to the top quarter of the panel and
the entries list below it, in the largest font that still fits. Timers and
alarms disappear five minutes after they fire.

```
   ┌───────────────────────────────┐   ┌───────────────────────────────┐
   │                               │   │            7:29 PM            │
   │            7:29               │   │ ▪▪▪▪ ▪▪▪▪ ▪▪▪▪ ▪▪·· ···· ···· │
   │ ▪▪▪▪ ▪▪▪▪ ▪▪▪▪ ▪▪·· ···· ···· │   │───────────────────────────────│
   │  Mon September 28         PM  │   │  Alarm1              8:59pm   │
   │ "set a timer for 3 minutes"   │   │  Timer1            00:03:00   │
   └───────────────────────────────┘   │  Watch1            00:12:41   │
                                       └───────────────────────────────┘
              idle                     with entries
```

Under the time is a row of 60 dots — one per second — in six groups of ten,
draining as the minute runs out. It replaces a blinking colon: it says how far
through the minute you are, and nothing on screen moves.

The bottom line belongs to the spoken-command echo and nothing else. It is
reserved whether or not a command is showing, so the date, the meridiem and
the entry rows never shift when one appears or expires.

## Install

On a fresh Raspberry Pi OS (64-bit, Bookworm or Trixie):

```bash
git clone https://github.com/Jonny-O/ledclock.git
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
"timekeeper, set a timer for three minutes"
"timekeeper" ... "set an alarm for four p.m."
```

| What you say | What happens |
| --- | --- |
| `set an alarm for 4pm` | `Alarm1  4:00pm` |
| `set a timer for 3 minutes` | `Timer1  00:03:00` |
| `start a stopwatch` | `Watch1  00:00:00`, counting up |
| `set an alarm for quarter past seven` | next 7:15 |
| `set a timer for one hour thirty minutes` | `01:30:00` |
| `cancel alarm one` / `delete timer two` | removes it |
| `cancel all timers` | clears every timer |
| `add ten minutes to timer one` | extends it |
| `pause timer one` / `resume timer one` | holds the count |
| `snooze` | pushes anything ringing back 9 minutes |
| `dismiss` | silences what's ringing |
| `shut down` / `reboot` | asks first — see [Power](#power) |

Times with no am/pm ("set an alarm for five") resolve to whichever reading
comes soonest. An alarm for a time already past today rolls to tomorrow.

### Counting up

A stopwatch is the timer run backwards, for when you don't know in advance how
long the thing will take:

```
"timekeeper, start a stopwatch"
"timekeeper, pause the stopwatch"
"timekeeper, cancel watch one"
```

It differs from a timer in three ways, all of which follow from having no
deadline: it never rings, it never disappears on its own — a timer lingers
five minutes after firing, a stopwatch stays until you cancel it — and it sorts
*below* the alarms and timers, which do have something to count towards.

The panel labels it `Watch1` rather than `Stopwatch1`, so the row is the same
six characters as `Alarm1` and `Timer1` and a stopwatch never pushes the entry
font down a size. Both names work when you speak to it.

`add ten minutes to watch one` moves the reading itself, since there is no
deadline to move instead. It will not go below zero.

### Power

`shut down`, `power off` and `reboot` do what they say, but never on one
utterance. The panel asks `shutdown? say yes` and waits ten seconds:

```
"timekeeper, shut down"     ->  panel: shutdown? say yes
"timekeeper, yes"           ->  goes down
```

Only `yes` gets through. Saying "shut down" a second time does not count, and
any other command — including `no` — stands it down and is then obeyed
normally. If the window passes, a stray "yes" does nothing.

That gate is the whole reason this is safe to leave enabled, because the
failure mode is a walk to the plug. `[power] confirm = false` removes it and
`enabled = false` removes the commands altogether.

Words that overlap with real commands keep their old meanings, and the
self-tests hold them there: `shut up` and `turn it off` still dismiss, `stop
timer one` still cancels a timer, and `restart` only means reboot when it is
the entire sentence.

Recognition is **fully offline** — Vosk with a small English model. Nothing
leaves the Pi, and it keeps working when the WiFi drops into autoAP mode.

Accuracy comes from restricting the recogniser to the command vocabulary
(`voice.use_grammar`). Set it `false` to experiment with free-form phrasing,
at a noticeable cost in reliability.

### Changing the wake phrase

`voice.wake_phrase` takes anything — one word or several:

```toml
wake_phrase = "timekeeper"        # wakes on one hit
wake_phrase = "hey timekeeper"    # both words, in order, together
wake_phrase = "clock clock clock" # one word repeated: see below
```

**Check a new phrase before you rely on it.** A word the speech model has no
pronunciation for is dropped from the grammar without complaint, and the clock
then simply never wakes — with nothing in the log to say why:

```bash
python -m ledclock --check-wake "timekeeper"   # or bare, to test the config
```

If a word comes back missing, splitting it usually rescues it: `time keeper`
is two words the model definitely knows. The clock now also logs an explicit
error at startup if its own wake phrase is unrecognisable.

**`wake_min_repeats` only applies to a phrase of one word repeated.** There it
means "say it three times, but two landing is enough", so a mic that clips the
first one doesn't cost you the command. A phrase of distinct words is always
matched in full and the setting is ignored.

The trade-off between the two forms is false wake-ups against effort. A single
distinctive word is the least trouble to say and, being rare, rarely turns up
by accident — but it only has one hit to be sure about. If yours starts
triggering on its own, write it twice (`"timekeeper timekeeper"`) and leave
`wake_min_repeats = 2`; that costs nothing but a syllable.

### Hearing you further away

Measure before changing anything. The meter reports peak and RMS level while
you talk, so you can walk to where you actually stand and see what the
recogniser is being given:

```bash
sudo systemctl stop ledclock          # it holds the mic
python -m ledclock --mic-level        # speak for 15s from across the room
sudo systemctl start ledclock
```

Speech that recognises reliably peaks around **-12 dBFS**. What matters more
than the absolute number is the gap between your voice and the room: below
about 20 dB of separation, no amount of gain will help, because gain raises
the room along with you.

In rough order of how much they buy you:

1. **Move the mic, not the gain.** Doubling the distance costs 6 dB. Getting
   the capsule off the desk — away from the surface that reflects everything —
   and pointed at where people stand routinely beats every setting below.
2. **Raise the ALSA capture control**, which has a real preamp behind it:
   `amixer -c Device sset Mic 100% cap`, then `sudo alsactl store`. This is
   already at 91% here. Check `Auto Gain Control` is on with `amixer -c Device`.
3. **`voice.gain`** multiplies the samples in software. It is the last resort,
   not the first: it cannot add information the capsule never captured, and it
   amplifies the noise floor equally. Useful when the capture control is
   maxed and you are still 6-10 dB short. `--mic-level` suggests a value.
4. **A better microphone.** This is usually the real answer. A generic C-Media
   dongle with an electret capsule is a near-field device; it was never going
   to do room pickup. A four-mic array with beamforming is a different
   category of thing, not a louder version of the same one.

   ⚠️ **Get a USB one.** The ReSpeaker 2-Mic and 4-Mic *HATs* are I²S and use
   GPIO 18, 19, 20 and 21 — GPIO 18 is one of the pins the HUB75 panel drives.
   They physically cannot coexist with this clock. USB arrays (ReSpeaker USB
   Mic Array, or a PS3 Eye, which is class-compliant and cheap) have no such
   conflict.

5. **A bigger language model.** `vosk-model-en-us-0.22-lgraph` is ~128 MB
   against the small model's 40 MB and handles reverberant far-field speech
   noticeably better. It still fits a 2 GB Pi alongside the panel and still
   supports the grammar. Drop it in `models/` and repoint `voice.model_path`.

Two things that are *not* worth doing: lowering `wake_min_repeats` to 1, which
trades range for false wake-ups; and raising `voice.gain` past about 4, which
is where the noise floor starts producing phantom commands.

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

# How loud is the mic actually hearing you?  (Stop the service first.)
python -m ledclock --mic-level

# Can the speech model actually hear this wake phrase?
python -m ledclock --check-wake "timekeeper"

# Which frequency is the piezo loudest at?  (Stop the service first.)
python -m ledclock --buzzer-sweep
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

A passive piezo on GPIO 12, driven with software PWM:

```toml
[buzzer]
enabled = true
pin = 12
type = "passive"          # driven with PWM; 'active' = module with its own oscillator
frequency_hz = [4400, 3300]
duty_cycle = 50
beat_on = 0.12
beat_off = 0.0
active_high = false       # the module's driver is a PNP, so it conducts on a LOW
```

### Choosing the alarm sound

`frequency_hz` takes one number for a plain beep, or a list to alternate
between, one tone per beat:

| `frequency_hz` | `beat_on` | `beat_off` | Result |
| --- | --- | --- | --- |
| `4400` | 0.15 | 0.35 | single beep, repeating |
| `[4400, 3300]` | 0.12 | **0** | continuous two-tone warble |
| `[4400, 3300]` | 0.15 | 0.35 | urgent alternating beeps |
| `[4400, 3300]` | 0.4 | 0.4 | slow two-tone doorbell |

`beat_off = 0` is what makes a warble rather than two beeps — the tones run
straight into each other with no silence, and `beat_on` then sets the warble
rate.

**Both tones have to sit near resonance.** This is the constraint that catches
people out: a piezo is a mechanical resonator, not a speaker, so a low tone is
not quieter — it is *absent*. Measured on this build, 500 Hz came in at +1.2 dB
above an empty room, which is nothing. Anything like 80/160 Hz will be silent no
matter how the code drives it. If you want an alarm that feels low and urgent,
get it from a **slow beat rate** with both tones up at 3300–4700 Hz, not from a
low frequency.

**If it is barely audible, the frequency is almost certainly wrong.** A passive
element is a mechanical resonator, and off its resonance it hardly moves air at
all. On this build the difference between 2730 Hz and 4400 Hz measured **29 dB
— roughly 29× the sound pressure** — from the same pin, the same wiring and the
same 3.3 V. Nothing about the circuit changed.

That peak belongs to the specific part, so measure yours rather than copying a
number:

```bash
python -m ledclock --buzzer-sweep            # 1000-6000 Hz in 100 Hz steps
python -m ledclock --buzzer-sweep 3000:5000:50
```

It steps the buzzer through the range and uses **the microphone as the
instrument**, reporting each frequency's level against the same band recorded in
silence so the room cancels out. It prints the config line to paste.

```
   freq     level
----------------------------------------------
   4000     +34.8  #################
   4200     +40.5  ####################
   4400     +41.7  ####################
   4600     +39.7  ###################

loudest at 4400 Hz (+41.7 dB)
```

Two things it is worth *not* blaming:

* **Software PWM.** lgpio's `tx_pwm` is bit-banged from userspace, which sounds
  like it ought to be the culprit. Measured against a tight busy-wait toggle
  loop it was within 1 dB at every frequency tested — the waveform is fine.
* **Duty cycle.** 50% is already the loudest; measured 8 dB above 10%, 4 dB
  above 75%.

If it is still too quiet at resonance, the remaining lever is voltage swing —
the element only ever sees 3.3 V. Driving it through a transistor from 5 V, or
push-pull between two GPIOs instead of pin-to-ground, buys roughly 4 dB and
6 dB respectively. Anything louder than a bare disc should go through a
transistor regardless, rather than off the pin directly.

**Do not move it to hardware PWM.** GPIO 12 and GPIO 18 are the same PWM
channel (PWM0_0), and the matrix library holds GPIO 18 in ALT5 for its own
timing — you can see it with `pinctrl get 18`. Claiming the channel for a tone
would fight the panel for it.

## How it fits together

| File | Role |
| --- | --- |
| `entries.py` | `Alarm` / `Timer` / `Stopwatch` and the thread-safe store; the whole lifecycle state machine |
| `intents.py` | Speech text → structured commands; also defines the recogniser vocabulary |
| `display.py` | Layout only — clock face sizing, font ladder for the entry rows |
| `textrender.py` | Outline-font rasterising with fixed digit cells, cached |
| `backends.py` | Where pixels go: the real panel, or a PNG for previews |
| `voice.py` | `arecord` → Vosk → wake-phrase state machine |
| `buttons.py` / `buzzer.py` | GPIO in and out, both degrading to no-ops if unavailable |
| `control.py` | The `--send` socket |
| `app.py` | Run loop; the only place display state is mutated |

Timing is all wall-clock based, so a restart or an NTP step never drifts a
count. Everything on screen is saved to `state.json` and restored on start,
including a stopwatch mid-count.

### The clock face

The time is drawn with a real outline font, rasterised by PIL and blitted to
the panel in one bulk `SetImage` call. Two settings, both in `[display]`:

```toml
clock_font         = ".../InterDisplay-Black.otf"   # the big full-screen clock
compact_clock_font = ".../DejaVuSans-Bold.ttf"      # the quarter-height clock
clock_antialias    = true
date_font          = "6x10.bdf"   # the date row, e.g. "Mon September 28"
small_font         = "4x6.bdf"    # the reserved bottom line
```

The date is a size up from the bottom line and spells the month out, which
costs nothing: the time is limited by the panel's **width**, so giving the
date row four more pixels of height leaves the digits at exactly 36px either
way. Set `date_font = "4x6.bdf"` to go back to the smaller row.

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
| Voice never triggers | `--check-wake`, then `--list-audio` and `--mic-level` |
| Only works up close | see [Hearing you further away](#hearing-you-further-away) |
| Voice triggers by itself | `wake_min_repeats = 3`, and lower `voice.gain` |
| It shut down on its own | `[power] confirm` got turned off; put it back |
| Buzzer barely audible | wrong frequency, not wiring — `--buzzer-sweep` |

## License

MIT — see [LICENSE](LICENSE).

Bundles nothing, but depends on
[hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
(GPLv2, fetched by `setup.sh`) and the
[Vosk](https://alphacephei.com/vosk/) small English model (Apache 2.0).
