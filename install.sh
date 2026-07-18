#!/usr/bin/env bash
#
# SpookiUI installer — macOS & Linux
#
# Checks prerequisites (Python 3.8+, the ghostty binary) and installs a
# `spookiui` command on your PATH by symlinking spookiui.py into a bin dir.
#
# Usage:
#   ./install.sh                 # install for current user (~/.local/bin)
#   PREFIX=/usr/local ./install.sh   # system-wide (may need sudo)
#
set -euo pipefail

# Resolve the directory this script lives in (the repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/spookiui.py"

# --------------------------------------------------------------------------- #
#  Pretty output helpers
# --------------------------------------------------------------------------- #
if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"
    YELLOW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"
    DIM="$(printf '\033[2m')"; RESET="$(printf '\033[0m')"
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
fi
info()  { printf '%s\n' "${DIM}·${RESET} $*"; }
ok()    { printf '%s\n' "${GREEN}✓${RESET} $*"; }
warn()  { printf '%s\n' "${YELLOW}!${RESET} $*"; }
err()   { printf '%s\n' "${RED}✗${RESET} $*" >&2; }
die()   { err "$*"; exit 1; }

printf '%s\n' "${BOLD}Installing SpookiUI${RESET}"

# --------------------------------------------------------------------------- #
#  1. Locate the source script
# --------------------------------------------------------------------------- #
[ -f "$SRC" ] || die "spookiui.py not found next to this script ($SRC)."

# --------------------------------------------------------------------------- #
#  2. Check Python >= 3.8
# --------------------------------------------------------------------------- #
PYTHON=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PYTHON="$(command -v "$cand")"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    die "Python 3.8+ is required but was not found. Install it and re-run ./install.sh"
fi
ok "Python: $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# curses ships with CPython on macOS/Linux, but confirm it imports.
"$PYTHON" -c 'import curses' 2>/dev/null \
    && ok "curses module available" \
    || warn "the 'curses' module failed to import — the TUI may not run (the CLI still will)."

# Compile-check the script so a broken checkout fails loudly here, not later.
"$PYTHON" -m py_compile "$SRC" && ok "spookiui.py compiles cleanly"

# Read the version straight from the script (authoritative single source).
# \x27 is a single quote — used so this whole program can stay single-quoted.
VERSION="$("$PYTHON" -c 'import re,sys; m=re.search(r"^__version__\s*=\s*[\"\x27]([^\"\x27]+)", open(sys.argv[1]).read(), re.M); print(m.group(1) if m else "")' "$SRC" 2>/dev/null || true)"
[ -n "$VERSION" ] && ok "SpookiUI version: $VERSION"

# --------------------------------------------------------------------------- #
#  3. Check for the ghostty binary (runtime dependency)
# --------------------------------------------------------------------------- #
if command -v ghostty >/dev/null 2>&1; then
    ok "ghostty: $(command -v ghostty)"
elif [ -x "/Applications/Ghostty.app/Contents/MacOS/ghostty" ]; then
    ok "ghostty: /Applications/Ghostty.app (macOS app bundle)"
else
    warn "the 'ghostty' binary was not found on PATH."
    info "SpookiUI needs Ghostty at runtime. Install it from https://ghostty.org"
    case "$(uname -s)" in
        Darwin) info "  macOS:  brew install --cask ghostty" ;;
        Linux)  info "  Linux:  see https://ghostty.org/docs/install for your distro" ;;
    esac
fi

# --------------------------------------------------------------------------- #
#  4. Pick a bin dir on PATH and link `spookiui` into it
# --------------------------------------------------------------------------- #
chmod +x "$SRC"

PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
mkdir -p "$BIN_DIR"
TARGET="$BIN_DIR/spookiui"

if ln -sf "$SRC" "$TARGET" 2>/dev/null; then
    ok "Linked ${BOLD}spookiui${RESET} → $TARGET"
else
    die "Could not write to $BIN_DIR. Retry with sudo, or set PREFIX to a writable dir:
    PREFIX=\"\$HOME/.local\" ./install.sh"
fi

# --------------------------------------------------------------------------- #
#  5. Confirm the bin dir is on PATH
# --------------------------------------------------------------------------- #
case ":$PATH:" in
    *":$BIN_DIR:"*)
        ok "$BIN_DIR is on your PATH"
        printf '\n%s\n' "${GREEN}${BOLD}Done${RESET}${GREEN} (SpookiUI ${VERSION:-?}).${RESET} Run ${BOLD}spookiui${RESET} to launch, or ${BOLD}spookiui --help${RESET} for the CLI."
        ;;
    *)
        warn "$BIN_DIR is not on your PATH yet."
        info "Add this line to your shell profile (~/.zshrc, ~/.bashrc, …):"
        printf '\n    %s\n\n' "${BOLD}export PATH=\"$BIN_DIR:\$PATH\"${RESET}"
        info "Then reopen your terminal, or run: export PATH=\"$BIN_DIR:\$PATH\""
        info "Until then you can run it directly: ${BOLD}$TARGET${RESET}"
        ;;
esac
