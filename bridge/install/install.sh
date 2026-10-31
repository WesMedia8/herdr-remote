#!/bin/sh
set -eu
REPO_URL=${REPO_URL:-https://github.com/WesMedia8/herdr-remote}
OS=$(uname -s)
case "$OS" in Darwin|Linux) ;; *) echo 'Use install.ps1 on Windows.'; exit 1 ;; esac
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3,10))'; then
    if [ "$OS" = Darwin ]; then echo 'Install Python 3.10+: brew install python'; else echo 'Install Python 3.10+: sudo apt install python3 python3-venv pipx'; fi
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    if [ "$OS" = Darwin ]; then echo 'Install Git: xcode-select --install'; else echo 'Install Git: sudo apt install git'; fi
    exit 1
fi
if ! command -v pipx >/dev/null 2>&1; then
    if [ "$OS" = Darwin ]; then
        if ! command -v brew >/dev/null 2>&1; then echo 'Install Homebrew from https://brew.sh then run: brew install pipx'; exit 1; fi
        brew install pipx
    else
        echo 'Recommended: sudo apt install python3 python3-venv pipx'
        echo 'Trying a user-only pipx install; if blocked by your distro, run the apt command above and rerun.'
        python3 -m pip install --user pipx
    fi
fi
# Brew/apt pipx may use a different Python from python3.
pipx_run() {
    if command -v pipx >/dev/null 2>&1; then pipx "$@"; else python3 -m pipx "$@"; fi
}
pipx_run ensurepath
PIPX_BIN=$(pipx_run environment --value PIPX_BIN_DIR)
PATH="$PIPX_BIN:$PATH"
export PATH
pipx_run install --force "git+$REPO_URL#subdirectory=bridge"
if ! herdr-remote-bridge doctor; then
    echo 'Install/connect Tailscale: https://tailscale.com/download and sign in.'
    if [ "$OS" = Linux ]; then echo 'Connect with: sudo tailscale up'; else echo 'Open the Tailscale app and sign in.'; fi
    echo 'Review doctor output for missing herdr tools. Press Enter when ready to pair.'
    if [ -r /dev/tty ]; then read -r answer </dev/tty; fi
fi
# Start the bridge before pairing, so the phone can connect the moment it scans the code.
printf 'Start the bridge now and at every login? [Y/n] '
answer=y
if [ -r /dev/tty ]; then read -r answer </dev/tty; fi
case "$answer" in
    n|N|no|NO) echo 'OK. Run "herdr-remote-bridge serve" in another window before scanning the code.' ;;
    *) herdr-remote-bridge service install ;;
esac
herdr-remote-bridge pair
