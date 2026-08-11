#!/usr/bin/env bash
#
# Set up ledclock on a fresh Raspberry Pi OS install.
#
#   ./setup.sh              # build everything, then prompt for system changes
#   ./setup.sh --yes        # accept every prompt (unattended)
#   ./setup.sh --build-only # skip the boot config and the systemd service
#
# Safe to re-run: every step checks whether it is already done.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
MATRIX_DIR="$DIR/rpi-rgb-led-matrix"
MODEL_DIR="$DIR/models"
MODEL_NAME="vosk-model-small-en-us-0.15"
MODEL_URL="https://alphacephei.com/vosk/models/$MODEL_NAME.zip"
BOOT_CFG=/boot/firmware/config.txt
BOOT_CMD=/boot/firmware/cmdline.txt

ASSUME_YES=0
BUILD_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        --build-only) BUILD_ONLY=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$*"; }

ask() {
    # ask "question" -> 0 for yes, 1 for no
    [ "$ASSUME_YES" = 1 ] && return 0
    local reply
    read -r -p "    $1 [y/N] " reply </dev/tty || return 1
    [[ "$reply" =~ ^[Yy] ]]
}

# Older Pi OS keeps the boot partition at /boot.
[ -f "$BOOT_CFG" ] || BOOT_CFG=/boot/config.txt
[ -f "$BOOT_CMD" ] || BOOT_CMD=/boot/cmdline.txt

# ---------------------------------------------------------------- packages
say "Installing packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git build-essential pkg-config cmake \
    python3-dev python3-venv python3-pil python3-numpy \
    python3-lgpio python3-gpiozero \
    alsa-utils \
    fonts-inter fonts-dejavu-core

# ------------------------------------------------------- matrix C++ library
if [ ! -d "$MATRIX_DIR" ]; then
    say "Cloning rpi-rgb-led-matrix"
    git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix.git "$MATRIX_DIR"
fi

if [ ! -f "$MATRIX_DIR/lib/librgbmatrix.a" ]; then
    say "Building the matrix library"
    make -C "$MATRIX_DIR" -j"$(nproc)"
fi

# ------------------------------------------------------------------- python
if [ ! -d "$VENV" ]; then
    say "Creating the virtualenv"
    # --system-site-packages so the apt-installed lgpio stays importable.
    python3 -m venv --system-site-packages "$VENV"
fi

say "Installing Python dependencies"
"$VENV/bin/pip" install -q --upgrade pip setuptools wheel cython

if ! "$VENV/bin/python" -c "import rgbmatrix" 2>/dev/null; then
    say "Building the rgbmatrix Python binding (a few minutes)"
    # Recent versions build from a pyproject.toml at the repo root.
    "$VENV/bin/pip" install "$MATRIX_DIR"
fi

"$VENV/bin/python" -c "import vosk" 2>/dev/null || "$VENV/bin/pip" install -q vosk

# -------------------------------------------------------------- voice model
if [ ! -d "$MODEL_DIR/$MODEL_NAME" ]; then
    say "Downloading the Vosk model (~40 MB)"
    mkdir -p "$MODEL_DIR"
    curl -sSL -o "$MODEL_DIR/$MODEL_NAME.zip" "$MODEL_URL"
    "$VENV/bin/python" -c "
import zipfile, sys
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
        "$MODEL_DIR/$MODEL_NAME.zip" "$MODEL_DIR"
    rm -f "$MODEL_DIR/$MODEL_NAME.zip"
fi

say "Running the self-tests"
"$VENV/bin/python" -m ledclock -c "$DIR/config.toml" --check-intents \
    | tail -n 4 || warn "self-tests reported failures"

if [ "$BUILD_ONLY" = 1 ]; then
    say "Build complete (--build-only, skipping system changes)"
    exit 0
fi

# ----------------------------------------------------------- system changes
# The matrix library drives the same PWM peripheral the onboard audio uses;
# with snd_bcm2835 loaded it refuses to start.
if grep -q '^dtparam=audio=on' "$BOOT_CFG" 2>/dev/null; then
    say "Onboard audio conflicts with the LED panel"
    warn "Disabling it loses the 3.5 mm jack. USB audio is unaffected."
    if ask "Disable onboard audio in $BOOT_CFG?"; then
        sudo cp "$BOOT_CFG" "$BOOT_CFG.bak-ledclock"
        sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$BOOT_CFG"
        echo 'blacklist snd_bcm2835' \
            | sudo tee /etc/modprobe.d/ledclock-blacklist-snd.conf >/dev/null
        warn "Applied. Takes effect on reboot."
    fi
fi

# Giving the refresh thread its own core visibly steadies the image.
if ! grep -q 'isolcpus=' "$BOOT_CMD" 2>/dev/null; then
    say "Reserving a CPU core for the panel refresh thread"
    if ask "Append isolcpus=3 to $BOOT_CMD?"; then
        sudo cp "$BOOT_CMD" "$BOOT_CMD.bak-ledclock"
        # cmdline.txt must stay a single line.
        sudo sed -i '1s/[[:space:]]*$//; 1s/$/ isolcpus=3/' "$BOOT_CMD"
        warn "Applied. Takes effect on reboot."
    fi
fi

# The default capture level is usually too low for reliable recognition.
if command -v amixer >/dev/null && amixer -c Device sget Mic >/dev/null 2>&1; then
    if ask "Raise the USB mic capture gain to 91% and save it?"; then
        amixer -c Device -q sset 'Mic' 91% cap || true
        sudo alsactl store || true
    fi
fi

# ------------------------------------------------------------------ service
say "Installing the systemd service"
if ask "Run ledclock at boot as a systemd service?"; then
    sed "s|__LEDCLOCK_DIR__|$DIR|g" "$DIR/ledclock.service" \
        | sudo tee /etc/systemd/system/ledclock.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now ledclock
    sleep 3
    sudo systemctl status ledclock --no-pager -n 10 || true
fi

say "Done"
echo "    Reboot to apply the boot changes, then check:"
echo "      systemctl status ledclock"
echo "      $VENV/bin/python -m ledclock --send 'set a timer for 3 minutes'"
