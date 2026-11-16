#!/bin/sh
# headforward installer for macOS and Linux.
#
#   curl -fsSL https://headforward.vercel.app/install.sh | sh
#
# Installs one file, headforward.py, and a `headforward` command. Needs only
# Python 3.9+ and curl or wget. No sudo, no pip. Run it again to upgrade.
# It changes no Claude Code settings: `headforward hooks install global` does that.
#
# Settings (environment variables):
#   HEADFORWARD_BIN_DIR            where the command goes (default ~/.local/bin)
#   HEADFORWARD_INSTALL_DIR        where headforward.py goes (default ~/.local/share/headforward)
#   HEADFORWARD_URL                where to download headforward.py from
#   HEADFORWARD_NO_MODIFY_PATH=1   don't add the command's folder to your shell profile
set -eu

URL="${HEADFORWARD_URL:-https://headforward.vercel.app/headforward.py}"
BIN_DIR="${HEADFORWARD_BIN_DIR:-$HOME/.local/bin}"
INSTALL_DIR="${HEADFORWARD_INSTALL_DIR:-$HOME/.local/share/headforward}"

say() { printf '%s\n' "$*"; }
fail() { printf 'headforward install: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" = 0 ] && [ -z "${HEADFORWARD_ALLOW_ROOT:-}" ]; then
  fail "don't run this with sudo; it installs into your home folder"
fi

# A Python 3.9+ that really runs (macOS's /usr/bin/python3 can be a stub that offers to install tools).
PYTHON=""
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1 &&
     "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    PYTHON="$(command -v "$p")"
    break
  fi
done
[ -n "$PYTHON" ] || fail "needs Python 3.9 or newer. macOS: brew install python (or xcode-select --install). Linux: install python3 with your package manager."

TMP="$(mktemp -d 2>/dev/null || mktemp -d -t headforward)"
trap 'rm -rf "$TMP"' EXIT INT TERM
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$TMP/headforward.py" || fail "could not download $URL"
elif command -v wget >/dev/null 2>&1; then
  wget -q "$URL" -O "$TMP/headforward.py" || fail "could not download $URL"
else
  fail "needs curl or wget to download headforward"
fi

# Check it is headforward and runs here before replacing anything.
VERSION="$("$PYTHON" "$TMP/headforward.py" --version 2>/dev/null)" || VERSION=""
case "$VERSION" in
  "headforward "*) ;;
  *) fail "the download from $URL is not a working headforward.py; nothing was changed" ;;
esac

PREVIOUS=""
if [ -f "$INSTALL_DIR/headforward.py" ]; then
  PREVIOUS="$("$PYTHON" "$INSTALL_DIR/headforward.py" --version 2>/dev/null || true)"
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR"
mv "$TMP/headforward.py" "$INSTALL_DIR/headforward.py.new"
mv "$INSTALL_DIR/headforward.py.new" "$INSTALL_DIR/headforward.py"   # atomic: hooks never see half a file
cat > "$BIN_DIR/headforward.new" <<EOF
#!/bin/sh
# Installed by https://headforward.vercel.app/install.sh
exec "$PYTHON" "$INSTALL_DIR/headforward.py" "\$@"
EOF
chmod 755 "$BIN_DIR/headforward.new"
mv "$BIN_DIR/headforward.new" "$BIN_DIR/headforward"

if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$VERSION" ]; then
  say "Upgraded $PREVIOUS -> $VERSION"
elif [ -n "$PREVIOUS" ]; then
  say "Reinstalled $VERSION"
else
  say "Installed $VERSION"
fi
say "  command: $BIN_DIR/headforward"
say "  script:  $INSTALL_DIR/headforward.py"

# Put the command on PATH for new shells.
case ":$PATH:" in
  *":$BIN_DIR:"*) ON_PATH=1 ;;
  *) ON_PATH="" ;;
esac
if [ -z "$ON_PATH" ]; then
  case "$BIN_DIR" in
    "$HOME"/*) SHOWN_BIN="\$HOME${BIN_DIR#"$HOME"}" ;;   # write $HOME/.local/bin, not the expanded path
    *) SHOWN_BIN="$BIN_DIR" ;;
  esac
  LINE="export PATH=\"$SHOWN_BIN:\$PATH\""
  case "${SHELL:-}" in
    */zsh) PROFILE="$HOME/.zshrc" ;;
    */bash) if [ "$(uname)" = Darwin ]; then PROFILE="$HOME/.bash_profile"; else PROFILE="$HOME/.bashrc"; fi ;;
    */fish) PROFILE="$HOME/.config/fish/config.fish"; LINE="fish_add_path \"$SHOWN_BIN\"" ;;
    *) PROFILE="$HOME/.profile" ;;
  esac
  if [ -n "${HEADFORWARD_NO_MODIFY_PATH:-}" ]; then
    say ""
    say "$BIN_DIR is not on your PATH. Add this line to $PROFILE:"
    say "  $LINE"
  elif ! grep -qsF "$LINE" "$PROFILE"; then
    mkdir -p "$(dirname "$PROFILE")"
    printf '\n# Added by the headforward installer\n%s\n' "$LINE" >> "$PROFILE"
    say ""
    say "Added $SHOWN_BIN to your PATH in $PROFILE. Open a new terminal, or run:"
    say "  $LINE"
  fi
fi

say ""
say "Next, turn it on for every Claude Code session (desktop app included):"
say "  headforward hooks install global"
say "Then check your setup:"
say "  headforward doctor"
