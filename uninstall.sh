#!/usr/bin/env bash
#
# SpookiUI uninstaller — macOS & Linux.
#
# Reverses install.sh: removes the `spookiui` command it symlinked onto your
# PATH. It never touches your Ghostty config, and it leaves the repo's own
# spookiui.py in place (that's the source, not something install.sh created —
# delete the repo folder yourself if you want it gone).
#
# Usage:
#   ./uninstall.sh                     # remove the `spookiui` command
#   PREFIX=/usr/local ./uninstall.sh   # if you installed with that PREFIX
#   ./uninstall.sh --purge             # also delete SpookiUI's cache + saved profiles
#
set -euo pipefail

# --------------------------------------------------------------------------- #
#  Pretty output helpers (same as install.sh)
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

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge|--data) PURGE=1 ;;
        -h|--help) sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done

printf '%s\n' "${BOLD}Uninstalling SpookiUI${RESET}"

# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

# Follow a symlink chain to its final target (bash 3.2 compatible — no readlink -f).
resolve() {
    local p="$1" t
    while [ -L "$p" ]; do
        t="$(readlink "$p")"
        case "$t" in
            /*) p="$t" ;;
            *)  p="$(cd -P "$(dirname "$p")" && pwd)/$t" ;;
        esac
    done
    printf '%s\n' "$p"
}

# Is a path inside a Homebrew Cellar / prefix? Those are `brew uninstall`'s job.
is_homebrew() {
    case "$1" in */Cellar/*) return 0 ;; esac
    if command -v brew >/dev/null 2>&1; then
        local bp; bp="$(brew --prefix 2>/dev/null || true)"
        if [ -n "$bp" ]; then
            case "$1" in "$bp"/*) return 0 ;; esac
        fi
    fi
    return 1
}

removed=0
skipped=0
seen="|"

# Inspect one candidate `spookiui` path and remove it only if it's the symlink
# install.sh created (a symlink whose target is a spookiui.py).
check_one() {
    local cand="$1" real
    case "$seen" in *"|$cand|"*) return 0 ;; esac
    seen="$seen$cand|"
    if [ ! -L "$cand" ] && [ ! -e "$cand" ]; then
        return 0
    fi
    if [ -L "$cand" ]; then
        real="$(resolve "$cand")"
        if is_homebrew "$real"; then
            warn "$cand is a Homebrew install — run 'brew uninstall spookiui' to remove it"
            skipped=$((skipped + 1))
            return 0
        fi
        case "$(basename "$real")" in
            spookiui.py)
                rm -f "$cand"
                ok "removed $cand"
                removed=$((removed + 1))
                ;;
            *)
                warn "$cand points to $real (not a SpookiUI install) — left alone"
                skipped=$((skipped + 1))
                ;;
        esac
    else
        warn "$cand is a real file, not the symlink install.sh creates — left alone"
        skipped=$((skipped + 1))
    fi
}

# --------------------------------------------------------------------------- #
#  1. Remove the `spookiui` command
# --------------------------------------------------------------------------- #
PREFIX="${PREFIX:-$HOME/.local}"

# Remove exactly what install.sh created: the symlink at $PREFIX/bin.
check_one "$PREFIX/bin/spookiui"

if [ "$removed" -eq 0 ] && [ "$skipped" -eq 0 ]; then
    warn "no SpookiUI install found at $PREFIX/bin/spookiui."
    info "Installed with a custom PREFIX? Re-run: ${BOLD}PREFIX=/your/prefix ./uninstall.sh${RESET}"
fi

# Point out (but never remove) any other `spookiui` commands elsewhere on PATH,
# so an install in a different PREFIX is never silently deleted.
report_one() {
    local cand="$1" real
    case "$seen" in *"|$cand|"*) return 0 ;; esac
    seen="$seen$cand|"
    [ -L "$cand" ] || return 0
    real="$(resolve "$cand")"
    if is_homebrew "$real"; then
        info "also found a Homebrew install at $cand ('brew uninstall spookiui' to remove)"
    elif [ "$(basename "$real")" = "spookiui.py" ]; then
        info "also found an install at $cand (PREFIX=$(dirname "$(dirname "$cand")") ./uninstall.sh to remove)"
    fi
    return 0
}
for d in "$HOME/.local/bin" "/usr/local/bin" "/opt/homebrew/bin" "$HOME/bin"; do
    report_one "$d/spookiui"
done
IFS=':' read -ra PATH_DIRS <<< "$PATH"
for d in "${PATH_DIRS[@]}"; do
    if [ -n "$d" ]; then report_one "$d/spookiui"; fi
done

# --------------------------------------------------------------------------- #
#  2. Optionally remove SpookiUI's own data (opt-in)
# --------------------------------------------------------------------------- #
if [ "$PURGE" -eq 1 ]; then
    CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/spookiui"
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/spookiui"
    for dir in "$CACHE_DIR" "$DATA_DIR"; do
        if [ -d "$dir" ]; then
            rm -rf "$dir"
            ok "removed $dir"
        fi
    done
    info "Ghostty config backups (…/config.spookiui.YYYYMMDD.bak) are left in place."
else
    info "SpookiUI's cache and saved profiles were kept — re-run with ${BOLD}--purge${RESET} to delete them."
fi

# --------------------------------------------------------------------------- #
#  3. Summary
# --------------------------------------------------------------------------- #
if [ "$removed" -gt 0 ]; then
    printf '\n%s\n' "${GREEN}${BOLD}Done.${RESET} Removed the ${BOLD}spookiui${RESET} command."
fi
info "The repo's spookiui.py is untouched; delete the repo folder to remove it."
