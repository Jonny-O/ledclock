# ledclock

A voice-controlled clock for a Raspberry Pi that displays the time on a HUB75 LED panel.

```
   ┌───────────────────────────────┐   ┌───────────────────────────────┐
   |        __    __   __          |   │            7:29 PM            │
   |         / o  __| (__\         |   │───────────────────────────────│
   |        /  o |__   __/         |   │  Alarm1              8:59pm   │
   │ ▪▪▪▪ ▪▪▪▪ ▪▪▪▪ ▪▪·· ···· ···· │   │  Timer1            00:03:00   │
   │  Mon September 28         PM  │   │  Watch1            00:12:41   │
   │ "set a timer for 3 minutes"   │   |                               |
   └───────────────────────────────┘   └───────────────────────────────┘
              idle                     with entries
```

When idle, the time is drawn large enough to read across a room. Under the time is a row of 60 dots — one per second — in six groups of ten,
draining as the minute runs out.

When you add alarms, timers or stopwatches the clock shrinks to the top 255 of the panel and
the entries list below it, in the largest font that still fits. Timers and alarms disappear five minutes after they fire.

The bottom-most line in either display mode shows the feedback of your voice commands.

## Hardware

| | |
| --- | --- |
| Pi | Ideal: Raspberry Pi 4 (2 GB is plenty)|
| | Minimal: Raspberry Pi 3A (see below) |
| Panel | 128×64 P2 HUB75, wired straight to the GPIO header |
| Mic | Any USB audio input (optional — voice can be disabled) |
| Buttons | Optional, any GPIO pins, mapped in `config.toml` |
| Buzzer | Optional piezo on a GPIO pin |

If you use an Adafruit RGB Matrix HAT or Bonnet instead of direct wiring, set
`hardware_mapping = "adafruit-hat"` in `config.toml`.


### Install

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


### Running on a Pi 3

While it works on as little as Pi 3A+, the lower-horsepower hardware means you need to tighten up the config. See the "config-pi3.toml" file for details. Expect the pause between speaking a command and seeing action taken to be longer on a Pi 3 versus a Pi 4.

The biggest impact with lower-end hardware is the available memory, as the voice recognition consumes most of what is available on a 512MB model. Here is what the memory breakdown looks like:

```
baseline python         8.2 MB
+ vosk import          38.6 MB
+ model loaded        155.7 MB
+ recognizer running  162.1 MB
+ numpy/PIL           172.1 MB
```


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
"timekeeper, add ten minutes to watch one"
```

The answer needs no wake phrase. Asking the question opens the mic for the
same ten seconds the answer is accepted for, and chirps to say so — the wake
run is there to keep the room's conversation out of the recogniser, and it has
nothing left to guard once the panel is asking you something. Saying
"timekeeper, yes" still works; it is just no longer required.

Only `yes` gets through. Saying "shut down" a second time does not count, and
any other command — including `no` — stands it down and is then obeyed
normally. The mic closes on the first thing it hears either way, so the window
is one answer long, not ten seconds of open microphone. If the window passes
without a word, a stray "yes" afterwards does nothing.

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

   ⚠️ ** Get a USB model ** The ReSpeaker 2-Mic and 4-Mic *HATs* are I²S and use
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

## If something looks wrong

| Symptom | Try |
| --- | --- |
| Panel completely dark | `panel_type = "FM6126A"` — common on 128×64 P2 panels |
| Colours wrong | `led_rgb_sequence`, e.g. `"RBG"` |
| Sparkle, ghosting, flicker | `gpio_slowdown` — 3–5 on a Pi 4, but 1–2 on a Pi 3 |
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
