#!/usr/bin/env python3
"""
SpookiUI — a live configurator for the Ghostty terminal.

Run with no arguments to launch the interactive TUI:

    ./spookiui.py

Because Ghostty cannot auto-reload its config on file change, this tool writes
your config file and then triggers Ghostty to reload it so changes appear
*live*: on macOS by clicking the "Reload Configuration" menu item (via
AppleScript), and on Linux by sending the running Ghostty process the SIGUSR2
signal it reloads on. When you run this inside a Ghostty window, you watch the
very terminal you're in repaint as you edit.

Every option Ghostty exposes is discovered dynamically from the binary itself
(`ghostty +show-config --default --docs`), so this stays in sync with whatever
Ghostty version you have installed — nothing is hard-coded.

There is also a scriptable, non-interactive CLI: `get`, `set`, `list`, `doc`,
`reset`, `version`, `reload`, `validate`, `themes`, `fonts`, `path`. Run
`./spookiui.py --help`.

On startup SpookiUI checks GitHub for a newer release (cached for a day; set
SPOOKIUI_NO_UPDATE_CHECK=1 to disable) and shows a badge if one is available.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

__version__ = "1.2.0"
GITHUB_REPO = "mattj85/SpookiUI"

# --------------------------------------------------------------------------- #
#  Ghostty binary discovery
# --------------------------------------------------------------------------- #

def find_ghostty() -> str | None:
    """Return a path to the `ghostty` executable, or None if not found."""
    exe = shutil.which("ghostty")
    if exe:
        return exe
    for cand in (
        "/Applications/Ghostty.app/Contents/MacOS/ghostty",
        os.path.expanduser("~/Applications/Ghostty.app/Contents/MacOS/ghostty"),
        "/usr/bin/ghostty",
        "/usr/local/bin/ghostty",
    ):
        if os.path.exists(cand):
            return cand
    return None


GHOSTTY = find_ghostty()
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
# Whether we can trigger a live reload on this platform (macOS menu click /
# Linux SIGUSR2). Other platforms write+validate but can't auto-reload.
CAN_RELOAD = IS_MACOS or IS_LINUX
INSIDE_GHOSTTY = os.environ.get("TERM_PROGRAM") == "ghostty"
# The platform we're running on, for hiding OS-exclusive options. On anything
# other than macOS/Linux we don't hide anything (we can't say what's relevant).
CURRENT_PLATFORM = "macos" if IS_MACOS else ("linux" if IS_LINUX else None)


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )


# --------------------------------------------------------------------------- #
#  Update check: is a newer release available on GitHub?
# --------------------------------------------------------------------------- #
#
# We compare our embedded __version__ against the repo's latest GitHub Release.
# The check is best-effort and must never get in the way: any failure (offline,
# rate-limited, no releases yet) is swallowed silently. Results are cached for a
# day so we don't hammer GitHub's unauthenticated 60-req/hour limit, and the
# whole thing can be turned off with SPOOKIUI_NO_UPDATE_CHECK=1.

UPDATE_CHECK_TTL = 24 * 60 * 60          # seconds between network checks
_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"


def _update_check_disabled() -> bool:
    return os.environ.get("SPOOKIUI_NO_UPDATE_CHECK", "").strip() not in ("", "0")


def _cache_file() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "spookiui", "update-check.json")


def _parse_version(v: str) -> tuple[int, ...]:
    """Turn 'v1.10.2', '1.2.0-beta' etc. into a comparable numeric tuple."""
    v = v.strip().lstrip("vV")
    nums = re.findall(r"\d+", v.split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums) if nums else (0,)


def is_newer(latest: str, current: str = __version__) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _read_cache() -> dict | None:
    try:
        with open(_cache_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_cache(data: dict) -> None:
    try:
        path = _cache_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _fetch_latest_release(timeout: float = 4.0) -> dict | None:
    """Hit the GitHub API for the latest release. Returns None on any problem."""
    req = urllib.request.Request(
        _RELEASES_API,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"SpookiUI/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None
    tag = payload.get("tag_name") or payload.get("name")
    if not tag:
        return None
    return {
        "latest": tag,
        "url": payload.get("html_url") or _RELEASES_URL,
        "notes": (payload.get("body") or "").strip(),
    }


def check_for_update(force: bool = False, now: float | None = None) -> dict | None:
    """Return update info, cached to at most one network call per day.

    Result: {latest, url, notes, current, outdated}. Returns None only when we
    have no information at all (never checked and the network call failed). A
    successful check that finds you're up to date returns a dict with
    outdated=False. `force` bypasses both the opt-out and the cache TTL.
    """
    if _update_check_disabled() and not force:
        return None
    now = time.time() if now is None else now

    cache = _read_cache()
    fresh = bool(cache) and (now - cache.get("checked_at", 0) < UPDATE_CHECK_TTL)
    if cache and fresh and not force:
        latest = cache.get("latest")
    else:
        got = _fetch_latest_release()
        if got is None:
            if not cache:
                return None                       # nothing to report
            latest = cache.get("latest")          # fall back to stale cache
        else:
            latest = got["latest"]
            cache = {"checked_at": now, **got}
            _write_cache(cache)

    if not latest:
        return None
    return {
        "latest": latest,
        "url": (cache or {}).get("url", _RELEASES_URL),
        "notes": (cache or {}).get("notes", ""),
        "current": __version__,
        "outdated": is_newer(latest),
    }


# --------------------------------------------------------------------------- #
#  Config-file location
# --------------------------------------------------------------------------- #

def config_path() -> str:
    """Best-effort path to the active Ghostty config file (XDG first)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    candidates = []
    if xdg:
        candidates.append(os.path.join(xdg, "ghostty", "config"))
    candidates.append(os.path.expanduser("~/.config/ghostty/config"))
    if IS_MACOS:
        candidates.append(os.path.expanduser(
            "~/Library/Application Support/com.mitchellh.ghostty/config"))
    for c in candidates:
        if os.path.exists(c):
            return c
    # Default target if nothing exists yet.
    return candidates[0]


# --------------------------------------------------------------------------- #
#  Schema: every option Ghostty knows about
# --------------------------------------------------------------------------- #

# Keys that may appear more than once (a list of values) even when they only
# show up once in the default dump because their default is empty.
FORCE_LIST = {
    "font-family", "font-family-bold", "font-family-italic",
    "font-family-bold-italic", "font-feature", "font-variation",
    "font-variation-bold", "font-variation-italic", "font-variation-bold-italic",
    "font-codepoint-map", "keybind", "palette", "config-file",
    "config-default-files", "env", "clipboard-codepoint-map", "key-remap",
    "command-palette-entry", "link",
}

COLOR_HINTS = (
    "background", "foreground", "cursor-color", "cursor-text", "bold-color",
    "split-divider-color", "unfocused-split-fill", "window-padding-color",
    "window-titlebar-background", "window-titlebar-foreground",
)

# Numeric options that have a well-defined range get a visual slider in the TUI
# instead of a plain type-a-number prompt: (min, max, step). Opacity-style
# options (docs describe "fully opaque"/"fully transparent") are detected
# automatically in slider_range(); this map is for the rest.
SLIDER_RANGES = {
    "minimum-contrast": (1.0, 21.0, 0.5),      # WCAG contrast ratio
    "bell-audio-volume": (0.0, 1.0, 0.05),     # 0.0 silence .. 1.0 loudest
    "background-image-opacity": (0.0, 1.0, 0.05),
}


def slider_range(opt: Option):
    """Return (min, max, step) if this option should use a slider, else None.

    Kept tolerant of Ghostty version drift: any future 0–1 opacity option is
    picked up from its docs without needing to be listed explicitly."""
    if opt.name in SLIDER_RANGES:
        return SLIDER_RANGES[opt.name]
    if opt.kind == "float":
        d = opt.doc.lower()
        if "fully opaque" in d and "fully transparent" in d:
            return (0.0, 1.0, 0.05)
    return None


@dataclass
class Option:
    name: str
    default: str = ""
    defaults: list[str] = field(default_factory=list)  # for list keys
    doc: str = ""
    values: list[str] = field(default_factory=list)    # enum choices
    kind: str = "text"          # bool|enum|int|float|color|text|list|theme|font|palette|keybind
    is_list: bool = False
    reload_note: str = ""       # non-empty when the option is not fully live
    category: str = "Advanced"
    platform: str | None = None  # "macos"/"linux" if OS-exclusive, else None

    @property
    def is_color(self) -> bool:
        return self.kind == "color"


_ENUM_RE = re.compile(r"\*\s+`([^`]+)`")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d*\.\d+$")


def _classify(opt: Option) -> None:
    name, dflt = opt.name, opt.default
    doc_low = opt.doc.lower()

    # reloadability hints
    if "cannot be reloaded at runtime" in doc_low or "fully restart" in doc_low \
            or "must fully restart" in doc_low:
        opt.reload_note = "needs full Ghostty restart"
    elif "only applies to new windows" in doc_low or "only affects new windows" in doc_low:
        opt.reload_note = "applies to new windows only"
    elif "will not affect existing" in doc_low:
        opt.reload_note = "affects new surfaces only"

    if name == "theme":
        opt.kind = "theme"
        return
    if name == "palette":
        opt.kind, opt.is_list = "palette", True
        return
    if name == "keybind":
        opt.kind, opt.is_list = "keybind", True
        return
    if name.startswith("font-family"):
        opt.kind, opt.is_list = "font", True
        return

    if dflt in ("true", "false"):
        opt.kind = "bool"
        opt.values = ["true", "false"]
        return

    # enum: bulleted backtick values in the docs where the default is one of them
    cands = _ENUM_RE.findall(opt.doc)
    cands = [c for c in cands if " " not in c and len(c) <= 32]
    if cands and dflt and dflt in cands:
        opt.kind = "enum"
        # de-dupe preserving order
        seen, uniq = set(), []
        for c in cands:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        opt.values = uniq
        return

    if opt.is_list:
        opt.kind = "list"
        return

    if _INT_RE.match(dflt):
        # a few integer-defaulted options are semantically fractional
        if "opacity" in name or name in ("minimum-contrast", "mouse-scroll-multiplier",
                                         "bell-audio-volume"):
            opt.kind = "float"
        else:
            opt.kind = "int"
        return
    if _FLOAT_RE.match(dflt):
        opt.kind = "float"
        return

    if name.endswith("-color") or any(h in name for h in COLOR_HINTS) \
            or name.startswith("selection-") or name.startswith("search-"):
        # padding-color is actually an enum; only treat as color if not enum
        if not opt.values:
            opt.kind = "color"
            return

    opt.kind = "text"


# ordered category buckets
CATEGORY_ORDER = [
    "Colors & Theme", "Font", "Cursor", "Window", "Spacing & Metrics",
    "Mouse", "Clipboard & Selection", "Quick Terminal", "Shell & Commands",
    "Keybindings", "macOS", "Linux / GTK", "Advanced",
]


def _categorize(name: str) -> str:
    n = name
    if n in ("theme", "palette", "foreground", "background", "background-opacity",
             "background-blur", "background-image", "minimum-contrast",
             "bold-color", "faint-opacity", "alpha-blending", "split-divider-color",
             "unfocused-split-fill", "unfocused-split-opacity", "window-colorspace",
             "background-opacity-cells", "background-image-fit",
             "background-image-opacity", "background-image-position",
             "background-image-repeat", "osc-color-report-format",
             "palette-generate", "palette-harmonious") \
            or n.endswith("-color") or n.startswith("selection-") \
            or n.startswith("search-"):
        return "Colors & Theme"
    if n.startswith("cursor"):
        return "Cursor"
    if n.startswith("font"):
        return "Font"
    if n.startswith("adjust-") or n.startswith("window-padding") \
            or n in ("grapheme-width-method", "freetype-load-flags"):
        return "Spacing & Metrics"
    if n.startswith("window") or n in ("maximize", "fullscreen", "initial-window",
                                       "resize-overlay", "resize-overlay-duration",
                                       "resize-overlay-position", "class",
                                       "title", "title-report", "undo-timeout",
                                       "auto-update", "auto-update-channel",
                                       "quit-after-last-window-closed",
                                       "quit-after-last-window-closed-delay",
                                       "confirm-close-surface"):
        return "Window"
    if n.startswith("mouse") or n in ("focus-follows-mouse", "click-repeat-interval",
                                      "cursor-click-to-move", "right-click-action",
                                      "link", "link-url", "link-previews"):
        return "Mouse"
    if n.startswith("clipboard") or n in ("copy-on-select", "selection-word-chars",
                                          "selection-clear-on-copy",
                                          "selection-clear-on-typing"):
        return "Clipboard & Selection"
    if n.startswith("quick-terminal"):
        return "Quick Terminal"
    if n in ("shell-integration", "shell-integration-features", "command",
             "initial-command", "working-directory", "env", "term",
             "wait-after-command", "enquiry-response",
             "abnormal-command-exit-runtime", "notify-on-command-finish",
             "notify-on-command-finish-action", "notify-on-command-finish-after",
             "scrollback-limit", "scroll-to-bottom", "image-storage-limit"):
        return "Shell & Commands"
    if n.startswith("keybind") or n in ("input", "key-remap", "macos-shortcuts",
                                        "command-palette-entry", "vt-kam-allowed"):
        return "Keybindings"
    if n.startswith("macos"):
        return "macOS"
    if n.startswith("gtk") or n.startswith("x11") or n.startswith("linux"):
        return "Linux / GTK"
    return "Advanced"


# Doc phrases (all lowercase) that mark an option as exclusive to one OS. Kept
# deliberately narrow and paired with a "mentions the other OS positively" guard
# so cross-platform options (e.g. "supported on macOS and Linux") aren't hidden.
_MAC_ONLY_HINTS = (
    "only supported on macos", "only implemented on macos", "only works on macos",
    "supported currently on macos", "no effect on linux", "no effect on other",
    "only visible with the native macos",
)
_LINUX_ONLY_HINTS = (
    "only supported on linux", "only implemented on linux", "only supported on gtk",
    "only supported in the gtk", "only applies to gtk", "only affects gtk builds",
    "relevant on linux", "only has an effect on linux",
    "feature is only supported on gtk", "configuration only applies to gtk",
    "no effect on macos",
)
# If any of these appear, the option touches both platforms — never hide it.
_CROSS_PLATFORM_HINTS = (
    "macos and linux", "macos and certain linux", "macos and on some linux",
    "macos and some linux", "linux and macos", "on macos and", "macos, linux",
    "macos and windows",
)


def _platform_of(opt: Option) -> str | None:
    """'macos' or 'linux' if the option is exclusive to that OS, else None.

    Keys off the option name prefix first (unambiguous) and falls back to
    scanning the scraped docs. Like the rest of the schema layer this stays
    tolerant of Ghostty version drift — an unrecognised phrase just means the
    option is treated as cross-platform and shown everywhere.
    """
    name = opt.name
    if name.startswith("macos"):
        return "macos"
    if name.startswith(("gtk", "x11", "adw", "linux")):
        return "linux"
    doc = opt.doc.lower()
    if any(p in doc for p in _CROSS_PLATFORM_HINTS):
        return None
    mac_only = any(p in doc for p in _MAC_ONLY_HINTS)
    linux_only = any(p in doc for p in _LINUX_ONLY_HINTS)
    if mac_only and not linux_only:
        return "macos"
    if linux_only and not mac_only:
        return "linux"
    return None


def platform_visible(opt: Option) -> bool:
    """Whether an option should be shown on the current OS."""
    if CURRENT_PLATFORM is None or opt.platform is None:
        return True
    return opt.platform == CURRENT_PLATFORM


def load_schema() -> dict[str, Option]:
    """Parse `ghostty +show-config --default --docs` into typed Options."""
    if not GHOSTTY:
        raise RuntimeError("ghostty binary not found on PATH")
    proc = _run([GHOSTTY, "+show-config", "--default", "--docs"], timeout=30)
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError("failed to read ghostty defaults: " + proc.stderr.strip())

    options: dict[str, Option] = {}
    doc_buf: list[str] = []
    key_re = re.compile(r"^([a-z0-9][a-z0-9-]*)\s*=\s?(.*)$")

    for raw in proc.stdout.splitlines():
        if raw.startswith("#"):
            content = raw[1:]
            if content.startswith(" "):
                content = content[1:]
            doc_buf.append(content)
            continue
        if raw.strip() == "":
            doc_buf = []
            continue
        m = key_re.match(raw)
        if not m:
            doc_buf = []
            continue
        name, value = m.group(1), m.group(2)
        if name in options:
            o = options[name]
            o.is_list = True
            o.defaults.append(value)
        else:
            o = Option(name=name, default=value, doc="\n".join(doc_buf).strip())
            o.defaults = [value] if value else []
            options[name] = o
        doc_buf = []

    for name, o in options.items():
        if name in FORCE_LIST:
            o.is_list = True
        _classify(o)
        o.category = _categorize(name)
        o.platform = _platform_of(o)
    return options


# --------------------------------------------------------------------------- #
#  The config file: parse, edit in place, render, save
# --------------------------------------------------------------------------- #

class ConfigFile:
    """A Ghostty config file that can be edited while preserving layout."""

    KEY_RE = re.compile(r"^(\s*)([a-z0-9][a-z0-9-]*)(\s*=\s*)(.*?)(\s*)$")
    MANAGED_HEADER = "# ─────────── added by SpookiUI ───────────"
    # Recognize the header written by earlier versions so we don't append a
    # second managed section to configs they created.
    LEGACY_HEADERS = ("# ─────────── added by GhostlyConfig ───────────",)

    def __init__(self, path: str):
        self.path = path
        self.lines: list[str] = []
        self.reload()

    def reload(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.lines = fh.read().split("\n")
        else:
            self.lines = []

    # ---- reading -------------------------------------------------------- #
    def _key_at(self, i: int) -> tuple[str, str] | None:
        line = self.lines[i]
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped == "":
            return None
        m = self.KEY_RE.match(line)
        if not m:
            return None
        return m.group(2), m.group(4)

    def indices_of(self, name: str) -> list[int]:
        out = []
        for i in range(len(self.lines)):
            kv = self._key_at(i)
            if kv and kv[0] == name:
                out.append(i)
        return out

    def get_values(self, name: str) -> list[str]:
        vals = []
        for i in self.indices_of(name):
            kv = self._key_at(i)
            if kv:
                vals.append(_unquote(kv[1]))
        return vals

    def get_value(self, name: str) -> str | None:
        vals = self.get_values(name)
        return vals[-1] if vals else None

    # ---- writing (in memory) -------------------------------------------- #
    def _format_line(self, name: str, value: str, quote_style: str | None) -> str:
        v = value
        needs_quote = (" " in value or "\t" in value)
        if quote_style == '"' or (quote_style is None and needs_quote):
            v = '"' + value.replace('"', '\\"') + '"'
        return f"{name} = {v}"

    def set_scalar(self, name: str, value: str) -> None:
        idxs = self.indices_of(name)
        if idxs:
            i = idxs[-1]
            old = self.lines[i]
            m = self.KEY_RE.match(old)
            quote = '"' if (m and m.group(4).startswith('"')) else None
            self.lines[i] = self._format_line(name, value, quote)
            # collapse any earlier duplicates so last-wins is unambiguous
            for j in idxs[:-1]:
                self.lines[j] = "# " + self.lines[j] + "  # (superseded)"
        else:
            self._append_managed([self._format_line(name, value, None)])

    def set_list(self, name: str, values: list[str]) -> None:
        idxs = self.indices_of(name)
        new_lines = [self._format_line(name, v, None) for v in values]
        if idxs:
            insert_at = idxs[0]
            keep = set(idxs)
            rebuilt: list[str] = []
            inserted = False
            for i, line in enumerate(self.lines):
                if i in keep:
                    if not inserted:
                        rebuilt.extend(new_lines)
                        inserted = True
                    continue
                rebuilt.append(line)
            self.lines = rebuilt
        elif new_lines:
            self._append_managed(new_lines)

    def unset(self, name: str) -> None:
        for i in self.indices_of(name):
            self.lines[i] = "# " + self.lines[i] + "  # (removed)"

    def _append_managed(self, new_lines: list[str]) -> None:
        headers = (self.MANAGED_HEADER, *self.LEGACY_HEADERS)
        if not any(h in self.lines for h in headers):
            if self.lines and self.lines[-1].strip() != "":
                self.lines.append("")
            self.lines.append(self.MANAGED_HEADER)
        self.lines.extend(new_lines)

    def render(self) -> str:
        return "\n".join(self.lines)

    # ---- persistence ---------------------------------------------------- #
    def write(self, text: str | None = None) -> None:
        if text is None:
            text = self.render()
        if text and not text.endswith("\n"):
            text += "\n"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def backup(self) -> str | None:
        """Make at most one backup per day (a safety net; the TUI also keeps
        an in-memory original for its own revert)."""
        if not os.path.exists(self.path):
            return None
        dst = f"{self.path}.spookiui.{time.strftime('%Y%m%d')}.bak"
        if not os.path.exists(dst):
            shutil.copy2(self.path, dst)
        return dst


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"')
    return v


# --------------------------------------------------------------------------- #
#  Ghostty control: validate + live reload
# --------------------------------------------------------------------------- #

def validate(text: str) -> tuple[bool, list[str]]:
    """Validate a full config file text. Returns (ok, error_lines)."""
    if not GHOSTTY:
        return True, []  # can't validate; assume ok
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".spookiui.cfg", delete=False) as tf:
        tf.write(text)
        tmp = tf.name
    try:
        proc = _run([GHOSTTY, "+validate-config", "--config-file=" + tmp], timeout=30)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    out = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode == 0 and not out:
        return True, []
    errs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # strip the temp path prefix for readability
        line = re.sub(re.escape(tmp) + r":?", "", line).lstrip(": ")
        errs.append(line)
    return (proc.returncode == 0 and not errs), errs


def reload_ghostty() -> tuple[bool, str]:
    """Trigger Ghostty to reload its configuration (live). macOS clicks the
    'Reload Configuration' menu item via AppleScript; Linux sends the running
    Ghostty process the SIGUSR2 signal it reloads on."""
    if IS_MACOS:
        return _reload_macos()
    if IS_LINUX:
        return _reload_linux()
    return False, "auto-reload not supported on this platform; press your reload_config keybind"


def _reload_macos() -> tuple[bool, str]:
    script = (
        'tell application "System Events" to tell process "Ghostty" to '
        'click menu item "Reload Configuration" of menu 1 of '
        'menu bar item "Ghostty" of menu bar 1'
    )
    try:
        proc = _run(["osascript", "-e", script], timeout=10)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if proc.returncode == 0:
        return True, "reloaded"
    err = proc.stderr.strip() or "reload failed"
    if "not allowed assistive" in err or "1002" in err or "osascript is not allowed" in err:
        err = ("needs Accessibility permission: System Settings → Privacy & "
               "Security → Accessibility → enable your terminal")
    elif "Ghostty" in err and "process" in err:
        err = "Ghostty doesn't appear to be running"
    return False, err


def _ghostty_pids() -> list[int]:
    """PIDs of running Ghostty processes (best effort)."""
    pids: list[int] = []
    for args in (["pgrep", "-x", "ghostty"], ["pgrep", "-if", "ghostty"]):
        try:
            proc = _run(args, timeout=5)
        except Exception:  # noqa: BLE001
            continue
        for line in proc.stdout.split():
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid != os.getpid() and pid not in pids:
                pids.append(pid)
        if pids:
            break
    return pids


def _reload_linux() -> tuple[bool, str]:
    """Ghostty reloads its config on SIGUSR2. Signal every running instance."""
    import signal
    pids = _ghostty_pids()
    if not pids:
        return False, "Ghostty doesn't appear to be running"
    sent = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGUSR2)
            sent += 1
        except ProcessLookupError:
            continue
        except PermissionError:
            return False, f"not permitted to signal Ghostty (pid {pid})"
        except OSError as e:
            return False, str(e)
    if sent:
        return True, "reloaded"
    return False, "Ghostty doesn't appear to be running"


def is_ghostty_running() -> bool:
    try:
        proc = _run(["pgrep", "-x", "ghostty"], timeout=5)
        if proc.stdout.strip():
            return True
        proc = _run(["pgrep", "-if", "Ghostty.app"], timeout=5)
        return bool(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def list_themes() -> list[str]:
    if not GHOSTTY:
        return []
    proc = _run([GHOSTTY, "+list-themes", "--plain"], timeout=30)
    out = proc.stdout if proc.stdout else proc.stderr
    themes = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # strip trailing " (resources)" / " (user)"
        line = re.sub(r"\s*\((resources|user)\)\s*$", "", line)
        themes.append(line)
    return themes


def list_fonts() -> list[str]:
    if not GHOSTTY:
        return []
    proc = _run([GHOSTTY, "+list-fonts"], timeout=45)
    fams = []
    for line in proc.stdout.splitlines():
        if line and not line[0].isspace() and line.strip():
            fams.append(line.strip())
    # de-dupe preserving order
    seen, out = set(), []
    for f in fams:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


_ACTIONS_CACHE: list[str] | None = None
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Keybind builder: modifiers Ghostty accepts, plus a curated list of named keys
# for the "special key" picker (letters/digits are captured by pressing them).
KEYBIND_MODS = ["super", "ctrl", "alt", "shift"]
KEYBIND_MOD_ALIASES = {
    "cmd": "super", "command": "super", "control": "ctrl",
    "opt": "alt", "option": "alt", "meta": "alt",
}
KEYBIND_NAMED_KEYS = [
    "space", "enter", "tab", "escape", "backspace", "delete", "insert",
    "home", "end", "page_up", "page_down", "up", "down", "left", "right",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "minus", "equal", "plus", "comma", "period", "slash", "backslash",
    "semicolon", "apostrophe", "grave_accent", "left_bracket", "right_bracket",
]


def list_actions() -> list[str]:
    """Keybind action names Ghostty knows about (for the keybind builder)."""
    global _ACTIONS_CACHE
    if _ACTIONS_CACHE is not None:
        return _ACTIONS_CACHE
    acts: list[str] = []
    if GHOSTTY:
        proc = _run([GHOSTTY, "+list-actions"], timeout=20)
        for line in (proc.stdout or proc.stderr).splitlines():
            line = line.strip()
            if _ACTION_RE.match(line):
                acts.append(line)
    seen, out = set(), []
    for a in acts:
        if a not in seen:
            seen.add(a)
            out.append(a)
    _ACTIONS_CACHE = out
    return out


# --------------------------------------------------------------------------- #
#  Theme colour resolution (for the live colour preview)
# --------------------------------------------------------------------------- #
#
# Themes are plain Ghostty config fragments (palette/background/foreground/…)
# living in the user's themes dir or the Ghostty resources dir. We parse them
# directly so we can render a colour card without applying anything. Everything
# here degrades gracefully: a theme we can't find or read just yields no card.

def ghostty_resources_dir() -> str | None:
    """Best-effort path to the Ghostty resources dir (contains `themes/`)."""
    d = os.environ.get("GHOSTTY_RESOURCES_DIR")
    if d and os.path.isdir(d):
        return d
    cands = []
    if IS_MACOS:
        cands += [
            "/Applications/Ghostty.app/Contents/Resources/ghostty",
            os.path.expanduser(
                "~/Applications/Ghostty.app/Contents/Resources/ghostty"),
        ]
    if GHOSTTY:
        # <prefix>/bin/ghostty  ->  <prefix>/share/ghostty
        prefix = os.path.dirname(os.path.dirname(os.path.realpath(GHOSTTY)))
        cands.append(os.path.join(prefix, "share", "ghostty"))
    cands += ["/usr/share/ghostty", "/usr/local/share/ghostty",
              "/opt/homebrew/share/ghostty"]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return None


def theme_search_dirs() -> list[str]:
    dirs = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.append(os.path.join(xdg, "ghostty", "themes"))
    dirs.append(os.path.expanduser("~/.config/ghostty/themes"))
    if IS_MACOS:
        dirs.append(os.path.expanduser(
            "~/Library/Application Support/com.mitchellh.ghostty/themes"))
    rd = ghostty_resources_dir()
    if rd:
        dirs.append(os.path.join(rd, "themes"))
    return dirs


def find_theme_file(name: str) -> str | None:
    name = name.strip()
    if not name:
        return None
    for d in theme_search_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def theme_variant_name(value: str) -> str:
    """A theme value may be composite (`light:A,dark:B`); pick one name to
    preview, preferring the dark variant."""
    value = value.strip()
    if "," not in value and ":" not in value:
        return value
    picks, first = {}, None
    for part in value.split(","):
        part = part.strip()
        if ":" in part:
            k, _, v = part.partition(":")
            picks[k.strip().lower()] = v.strip()
        elif first is None:
            first = part
    return picks.get("dark") or picks.get("light") or first or value


_THEME_COLOR_CACHE: dict[str, dict | None] = {}


def parse_theme_colors(name: str) -> dict | None:
    """Parse a theme file into {palette:{i:hex}, foreground, background, cursor}."""
    if name in _THEME_COLOR_CACHE:
        return _THEME_COLOR_CACHE[name]
    path = find_theme_file(name)
    res: dict | None = None
    if path:
        palette, fg, bg, cursor = {}, None, None, None
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if key == "palette":
                        idx, _, col = val.partition("=")
                        try:
                            palette[int(idx)] = col.strip()
                        except ValueError:
                            pass
                    elif key == "background":
                        bg = val
                    elif key == "foreground":
                        fg = val
                    elif key == "cursor-color":
                        cursor = val
            res = {"palette": palette, "foreground": fg,
                   "background": bg, "cursor": cursor}
        except OSError:
            res = None
    _THEME_COLOR_CACHE[name] = res
    return res


# --------------------------------------------------------------------------- #
#  Session controller shared by TUI + CLI
# --------------------------------------------------------------------------- #

class Session:
    def __init__(self):
        self.schema = load_schema()
        self.cfg = ConfigFile(config_path())
        self.original_text = self.cfg.render()
        self.backup_path: str | None = None
        self.auto_apply = True
        self.dirty = False

    def ensure_backup(self) -> None:
        if self.backup_path is None:
            self.backup_path = self.cfg.backup()

    def effective(self, name: str) -> str:
        """Current effective value (user override or default)."""
        opt = self.schema.get(name)
        val = self.cfg.get_value(name)
        if val is not None:
            return val
        return opt.default if opt else ""

    def effective_list(self, name: str) -> list[str]:
        vals = self.cfg.get_values(name)
        if vals:
            return vals
        opt = self.schema.get(name)
        return list(opt.defaults) if opt else []

    def is_overridden(self, name: str) -> bool:
        # An option counts as changed only if it's present in the config *and*
        # its effective value differs from Ghostty's default. Re-setting a key
        # to its default value (e.g. toggling a bool back) must clear the mark.
        if not self.cfg.indices_of(name):
            return False
        opt = self.schema.get(name)
        if opt is None:
            return True  # unknown key the user added by hand — treat as changed
        if opt.is_list:
            return self.effective_list(name) != list(opt.defaults)
        return self.effective(name) != opt.default

    def stage_scalar(self, name: str, value: str) -> None:
        self.cfg.set_scalar(name, value)
        self.dirty = True

    def stage_list(self, name: str, values: list[str]) -> None:
        self.cfg.set_list(name, values)
        self.dirty = True

    def stage_unset(self, name: str) -> None:
        self.cfg.unset(name)
        self.dirty = True

    def apply(self) -> tuple[bool, str]:
        """Validate + write + reload. Returns (ok, message)."""
        text = self.cfg.render()
        ok, errs = validate(text)
        if not ok:
            return False, "invalid: " + (errs[0] if errs else "validation failed")
        self.ensure_backup()
        self.cfg.write(text)
        self.dirty = False
        if self.auto_apply:
            r_ok, msg = reload_ghostty()
            if r_ok:
                return True, "saved + reloaded live"
            return True, "saved (reload: " + msg + ")"
        return True, "saved (auto-apply off — press 's'/reload to apply live)"

    def revert_all(self) -> tuple[bool, str]:
        self.cfg.lines = self.original_text.split("\n")
        self.dirty = False
        self.cfg.write(self.original_text)
        if self.auto_apply:
            reload_ghostty()
        return True, "reverted to session start"

    def restore_defaults(self) -> tuple[bool, str]:
        """Clear the config file entirely so Ghostty falls back to every one of
        its built-in defaults. Follows the same validate → back up → write →
        reload → rollback discipline as every other mutation path."""
        snap = list(self.cfg.lines)
        blank = (
            self.cfg.MANAGED_HEADER + "\n"
            "# Configuration reset to Ghostty defaults by SpookiUI.\n"
        )
        ok, errs = validate(blank)
        if not ok:
            self.cfg.lines = snap
            return False, "invalid: " + (errs[0] if errs else "validation failed")
        self.ensure_backup()
        self.cfg.lines = blank.rstrip("\n").split("\n")
        self.cfg.write(blank)
        self.dirty = False
        if self.auto_apply and CAN_RELOAD:
            r_ok, msg = reload_ghostty()
            if r_ok:
                return True, "restored Ghostty defaults + reloaded live"
            return True, "restored Ghostty defaults (reload: " + msg + ")"
        return True, "restored Ghostty defaults"

    def overrides(self) -> list[tuple[str, str]]:
        """Options the user has changed from default (name, current value)."""
        out = []
        for name, opt in self.schema.items():
            if self.is_overridden(name):
                if opt.is_list:
                    out.append((name, ", ".join(self.effective_list(name)) or "(cleared)"))
                else:
                    out.append((name, self.effective(name)))
        return out


# --------------------------------------------------------------------------- #
#  Color helpers (for swatches)
# --------------------------------------------------------------------------- #

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex(value: str) -> tuple[int, int, int] | None:
    m = _HEX_RE.match(value.strip())
    if not m:
        return None
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_256(r: int, g: int, b: int) -> int:
    """Map an RGB triple to the nearest xterm-256 color index."""
    def to6(v: int) -> int:
        if v < 48:
            return 0
        if v < 115:
            return 1
        return (v - 35) // 40
    ri, gi, bi = to6(r), to6(g), to6(b)
    ci = 16 + 36 * ri + 6 * gi + bi
    # grayscale ramp candidate
    avg = (r + g + b) // 3
    if avg < 8:
        gray = 232
    elif avg > 238:
        gray = 255
    else:
        gray = 232 + (avg - 8) // 10
    # pick whichever is closer
    def cube_val(i):
        return 0 if i == 0 else 55 + i * 40
    cr, cg, cb = cube_val(ri), cube_val(gi), cube_val(bi)
    gl = 8 + (gray - 232) * 10
    d_cube = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    d_gray = (gl - r) ** 2 + (gl - g) ** 2 + (gl - b) ** 2
    return ci if d_cube <= d_gray else gray


# --------------------------------------------------------------------------- #
#  The interactive TUI
# --------------------------------------------------------------------------- #

def run_tui(sess: "Session") -> None:
    import curses
    import locale
    # Make curses render UTF-8 (slider bar, badges, box chars) regardless of how
    # the process was started; addstr encodes via the locale's preferred codec.
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        curses.set_escdelay(25)  # make single-ESC snappy, not a 1s wait
    except Exception:
        pass
    try:
        curses.wrapper(lambda scr: App(scr, sess).run())
    except KeyboardInterrupt:
        # Ctrl+C from inside a modal getch loop — wrapper has already restored
        # the terminal in its finally, so just exit quietly.
        pass


class App:
    def __init__(self, stdscr, sess: Session):
        import curses
        self.curses = curses
        self.scr = stdscr
        self.sess = sess
        self.status = ""
        self.status_kind = "info"     # info|ok|error|warn
        self.focus = "cats"           # cats|opts
        self.cat_idx = 0
        self.opt_idx = 0
        self.opt_scroll = 0
        self.doc_scroll = 0
        self.search = ""
        self.search_mode = False      # showing flat search results

        # options per category, sorted alpha — OS-exclusive options that don't
        # apply to the current platform are hidden.
        self.by_cat: dict[str, list[str]] = {}
        for c in CATEGORY_ORDER:
            names = sorted(n for n, o in sess.schema.items()
                           if o.category == c and platform_visible(o))
            if names:
                self.by_cat[c] = names
        self.categories = [c for c in CATEGORY_ORDER if c in self.by_cat]

        self._swatch_cache: dict[int, int] = {}
        self._pair_cache: dict[tuple, int] = {}   # (fg_idx, bg_idx) -> pair
        self._next_pair = 32
        self._init_colors()

        # Check GitHub for a newer release in the background so startup isn't
        # blocked on the network. Result is picked up on the next redraw.
        self._update_info: dict | None = None
        self._update_announced = False
        self._start_update_check()

        curses.curs_set(0)
        stdscr.keypad(True)

    def _start_update_check(self):
        if _update_check_disabled():
            return

        def worker():
            try:
                self._update_info = check_for_update()
            except Exception:  # noqa: BLE001 — never let the checker crash the TUI
                self._update_info = None

        threading.Thread(target=worker, daemon=True).start()

    # ---- colors --------------------------------------------------------- #
    def _init_colors(self):
        c = self.curses
        c.start_color()
        try:
            c.use_default_colors()
            bg = -1
        except Exception:
            bg = c.COLOR_BLACK
        # 1 header, 2 footer hint, 3 selected, 4 dim, 5 accent, 6 ok,
        # 7 error, 8 warn, 9 category-selected, 10 title
        c.init_pair(1, c.COLOR_BLACK, c.COLOR_CYAN)
        c.init_pair(2, c.COLOR_CYAN, bg)
        c.init_pair(3, c.COLOR_BLACK, c.COLOR_WHITE)
        c.init_pair(4, c.COLOR_WHITE, bg)
        c.init_pair(5, c.COLOR_YELLOW, bg)
        c.init_pair(6, c.COLOR_GREEN, bg)
        c.init_pair(7, c.COLOR_RED, bg)
        c.init_pair(8, c.COLOR_MAGENTA, bg)
        c.init_pair(9, c.COLOR_BLACK, c.COLOR_YELLOW)
        c.init_pair(10, c.COLOR_MAGENTA, bg)
        self.has_swatch = c.can_change_color() or c.COLORS >= 256

    def swatch_pair(self, value: str):
        c = self.curses
        rgb = parse_hex(value)
        if not rgb or c.COLORS < 256:
            return None
        idx = rgb_to_256(*rgb)
        if idx in self._swatch_cache:
            return self._swatch_cache[idx]
        if self._next_pair >= min(c.COLOR_PAIRS, 250):
            return None
        pair = self._next_pair
        self._next_pair += 1
        try:
            c.init_pair(pair, idx, -1)
        except Exception:
            return None
        self._swatch_cache[idx] = pair
        return pair

    def color_pair(self, fg_hex, bg_hex=None):
        """A curses pair for an explicit fg (and optional bg) hex, cached.
        Returns None when the terminal can't do 256 colours."""
        c = self.curses
        if c.COLORS < 256:
            return None
        frgb = parse_hex(fg_hex) if fg_hex else None
        brgb = parse_hex(bg_hex) if bg_hex else None
        fi = rgb_to_256(*frgb) if frgb else -1
        bi = rgb_to_256(*brgb) if brgb else -1
        key = (fi, bi)
        if key in self._pair_cache:
            return self._pair_cache[key]
        if self._next_pair >= min(c.COLOR_PAIRS, 250):
            return None
        pair = self._next_pair
        self._next_pair += 1
        try:
            c.init_pair(pair, fi, bi)
        except Exception:
            return None
        self._pair_cache[key] = pair
        return pair

    # ---- colour preview ------------------------------------------------- #
    def _effective_colors(self, theme_override=None) -> dict:
        """Resolve the colours that would actually render: Ghostty defaults,
        then the active (or overridden) theme, then explicit config overrides.
        Pass theme_override to preview a theme without touching config."""
        schema = self.sess.schema
        pal: dict[int, str] = {}
        popt = schema.get("palette")
        if popt:
            for d in popt.defaults:
                idx, _, col = d.partition("=")
                try:
                    pal[int(idx)] = col.strip()
                except ValueError:
                    pass
        fg = schema["foreground"].default if "foreground" in schema else "#ffffff"
        bg = schema["background"].default if "background" in schema else "#000000"
        cursor = None

        theme_val = (theme_override if theme_override is not None
                     else self.sess.effective("theme"))
        if theme_val:
            tc = parse_theme_colors(theme_variant_name(theme_val))
            if tc:
                pal.update(tc["palette"])
                fg = tc["foreground"] or fg
                bg = tc["background"] or bg
                cursor = tc["cursor"] or cursor

        if theme_override is None:  # layer explicit config overrides on top
            fo = self.sess.cfg.get_value("foreground")
            if fo:
                fg = fo
            bo = self.sess.cfg.get_value("background")
            if bo:
                bg = bo
            for v in self.sess.cfg.get_values("palette"):
                idx, _, col = v.partition("=")
                try:
                    pal[int(idx)] = col.strip()
                except ValueError:
                    pass

        palette = [pal.get(i, "#000000") for i in range(16)]
        return {"palette": palette, "fg": fg, "bg": bg, "cursor": cursor}

    def _draw_color_preview(self, y, x, width, colors) -> int:
        """Render a compact theme card (two swatch rows + a sample line).
        Returns the number of rows drawn (0 if colours are unavailable)."""
        c = self.curses
        if not self.has_swatch or c.COLORS < 256 or width < 18:
            return 0
        pal, fg, bg = colors["palette"], colors["fg"], colors["bg"]
        rows = 0
        for band in range(2):
            yy = y + band
            xx = x
            for i in range(8):
                pair = self.color_pair(pal[band * 8 + i])
                self.safe(yy, xx, "██", c.color_pair(pair) if pair else c.color_pair(4))
                xx += 3
            rows += 1
        sample = " AaBbCc 123 #!$ "
        pair = self.color_pair(fg, bg)
        self.safe(y + 2, x, sample[:width],
                  (c.color_pair(pair) if pair else c.color_pair(4)) | c.A_BOLD)
        rows += 1
        return rows

    # ---- geometry helpers ---------------------------------------------- #
    def dims(self):
        h, w = self.scr.getmaxyx()
        return h, w

    def safe(self, y, x, text, attr=0):
        h, w = self.dims()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - 1)]
        try:
            self.scr.addstr(y, x, text, attr)
        except self.curses.error:
            pass

    # ---- current option ------------------------------------------------- #
    def current_names(self) -> list[str]:
        if self.search_mode:
            return self._search_results
        if not self.categories:
            return []
        return self.by_cat[self.categories[self.cat_idx]]

    def current_option(self) -> Option | None:
        names = self.current_names()
        if not names:
            return None
        self.opt_idx = max(0, min(self.opt_idx, len(names) - 1))
        return self.sess.schema[names[self.opt_idx]]

    # ---- draw ----------------------------------------------------------- #
    def run(self):
        c = self.curses
        while True:
            self.draw()
            try:
                ch = self.scr.getch()
            except KeyboardInterrupt:
                ch = 3  # Ctrl+C arrived as a signal — treat as a graceful quit
            if ch == c.KEY_RESIZE:
                continue
            if not self.handle_key(ch):
                break

    def draw(self):
        c = self.curses
        # Announce a newly-discovered update once, without clobbering a message
        # the user is already looking at.
        info = self._update_info
        if info and info.get("outdated") and not self._update_announced:
            self._update_announced = True
            if not self.status:
                self._msg(f"SpookiUI {info['latest']} is available — {info['url']}", "warn")
        self.scr.erase()
        h, w = self.dims()
        self._draw_header(w)
        body_top, body_bottom = 1, h - 3
        self._draw_columns(body_top, body_bottom, w)
        self._draw_footer(h, w)
        self.scr.noutrefresh()
        c.doupdate()

    def _draw_header(self, w):
        c = self.curses
        title = " SpookiUI · live Ghostty configurator "
        flags = []
        info = self._update_info
        if info and info.get("outdated"):
            flags.append("⬆ UPDATE " + info["latest"])
        flags.append("AUTO-APPLY:ON" if self.sess.auto_apply else "AUTO-APPLY:OFF")
        if self.sess.dirty:
            flags.append("UNSAVED*")
        flags.append("live" if (self.sess.auto_apply and CAN_RELOAD) else "manual")
        right = " " + " · ".join(flags) + " "
        bar = title + " " * max(0, w - len(title) - len(right)) + right
        self.safe(0, 0, bar[:w], c.color_pair(1) | c.A_BOLD)

    def _draw_columns(self, top, bottom, w):
        c = self.curses
        cat_w = 22
        opt_w = max(28, min(40, (w - cat_w) // 2))
        det_x = cat_w + opt_w + 1

        # categories
        for i, cat in enumerate(self.categories):
            y = top + i
            if y > bottom:
                break
            attr = c.A_NORMAL
            label = f" {cat}"
            if self.search_mode:
                attr = c.color_pair(4)
            elif i == self.cat_idx:
                attr = c.color_pair(9) | c.A_BOLD if self.focus == "cats" \
                    else c.color_pair(5) | c.A_BOLD
            self.safe(y, 0, label.ljust(cat_w)[:cat_w], attr)
        # vertical separators
        for y in range(top, bottom + 1):
            self.safe(y, cat_w, "│", c.color_pair(4))
            self.safe(y, cat_w + opt_w, "│", c.color_pair(4))

        # option list
        names = self.current_names()
        rows = bottom - top + 1
        if self.opt_idx < self.opt_scroll:
            self.opt_scroll = self.opt_idx
        elif self.opt_idx >= self.opt_scroll + rows:
            self.opt_scroll = self.opt_idx - rows + 1
        for r in range(rows):
            i = self.opt_scroll + r
            if i >= len(names):
                break
            name = names[i]
            opt = self.sess.schema[name]
            y = top + r
            selected = (i == self.opt_idx) and (self.focus == "opts" or self.search_mode)
            over = self.sess.is_overridden(name)
            mark = "●" if over else " "
            val = self._short_value(opt)
            x0 = cat_w + 1
            base = c.color_pair(3) | c.A_BOLD if selected else c.A_NORMAL
            self.safe(y, x0, mark, (c.color_pair(6) if over else c.color_pair(4)) | (c.A_BOLD if selected else 0))
            nm = name[: opt_w - 4]
            self.safe(y, x0 + 2, nm.ljust(opt_w - 3)[: opt_w - 3], base)
        # scroll indicator
        if len(names) > rows:
            self.safe(top, cat_w + opt_w - 1, "↕", c.color_pair(5))

        # detail pane
        self._draw_detail(top, bottom, det_x, w - det_x - 1)

    def _short_value(self, opt: Option) -> str:
        if opt.is_list:
            vals = self.sess.effective_list(opt.name)
            return f"[{len(vals)}]"
        return self.sess.effective(opt.name)

    def _draw_detail(self, top, bottom, x, width):
        c = self.curses
        if width < 10:
            return
        opt = self.current_option()
        if not opt:
            self.safe(top, x, "no options", c.color_pair(4))
            return
        y = top
        self.safe(y, x, opt.name, c.color_pair(10) | c.A_BOLD); y += 1
        kind = opt.kind + (" · list" if opt.is_list and opt.kind not in ("list", "font", "palette", "keybind") else "")
        self.safe(y, x, f"type: {kind}", c.color_pair(2)); y += 1

        if opt.is_list:
            vals = self.sess.effective_list(opt.name)
            self.safe(y, x, "value:", c.color_pair(4)); y += 1
            for v in vals[: 6]:
                self._draw_value_line(y, x + 2, width - 2, opt, v)
                y += 1
            if len(vals) > 6:
                self.safe(y, x + 2, f"… +{len(vals) - 6} more", c.color_pair(4)); y += 1
            if not vals:
                self.safe(y, x + 2, "(none)", c.color_pair(4)); y += 1
        else:
            cur = self.sess.effective(opt.name)
            self.safe(y, x, "value: ", c.color_pair(4))
            self._draw_value_line(y, x + 7, width - 7, opt, cur, bold=True)
            y += 1
            self.safe(y, x, f"default: {opt.default or '(empty)'}"[:width], c.color_pair(4)); y += 1

        overridden = "yes" if self.sess.is_overridden(opt.name) else "no (default)"
        self.safe(y, x, f"changed: {overridden}", c.color_pair(6) if self.sess.is_overridden(opt.name) else c.color_pair(4)); y += 1
        if opt.reload_note:
            self.safe(y, x, "⚠ " + opt.reload_note, c.color_pair(8)); y += 1
        if opt.values and opt.kind in ("enum", "bool"):
            self.safe(y, x, "choices: " + ", ".join(opt.values), c.color_pair(5)); y += 1
        y += 1
        # live colour preview for colour-related options
        if (opt.name == "theme" or opt.kind in ("color", "theme", "palette")
                or opt.category == "Colors & Theme") and y + 3 <= bottom:
            self.safe(y, x, "─ preview " + "─" * max(0, width - 10), c.color_pair(4)); y += 1
            used = self._draw_color_preview(y, x, width, self._effective_colors())
            if used:
                y += used + 1
        # docs
        self.safe(y, x, "─ docs " + "─" * max(0, width - 7), c.color_pair(4)); y += 1
        doc_lines = self._wrap(opt.doc or "(no documentation)", width)
        for line in doc_lines[self.doc_scroll:]:
            if y > bottom:
                self.safe(bottom, x, "… (scroll docs: [ ])", c.color_pair(4))
                break
            self.safe(y, x, line, c.color_pair(4))
            y += 1

    def _draw_value_line(self, y, x, width, opt: Option, value, bold=False):
        c = self.curses
        attr = c.A_BOLD if bold else c.A_NORMAL
        rgb = parse_hex(value if not opt.is_list else value.split("=")[-1])
        if rgb:
            pair = self.swatch_pair(value if not opt.is_list else value.split("=")[-1])
            self.safe(y, x, "██ ", (c.color_pair(pair) if pair else c.color_pair(5)))
            self.safe(y, x + 3, str(value)[: width - 3], c.color_pair(5) | attr)
        else:
            self.safe(y, x, str(value)[:width], c.color_pair(5) | attr)

    def _wrap(self, text, width):
        out = []
        for para in text.split("\n"):
            if not para.strip():
                out.append("")
                continue
            indent = len(para) - len(para.lstrip())
            words = para.split()
            cur = " " * indent
            for wd in words:
                if len(cur) + len(wd) + 1 > width and cur.strip():
                    out.append(cur)
                    cur = " " * indent + wd
                else:
                    cur = (cur + " " + wd) if cur.strip() else (cur + wd)
            out.append(cur)
        return out

    def _draw_footer(self, h, w):
        c = self.curses
        sy = h - 2
        # status line
        kindmap = {"ok": 6, "error": 7, "warn": 8, "info": 2}
        self.safe(sy, 0, self.status[:w], c.color_pair(kindmap.get(self.status_kind, 2)) | c.A_BOLD)
        # hints
        if self.search_mode:
            hints = " type to filter · ↑↓ move · Enter edit · Esc exit search "
        elif self.focus == "cats":
            hints = " ↑↓ category · →/Enter options · / search · a auto-apply · d changes · ? help · q quit "
        else:
            hints = " ↑↓ option · Enter/→ edit · ← back · u reset · s save · r reload · / search · ? help · q quit "
        bar = hints + " " * max(0, w - len(hints))
        self.safe(h - 1, 0, bar[:w], c.color_pair(1))

    # ---- key handling --------------------------------------------------- #
    def handle_key(self, ch) -> bool:
        c = self.curses
        if self.search_mode:
            return self._handle_search_key(ch)

        if ch in (ord("q"), ord("Q"), 3, 24):  # q, Ctrl+C, Ctrl+X
            return self._quit()
        if ch == ord("?"):
            self._help()
            return True
        if ch in (ord("\t"),):
            self.focus = "opts" if self.focus == "cats" else "cats"
            return True
        if ch == ord("/"):
            self._enter_search()
            return True
        if ch in (ord("a"), ord("A")):
            self.sess.auto_apply = not self.sess.auto_apply
            self._msg(f"auto-apply {'ON — changes go live' if self.sess.auto_apply else 'OFF — staged only'}", "info")
            return True
        if ch in (ord("d"), ord("D")):
            self._changes_overlay()
            return True
        if ch in (ord("s"), ord("S")):
            ok, m = self.sess.apply()
            self._msg(m, "ok" if ok else "error")
            return True
        if ch in (ord("r"),):
            rok, m = reload_ghostty()
            self._msg("reload: " + m, "ok" if rok else "warn")
            return True
        if ch in (ord("R"),):
            if self._confirm("Revert ALL changes to session start?"):
                ok, m = self.sess.revert_all()
                self._msg(m, "ok")
            return True
        if ch in (ord("X"),):
            if self._confirm("Wipe config & restore ALL Ghostty defaults? (backup kept)"):
                ok, m = self.sess.restore_defaults()
                self._msg(m, "ok" if ok else "error")
            return True
        if ch in (ord("["),):
            self.doc_scroll = max(0, self.doc_scroll - 1); return True
        if ch in (ord("]"),):
            self.doc_scroll += 1; return True

        if self.focus == "cats":
            return self._handle_cat_key(ch)
        return self._handle_opt_key(ch)

    def _handle_cat_key(self, ch) -> bool:
        c = self.curses
        if ch in (c.KEY_UP, ord("k")):
            self.cat_idx = (self.cat_idx - 1) % len(self.categories)
            self.opt_idx = self.opt_scroll = self.doc_scroll = 0
        elif ch in (c.KEY_DOWN, ord("j")):
            self.cat_idx = (self.cat_idx + 1) % len(self.categories)
            self.opt_idx = self.opt_scroll = self.doc_scroll = 0
        elif ch in (c.KEY_RIGHT, ord("l"), ord("\n"), c.KEY_ENTER, 10, 13):
            self.focus = "opts"
        return True

    def _handle_opt_key(self, ch) -> bool:
        c = self.curses
        names = self.current_names()
        if ch in (c.KEY_UP, ord("k")):
            self.opt_idx = (self.opt_idx - 1) % max(1, len(names)); self.doc_scroll = 0
        elif ch in (c.KEY_DOWN, ord("j")):
            self.opt_idx = (self.opt_idx + 1) % max(1, len(names)); self.doc_scroll = 0
        elif ch in (c.KEY_NPAGE,):
            self.opt_idx = min(len(names) - 1, self.opt_idx + 10); self.doc_scroll = 0
        elif ch in (c.KEY_PPAGE,):
            self.opt_idx = max(0, self.opt_idx - 10); self.doc_scroll = 0
        elif ch in (c.KEY_LEFT, ord("h")):
            self.focus = "cats"
        elif ch in (ord("u"), ord("U")):
            self._reset_current()
        elif ch in (ord("\n"), c.KEY_ENTER, 10, 13, c.KEY_RIGHT, ord("l")):
            self.edit_current()
        return True

    # ---- search --------------------------------------------------------- #
    def _enter_search(self):
        self.search = ""
        self.search_mode = True
        self._search_results = sorted(
            n for n, o in self.sess.schema.items() if platform_visible(o))
        self.opt_idx = self.opt_scroll = 0

    def _handle_search_key(self, ch) -> bool:
        c = self.curses
        if ch in (27,):  # Esc
            self.search_mode = False
            self.focus = "opts"
            self.opt_idx = self.opt_scroll = 0
            return True
        if ch in (c.KEY_UP,):
            self.opt_idx = max(0, self.opt_idx - 1); self.doc_scroll = 0; return True
        if ch in (c.KEY_DOWN,):
            self.opt_idx = min(len(self._search_results) - 1, self.opt_idx + 1); self.doc_scroll = 0; return True
        if ch in (c.KEY_NPAGE,):
            self.opt_idx = min(len(self._search_results) - 1, self.opt_idx + 10); return True
        if ch in (c.KEY_PPAGE,):
            self.opt_idx = max(0, self.opt_idx - 10); return True
        if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
            if self._search_results:
                self.edit_current()
            return True
        if ch in (c.KEY_BACKSPACE, 127, 8):
            self.search = self.search[:-1]
        elif 32 <= ch < 127:
            self.search += chr(ch)
        else:
            return True
        q = self.search.lower()
        self._search_results = sorted(
            n for n, o in self.sess.schema.items()
            if platform_visible(o) and (q in n.lower() or q in o.doc.lower())
        )
        self.opt_idx = self.opt_scroll = 0
        self._msg(f"search: {self.search}   ({len(self._search_results)} matches)", "info")
        return True

    # ---- editing dispatch ---------------------------------------------- #
    def edit_current(self):
        opt = self.current_option()
        if not opt:
            return
        if opt.kind == "bool":
            self._edit_bool(opt)
        elif opt.kind == "enum":
            self._edit_enum(opt)
        elif opt.kind == "theme":
            self._edit_theme(opt)
        elif opt.kind == "font":
            self._edit_font(opt)
        elif opt.kind in ("int", "float"):
            rng = slider_range(opt)
            if rng:
                self._edit_slider(opt, *rng)
            else:
                self._edit_number(opt)
        elif opt.kind in ("list", "keybind", "palette"):
            self._edit_list(opt)
        else:  # color, text
            self._edit_text(opt)

    def _commit_scalar(self, opt: Option, value: str, preview=False):
        """Stage a scalar; apply live if auto-apply. Returns (ok, errs)."""
        snap = list(self.sess.cfg.lines)
        self.sess.cfg.set_scalar(opt.name, value)
        if not self.sess.auto_apply:
            self.sess.dirty = True
            return True, []
        text = self.sess.cfg.render()
        ok, errs = validate(text)
        if not ok:
            self.sess.cfg.lines = snap  # roll back invalid change
            return False, errs
        self.sess.ensure_backup()
        self.sess.cfg.write(text)
        self.sess.dirty = False
        reload_ghostty()
        return True, []

    def _commit_list(self, opt: Option, values: list[str]):
        snap = list(self.sess.cfg.lines)
        self.sess.cfg.set_list(opt.name, values)
        if not self.sess.auto_apply:
            self.sess.dirty = True
            return True, []
        text = self.sess.cfg.render()
        ok, errs = validate(text)
        if not ok:
            self.sess.cfg.lines = snap
            return False, errs
        self.sess.ensure_backup()
        self.sess.cfg.write(text)
        self.sess.dirty = False
        reload_ghostty()
        return True, []

    def _reset_current(self):
        opt = self.current_option()
        if not opt or not self.sess.is_overridden(opt.name):
            self._msg("already at default", "info")
            return
        snap = list(self.sess.cfg.lines)
        self.sess.cfg.unset(opt.name)
        if self.sess.auto_apply:
            text = self.sess.cfg.render()
            ok, errs = validate(text)
            if not ok:
                self.sess.cfg.lines = snap
                self._msg("reset failed: " + (errs[0] if errs else "?"), "error")
                return
            self.sess.ensure_backup()
            self.sess.cfg.write(text)
            reload_ghostty()
        self.sess.dirty = True if not self.sess.auto_apply else False
        self._msg(f"{opt.name} reset to default ({opt.default or 'empty'})", "ok")

    def _edit_bool(self, opt: Option):
        cur = self.sess.effective(opt.name)
        new = "false" if cur == "true" else "true"
        ok, errs = self._commit_scalar(opt, new)
        if ok:
            self._msg(f"{opt.name} = {new}" + (" (live)" if self.sess.auto_apply else " (staged)"), "ok")
        else:
            self._msg("invalid: " + (errs[0] if errs else "?"), "error")

    # ---- restore + status helpers -------------------------------------- #
    def _snap(self) -> list[str]:
        return list(self.sess.cfg.lines)

    def _restore(self, snap: list[str]) -> None:
        """Undo any (previewed) edits back to `snap`, re-applying live."""
        self.sess.cfg.lines = snap
        if self.sess.auto_apply:
            self.sess.cfg.write()
            reload_ghostty()
        self.sess.dirty = (self.sess.cfg.render().rstrip("\n")
                           != self.sess.original_text.rstrip("\n"))

    def _report(self, opt: Option, value: str, ok: bool, errs) -> None:
        if ok:
            tag = " (live)" if self.sess.auto_apply else " (staged)"
            self._msg(f"{opt.name} = {value}{tag}", "ok")
        else:
            self._msg("invalid: " + (errs[0] if errs else "?"), "error")

    def _edit_enum(self, opt: Option):
        snap = self._snap()
        cur = self.sess.effective(opt.name)
        choice = self._picker(opt.name, opt.values, cur,
                              preview=lambda v: self._commit_scalar(opt, v, preview=True))
        if choice is None:
            self._restore(snap); self._msg("cancelled", "info"); return
        ok, errs = self._commit_scalar(opt, choice)
        self._report(opt, choice, ok, errs)

    def _edit_theme(self, opt: Option):
        self._msg("loading themes…", "info"); self.draw()
        themes = list_themes()
        if not themes:
            self._msg("no themes found", "warn"); return
        snap = self._snap()
        cur = self.sess.effective(opt.name)
        choice = self._picker("theme", themes, cur,
                              preview=lambda v: self._commit_scalar(opt, v, preview=True),
                              side=lambda item, sx, sy, sw: self._draw_theme_card(item, sx, sy, sw))
        if choice is None:
            self._restore(snap); self._msg("cancelled", "info"); return
        ok, errs = self._commit_scalar(opt, choice)
        self._report(opt, choice, ok, errs)

    def _edit_font(self, opt: Option):
        self._msg("loading fonts…", "info"); self.draw()
        fonts = list_fonts()
        if not fonts:
            self._edit_text(opt); return
        snap = self._snap()
        curlist = self.sess.effective_list(opt.name)
        cur = curlist[0] if curlist else ""
        choice = self._picker(opt.name + " (primary)", fonts, cur,
                              preview=lambda v: self._commit_list(opt, [v]))
        if choice is None:
            self._restore(snap); self._msg("cancelled", "info"); return
        ok, errs = self._commit_list(opt, [choice])
        self._report(opt, choice, ok, errs)

    def _edit_number(self, opt: Option):
        snap = self._snap()
        cur = self.sess.effective(opt.name)
        is_float = opt.kind == "float"
        step = 0.05 if is_float else 1
        try:
            val = float(cur) if cur not in ("", None) else 0.0
        except ValueError:
            val = 0.0
        buf = cur
        c = self.curses
        c.curs_set(1)
        try:
            while True:
                self._prompt_bar(f"{opt.name} = ", buf,
                                 "↑↓ / +- step · type value · Enter apply · Esc cancel")
                ch = self.scr.getch()
                if ch in (27,):
                    self._restore(snap); self._msg("cancelled", "info"); return
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    ok, errs = self._commit_scalar(opt, buf.strip())
                    if not ok:
                        self._restore(snap)
                    self._report(opt, buf.strip(), ok, errs)
                    return
                if ch in (c.KEY_UP, ord("+"), ord("=")):
                    val = self._num(buf, val) + step
                    buf = self._fmt_num(val, is_float)
                    self._commit_scalar(opt, buf, preview=True)
                elif ch in (c.KEY_DOWN, ord("-"), ord("_")):
                    val = self._num(buf, val) - step
                    buf = self._fmt_num(val, is_float)
                    self._commit_scalar(opt, buf, preview=True)
                elif ch in (c.KEY_BACKSPACE, 127, 8):
                    buf = buf[:-1]
                elif 32 <= ch < 127 and chr(ch) in "0123456789.-":
                    buf += chr(ch)
        finally:
            c.curs_set(0)

    def _num(self, buf, fallback):
        try:
            return float(buf)
        except ValueError:
            return fallback

    def _fmt_num(self, val, is_float):
        if is_float:
            s = f"{val:.2f}"
            return s.rstrip("0").rstrip(".") if "." in s else s
        return str(int(round(val)))

    def _edit_slider(self, opt: Option, lo: float, hi: float, step: float):
        """Visual slider for a bounded numeric option. Left/right (or -/+, h/l,
        j/k) nudge by one step, PgUp/PgDn by ten, Home/End jump to the ends.
        Every change previews live; Esc rolls back to where we started."""
        c = self.curses
        snap = self._snap()
        is_float = opt.kind == "float" or step < 1
        nsteps = max(1, int(round((hi - lo) / step)))
        # Track position as an integer step index to avoid float drift.
        cur = self.sess.effective(opt.name)
        try:
            start = float(cur) if cur not in ("", None) else lo
        except ValueError:
            start = lo
        idx = max(0, min(nsteps, int(round((start - lo) / step))))

        def value(i):
            return hi if i >= nsteps else lo + i * step

        pending = True   # preview on entry so the live value matches the knob
        while True:
            self._draw_slider(opt, lo, hi, value(idx), is_float, idx / nsteps)
            if pending:
                self._commit_scalar(opt, self._fmt_num(value(idx), is_float),
                                    preview=True)
                pending = False
            ch = self.scr.getch()
            if ch in (27,):
                self._restore(snap); self._msg("cancelled", "info"); return
            if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                v = self._fmt_num(value(idx), is_float)
                ok, errs = self._commit_scalar(opt, v)
                if not ok:
                    self._restore(snap)
                self._report(opt, v, ok, errs)
                return
            new = idx
            if ch in (c.KEY_LEFT, c.KEY_DOWN, ord("-"), ord("_"), ord("h"), ord("j")):
                new = idx - 1
            elif ch in (c.KEY_RIGHT, c.KEY_UP, ord("+"), ord("="), ord("l"), ord("k")):
                new = idx + 1
            elif ch in (c.KEY_NPAGE,):
                new = idx - 10
            elif ch in (c.KEY_PPAGE,):
                new = idx + 10
            elif ch in (c.KEY_HOME,):
                new = 0
            elif ch in (c.KEY_END,):
                new = nsteps
            new = max(0, min(nsteps, new))
            if new != idx:
                idx = new
                pending = True

    def _draw_slider(self, opt: Option, lo, hi, val, is_float, frac):
        c = self.curses
        self.scr.erase()
        h, w = self.dims()
        self.safe(0, 0, f" set · {opt.name} ".ljust(w), c.color_pair(1) | c.A_BOLD)
        row = 2
        for dl in opt.doc.split("\n")[:3]:
            self.safe(row, 2, dl[:w - 3], c.color_pair(4)); row += 1

        lo_s, hi_s, val_s = (self._fmt_num(lo, is_float),
                             self._fmt_num(hi, is_float),
                             self._fmt_num(val, is_float))
        bar_w = max(10, min(48, w - (len(lo_s) + len(hi_s) + 8)))
        knob = max(0, min(bar_w - 1, int(round(frac * (bar_w - 1)))))
        y = max(row + 2, h // 2 - 1)
        x = max(2, (w - (bar_w + len(lo_s) + len(hi_s) + 4)) // 2)

        self.safe(y, x, lo_s + " ", c.color_pair(4))
        bx = x + len(lo_s) + 1
        self.safe(y, bx, "━" * knob, c.color_pair(5) | c.A_BOLD)
        self.safe(y, bx + knob, "●", c.color_pair(6) | c.A_BOLD)
        self.safe(y, bx + knob + 1, "─" * (bar_w - knob - 1), c.color_pair(4))
        self.safe(y, bx + bar_w + 1, " " + hi_s, c.color_pair(4))

        vtxt = f"  {val_s}  "
        self.safe(y + 2, max(2, (w - len(vtxt)) // 2), vtxt, c.color_pair(10) | c.A_BOLD)
        default_line = f"default: {opt.default or '(empty)'}"
        self.safe(y + 3, max(2, (w - len(default_line)) // 2), default_line, c.color_pair(4))
        self.safe(h - 1, 0,
                  " ←/→ adjust · PgUp/PgDn ×10 · Home/End min/max · Enter apply · Esc cancel ".ljust(w),
                  c.color_pair(1))
        self.scr.refresh()

    def _edit_text(self, opt: Option):
        snap = self._snap()
        cur = self.sess.effective(opt.name)
        new = self._line_editor(
            f"{opt.name} = ", cur,
            live=(lambda v: self._commit_scalar(opt, v, preview=True))
            if opt.kind == "color" else None)
        if new is None:
            self._restore(snap); self._msg("cancelled", "info"); return
        ok, errs = self._commit_scalar(opt, new.strip())
        if not ok:
            self._restore(snap)
        self._report(opt, new.strip(), ok, errs)

    # ---- list editor ---------------------------------------------------- #
    def _edit_list(self, opt: Option):
        c = self.curses
        values = self.sess.effective_list(opt.name)[:]
        sel = 0
        hint_add = {"keybind": "trigger=action e.g. cmd+k=clear_screen",
                    "palette": "index=color e.g. 4=#89b4fa",
                    "env": "NAME=value"}.get(opt.name, opt.name + " entry")
        while True:
            self.scr.erase()
            h, w = self.dims()
            self.safe(0, 0, f" edit list · {opt.name} ".ljust(w), c.color_pair(1) | c.A_BOLD)
            self.safe(1, 0, opt.doc.split(chr(10))[0][:w], c.color_pair(4))
            top = 3
            for i, v in enumerate(values):
                y = top + i
                if y >= h - 2:
                    break
                attr = c.color_pair(3) | c.A_BOLD if i == sel else c.A_NORMAL
                self.safe(y, 2, f"{i+1:2}. ", attr)
                self._draw_value_line(y, 7, w - 8, opt, v, bold=(i == sel))
            if not values:
                self.safe(top, 2, "(empty — press 'a' to add)", c.color_pair(4))
            self.safe(h - 1, 0, " a add · e edit · d delete · Enter save · Esc cancel ".ljust(w),
                      c.color_pair(1))
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (27,):
                # nothing is persisted until Enter, so cancelling is a no-op
                self._msg("cancelled", "info"); return
            if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                ok, errs = self._commit_list(opt, values)
                self._msg(f"{opt.name}: {len(values)} entries" + (" (live)" if self.sess.auto_apply else " (staged)")
                          if ok else "invalid: " + (errs[0] if errs else "?"),
                          "ok" if ok else "error")
                return
            if ch in (c.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (c.KEY_DOWN, ord("j")):
                sel = min(len(values) - 1, sel + 1) if values else 0
            elif ch in (ord("a"),):
                nv = (self._edit_keybind_form() if opt.name == "keybind"
                      else self._line_editor("add › ", "", hint=hint_add))
                if nv:
                    values.append(nv.strip()); sel = len(values) - 1
            elif ch in (ord("e"),) and values:
                nv = (self._edit_keybind_form(values[sel]) if opt.name == "keybind"
                      else self._line_editor("edit › ", values[sel], hint=hint_add))
                if nv is not None:
                    values[sel] = nv.strip()
            elif ch in (ord("d"), c.KEY_DC) and values:
                del values[sel]
                sel = max(0, min(sel, len(values) - 1))

    # ---- keybind builder ------------------------------------------------ #
    def _assemble_keybind(self, state) -> str:
        mods = [m for m in KEYBIND_MODS if state["mods"][m]]
        trigger = "+".join(mods + ([state["key"]] if state["key"] else []))
        action = state["action"]
        if action and state["args"].strip():
            action = f"{action}:{state['args'].strip()}"
        return f"{trigger}={action}"

    def _parse_keybind_into(self, initial: str, state) -> None:
        trig, _, act = initial.partition("=")
        parts = [p for p in trig.split("+") if p]
        if parts:
            state["key"] = parts[-1].strip()
            for m in parts[:-1]:
                mm = KEYBIND_MOD_ALIASES.get(m.strip().lower(), m.strip().lower())
                if mm in state["mods"]:
                    state["mods"][mm] = True
        action, _, args = act.strip().partition(":")
        state["action"] = action.strip()
        state["args"] = args.strip()

    def _validate_keybind(self, binding: str) -> bool:
        ok, _ = validate(f"keybind = {binding}\n")
        return ok

    def _edit_keybind_form(self, initial: str | None = None) -> str | None:
        """Guided keybind builder: toggle modifiers, capture/pick a key, choose
        an action from Ghostty's own list. Cross-platform — terminals can't
        report ⌘/Super as a keypress, so modifiers are explicit toggles rather
        than captured. Returns a validated `trigger=action` string, or None."""
        c = self.curses
        state = {"mods": {m: False for m in KEYBIND_MODS},
                 "key": "", "action": "", "args": ""}
        if initial:
            self._parse_keybind_into(initial, state)
        row, modsel, error = 0, 0, ""
        NROWS = 5   # 0 mods, 1 key, 2 action, 3 args, 4 save
        while True:
            self._draw_keybind_form(state, row, modsel, error)
            ch = self.scr.getch()
            if ch in (27,):
                return None
            if ch in (c.KEY_DOWN, ord("\t")):
                row = (row + 1) % NROWS; continue
            if ch in (c.KEY_UP, c.KEY_BTAB, 353):
                row = (row - 1) % NROWS; continue
            error = ""
            if row == 0:                       # modifiers
                if ch in (c.KEY_LEFT,):
                    modsel = (modsel - 1) % len(KEYBIND_MODS)
                elif ch in (c.KEY_RIGHT,):
                    modsel = (modsel + 1) % len(KEYBIND_MODS)
                elif ch in (ord(" "),):
                    m = KEYBIND_MODS[modsel]; state["mods"][m] = not state["mods"][m]
            elif row == 1:                     # key
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    pick = self._picker("special key", KEYBIND_NAMED_KEYS, state["key"])
                    if pick:
                        state["key"] = pick
                elif ch in (c.KEY_BACKSPACE, 127, 8, c.KEY_DC):
                    state["key"] = ""
                elif 32 < ch < 127:            # printable (not space) -> capture it
                    k = chr(ch)
                    state["key"] = k.lower() if k.isalpha() else k
            elif row == 2:                     # action
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    acts = list_actions()
                    if acts:
                        pick = self._picker("action", acts, state["action"])
                        if pick:
                            state["action"] = pick
                    else:
                        error = "could not load Ghostty actions"
            elif row == 3:                     # optional args
                if ch in (c.KEY_BACKSPACE, 127, 8):
                    state["args"] = state["args"][:-1]
                elif ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    row = 4
                elif 32 <= ch < 127:
                    state["args"] += chr(ch)
            elif row == 4:                     # save
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    if not state["key"]:
                        error, row = "pick a key first", 1; continue
                    if not state["action"]:
                        error, row = "pick an action first", 2; continue
                    result = self._assemble_keybind(state)
                    if not self._validate_keybind(result):
                        error = f"Ghostty rejected: {result}"; continue
                    return result

    def _draw_keybind_form(self, state, row, modsel, error):
        c = self.curses
        self.scr.erase()
        h, w = self.dims()
        self.safe(0, 0, " build keybind ".ljust(w), c.color_pair(1) | c.A_BOLD)
        y = 2
        self.safe(y, 2, "Modifiers:", c.color_pair(4))
        x = 14
        for i, m in enumerate(KEYBIND_MODS):
            box = "[x]" if state["mods"][m] else "[ ]"
            label = f"{box} {m}"
            focused = (row == 0 and i == modsel)
            self.safe(y, x, label, c.color_pair(3) | c.A_BOLD if focused else c.color_pair(5))
            x += len(label) + 2
        if IS_MACOS:
            self.safe(y + 1, 14, "(super = ⌘ Command on macOS)", c.color_pair(4))
        y += 3

        def field(label, val, r, hint):
            focused = (row == r)
            self.safe(y, 2, f"{label:9}", c.color_pair(4))
            self.safe(y, 12, (val or "—")[:24],
                      c.color_pair(3) | c.A_BOLD if focused else c.color_pair(5))
            self.safe(y, 40, hint, c.color_pair(4))

        field("Key:", state["key"], 1, "type a key · Enter → named-key list"); y += 1
        field("Action:", state["action"], 2, "Enter → choose from Ghostty actions"); y += 1
        field("Args:", state["args"], 3, "optional, e.g. 1  or  mixed"); y += 2

        result = self._assemble_keybind(state)
        self.safe(y, 2, "Result: ", c.color_pair(4))
        self.safe(y, 10, result, c.color_pair(6) | c.A_BOLD); y += 2
        self.safe(y, 2, "▸ Save this binding",
                  c.color_pair(3) | c.A_BOLD if row == 4 else c.color_pair(6)); y += 1
        if error:
            self.safe(y + 1, 2, "⚠ " + error, c.color_pair(7) | c.A_BOLD)
        self.safe(h - 1, 0,
                  " Tab/↑↓ field · ←/→ pick mod · Space toggle · Enter pick/save · Esc cancel ".ljust(w),
                  c.color_pair(1))
        self.scr.refresh()

    # ---- primitive input widgets --------------------------------------- #
    def _prompt_bar(self, label, buf, hint):
        c = self.curses
        h, w = self.dims()
        self.safe(h - 2, 0, " " * (w - 1), 0)
        self.safe(h - 2, 0, label + buf, c.color_pair(5) | c.A_BOLD)
        self.safe(h - 1, 0, (" " + hint).ljust(w), c.color_pair(1))
        try:
            self.scr.move(h - 2, min(w - 1, len(label) + len(buf)))
        except self.curses.error:
            pass
        self.scr.refresh()

    def _line_editor(self, label, initial, hint="Enter apply · Esc cancel", live=None):
        c = self.curses
        buf = initial or ""
        c.curs_set(1)
        try:
            while True:
                self._prompt_bar(label, buf, hint)
                ch = self.scr.getch()
                if ch in (27,):
                    return None
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    return buf
                if ch in (c.KEY_BACKSPACE, 127, 8):
                    buf = buf[:-1]
                elif ch in (c.KEY_DC,):
                    pass
                elif 32 <= ch < 127:
                    buf += chr(ch)
                else:
                    continue
                if live:
                    live(buf)
        finally:
            c.curs_set(0)

    def _picker(self, title, items, current, preview=None, side=None):
        """Scrollable, type-to-filter picker. Live-previews the highlighted
        item (debounced) when auto-apply is on. `side`, if given, is a callback
        (item, x, y, width) that draws a panel to the right of the list.
        Returns choice or None."""
        c = self.curses
        query = ""
        filtered = list(items)
        sel = 0
        if current in filtered:
            sel = filtered.index(current)
        last_preview = None
        last_move = time.monotonic()
        self.scr.timeout(90)
        try:
            while True:
                # debounced live preview
                if preview and self.sess.auto_apply and filtered:
                    want = filtered[sel]
                    if want != last_preview and (time.monotonic() - last_move) > 0.11:
                        preview(want)
                        last_preview = want
                self._draw_picker(title, query, filtered, sel, current, side)
                ch = self.scr.getch()
                if ch == -1:
                    continue
                last_move = time.monotonic()
                if ch in (27,):
                    return None
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    return filtered[sel] if filtered else None
                if ch in (c.KEY_UP,):
                    sel = max(0, sel - 1)
                elif ch in (c.KEY_DOWN,):
                    sel = min(len(filtered) - 1, sel + 1) if filtered else 0
                elif ch in (c.KEY_NPAGE,):
                    sel = min(len(filtered) - 1, sel + 10) if filtered else 0
                elif ch in (c.KEY_PPAGE,):
                    sel = max(0, sel - 10)
                elif ch in (c.KEY_BACKSPACE, 127, 8):
                    query = query[:-1]
                    filtered = [x for x in items if query.lower() in x.lower()]
                    sel = 0
                elif 32 <= ch < 127:
                    query += chr(ch)
                    filtered = [x for x in items if query.lower() in x.lower()]
                    sel = 0
        finally:
            self.scr.timeout(-1)

    def _draw_picker(self, title, query, filtered, sel, current, side=None):
        c = self.curses
        self.scr.erase()
        h, w = self.dims()
        self.safe(0, 0, f" select {title}  ({len(filtered)}) ".ljust(w), c.color_pair(1) | c.A_BOLD)
        self.safe(1, 0, f" filter: {query}", c.color_pair(5) | c.A_BOLD)
        top = 3
        rows = h - 5
        # reserve a right-hand panel for `side` when the screen is wide enough
        side_x = None
        list_w = w
        if side and w >= 56:
            list_w = w // 2
            side_x = list_w + 2
        scroll = max(0, sel - rows + 1)
        for r in range(rows):
            i = scroll + r
            if i >= len(filtered):
                break
            item = filtered[i]
            y = top + r
            is_sel = i == sel
            attr = c.color_pair(3) | c.A_BOLD if is_sel else c.A_NORMAL
            marker = "→ " if is_sel else ("• " if item == current else "  ")
            rgb = parse_hex(item.split("=")[-1] if "=" in item else item)
            x = 2
            self.safe(y, x, marker, attr)
            if rgb:
                pair = self.swatch_pair(item.split("=")[-1] if "=" in item else item)
                self.safe(y, x + 2, "██ ", c.color_pair(pair) if pair else attr)
                self.safe(y, x + 5, item[: list_w - x - 6], attr)
            else:
                self.safe(y, x + 2, item[: list_w - x - 3], attr)
        if side_x is not None and filtered:
            side(filtered[sel], side_x, top, w - side_x - 1)
        hint = " type to filter · ↑↓ move · Enter select · Esc cancel "
        if self.sess.auto_apply:
            hint = " ● LIVE PREVIEW ·" + hint
        self.safe(h - 1, 0, hint.ljust(w), c.color_pair(1))
        self.scr.refresh()

    def _draw_theme_card(self, name, x, y, width):
        """Right-hand panel in the theme picker: the highlighted theme's colours."""
        c = self.curses
        self.safe(y, x, name[:width], c.color_pair(10) | c.A_BOLD)
        colors = self._effective_colors(theme_override=name)
        used = self._draw_color_preview(y + 2, x, width, colors)
        if not used:
            hint = ("(256-colour terminal needed for preview)"
                    if c.COLORS < 256 else "(no colour data for this theme)")
            self.safe(y + 2, x, hint[:width], c.color_pair(4))
            return
        yy = y + 2 + used + 1
        self.safe(yy, x, f"bg {colors['bg']}   fg {colors['fg']}"[:width], c.color_pair(4))

    # ---- overlays ------------------------------------------------------- #
    def _changes_overlay(self):
        c = self.curses
        ovr = self.sess.overrides()
        self.scr.erase()
        h, w = self.dims()
        self.safe(0, 0, f" changed from default · {len(ovr)} option(s) ".ljust(w),
                  c.color_pair(1) | c.A_BOLD)
        y = 2
        if not ovr:
            self.safe(2, 2, "nothing changed — all defaults", c.color_pair(4))
        for name, val in ovr:
            if y >= h - 2:
                self.safe(h - 2, 2, "…", c.color_pair(4)); break
            self.safe(y, 2, name, c.color_pair(10) | c.A_BOLD)
            self.safe(y, 34, ("= " + val)[: w - 36], c.color_pair(5))
            y += 1
        self.safe(h - 1, 0, f" config: {self.sess.cfg.path}  ·  any key to close ".ljust(w),
                  c.color_pair(1))
        self.scr.refresh()
        self.scr.getch()

    def _help(self):
        c = self.curses
        info = self._update_info
        update_line = (
            f"A newer release ({info['latest']}) is available at {info['url']}"
            if info and info.get("outdated")
            else f"You're on the latest version (v{__version__})."
        )
        lines = [
            f"SpookiUI v{__version__} — live Ghostty configurator",
            "",
            "Navigation",
            "  ↑/↓ or j/k    move            Tab      switch pane",
            "  →/Enter/l     into options / edit an option",
            "  ←/h           back to categories",
            "  PgUp/PgDn     jump by 10",
            "  /             search all options by name or docs",
            "",
            "Editing (changes apply LIVE when auto-apply is on)",
            "  Enter         edit the selected option",
            "   • booleans toggle instantly",
            "   • enums/theme/font open a picker with live preview",
            "   • theme picker shows a live colour card for each theme",
            "   • numbers: ↑↓ or +/- to step, or type a value",
            "   • bounded values (opacity, contrast): a slider — ←/→ to adjust",
            "   • colors/text: type a value (#hex or name); colours preview",
            "   • lists (palette/env): a add, e edit, d delete",
            "   • keybind: a/e open a builder — toggle modifiers, pick an action",
            "  u             reset the selected option to its default",
            "",
            "Session",
            "  a   toggle auto-apply (live vs. staged)",
            "  s   save + reload now      r   re-trigger reload",
            "  R   revert everything to session start",
            "  X   wipe config & restore all Ghostty defaults (backup kept)",
            "  d   show what you've changed",
            "  q   quit",
            "",
            "Options that only apply to the other OS are hidden automatically.",
            "",
            "Live reload works by clicking Ghostty's 'Reload Configuration'",
            "menu item on macOS, or sending it SIGUSR2 on Linux. A timestamped",
            "backup of your config is made on the first change of each session.",
            "",
            update_line,
        ]
        self.scr.erase()
        h, w = self.dims()
        self.safe(0, 0, " help ".ljust(w), c.color_pair(1) | c.A_BOLD)
        for i, ln in enumerate(lines):
            if i + 2 >= h - 1:
                break
            attr = c.color_pair(5) | c.A_BOLD if ln and not ln.startswith(" ") and ":" not in ln and ln[0].isupper() and "—" not in ln else c.color_pair(4)
            self.safe(i + 2, 2, ln[: w - 3], attr)
        self.safe(h - 1, 0, " any key to close ".ljust(w), c.color_pair(1))
        self.scr.refresh()
        self.scr.getch()

    def _confirm(self, question) -> bool:
        c = self.curses
        h, w = self.dims()
        self.safe(h - 2, 0, (" " + question + "  [y/N] ").ljust(w), c.color_pair(8) | c.A_BOLD)
        self.safe(h - 1, 0, " ".ljust(w), c.color_pair(1))
        self.scr.refresh()
        ch = self.scr.getch()
        return ch in (ord("y"), ord("Y"))

    def _quit(self) -> bool:
        if self.sess.dirty and not self.sess.auto_apply:
            if self._confirm("You have unsaved changes. Save before quitting?"):
                ok, m = self.sess.apply()
                self._msg(m, "ok" if ok else "error")
        return False

    def _msg(self, text, kind="info"):
        self.status = text
        self.status_kind = kind


# --------------------------------------------------------------------------- #
#  Non-interactive CLI
# --------------------------------------------------------------------------- #

def cli_list(sess: Session, args) -> int:
    cats = CATEGORY_ORDER if not args.category else [args.category]
    show_all = getattr(args, "all", False)
    for cat in cats:
        names = sorted(n for n, o in sess.schema.items() if o.category == cat
                       and (show_all or platform_visible(o)))
        if not names:
            continue
        print(f"\n== {cat} ==")
        for n in names:
            o = sess.schema[n]
            mark = "*" if sess.is_overridden(n) else " "
            val = ", ".join(sess.effective_list(n)) if o.is_list else sess.effective(n)
            print(f" {mark} {n:34} {o.kind:7} {val}")
    return 0


def cli_get(sess: Session, args) -> int:
    o = sess.schema.get(args.key)
    if not o:
        print(f"unknown option: {args.key}", file=sys.stderr)
        return 2
    if o.is_list:
        for v in sess.effective_list(args.key):
            print(v)
    else:
        print(sess.effective(args.key))
    return 0


def cli_doc(sess: Session, args) -> int:
    o = sess.schema.get(args.key)
    if not o:
        print(f"unknown option: {args.key}", file=sys.stderr)
        return 2
    print(f"{o.name}  (type: {o.kind}{', list' if o.is_list else ''})")
    print(f"default: {o.default or '(empty)'}")
    if o.values:
        print("choices: " + ", ".join(o.values))
    if o.reload_note:
        print("note: " + o.reload_note)
    print()
    print(o.doc or "(no documentation)")
    return 0


def cli_set(sess: Session, args) -> int:
    o = sess.schema.get(args.key)
    if not o:
        print(f"unknown option: {args.key}", file=sys.stderr)
        return 2
    snap = list(sess.cfg.lines)
    if o.is_list:
        sess.stage_list(args.key, list(args.value))
    else:
        sess.stage_scalar(args.key, args.value[0])

    # always validate before writing, reload or not
    ok, errs = validate(sess.cfg.render())
    if not ok:
        sess.cfg.lines = snap
        print("invalid: " + (errs[0] if errs else "validation failed"), file=sys.stderr)
        return 1
    sess.ensure_backup()
    sess.cfg.write()
    if args.no_reload:
        print(f"{args.key} set · saved (no reload)")
        return 0
    rok, m = reload_ghostty()
    print(f"{args.key} set · saved · reload: {m}")
    return 0 if rok else 0


def cli_version(sess: Session, args) -> int:
    print(f"SpookiUI v{__version__}")
    if args.no_check:
        return 0
    info = check_for_update(force=True)
    if info is None:
        print("update check: no published release found (or GitHub unreachable)",
              file=sys.stderr)
        return 0
    if info["outdated"]:
        print(f"a newer release is available: {info['latest']}")
        print(f"  {info['url']}")
    else:
        print("you're on the latest release")
    return 0


def cli_reset(sess: Session, args) -> int:
    if not args.yes:
        print("This clears your config file and restores every Ghostty default.\n"
              f"A dated backup of {sess.cfg.path} is kept.\n"
              "Re-run with --yes to proceed.", file=sys.stderr)
        return 1
    ok, m = sess.restore_defaults()
    print(m)
    return 0 if ok else 1


def cli_reload(sess: Session, args) -> int:
    ok, m = reload_ghostty()
    print(m)
    return 0 if ok else 1


def cli_validate(sess: Session, args) -> int:
    ok, errs = validate(sess.cfg.render())
    if ok:
        print("config is valid")
        return 0
    for e in errs:
        print(e, file=sys.stderr)
    return 1


def cli_themes(sess: Session, args) -> int:
    for t in list_themes():
        print(t)
    return 0


def cli_fonts(sess: Session, args) -> int:
    for f in list_fonts():
        print(f)
    return 0


def cli_path(sess: Session, args) -> int:
    print(sess.cfg.path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spookiui",
        description="Live configurator for the Ghostty terminal. Run with no "
                    "subcommand to launch the interactive TUI.")
    p.add_argument("-V", "--version", action="version",
                   version=f"SpookiUI v{__version__}")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("list", help="list options (optionally by category)")
    sp.add_argument("category", nargs="?", help="category name to filter by")
    sp.add_argument("--all", action="store_true",
                    help="include options for other operating systems")
    sp.set_defaults(func=cli_list)

    sp = sub.add_parser("get", help="print an option's current value")
    sp.add_argument("key")
    sp.set_defaults(func=cli_get)

    sp = sub.add_parser("doc", help="show docs for an option")
    sp.add_argument("key")
    sp.set_defaults(func=cli_doc)

    sp = sub.add_parser("set", help="set an option (writes + reloads live)")
    sp.add_argument("key")
    sp.add_argument("value", nargs="+", help="value(s); repeat for list options")
    sp.add_argument("--no-reload", action="store_true", help="write without live reload")
    sp.set_defaults(func=cli_set)

    sp = sub.add_parser("version", help="print the version and check GitHub for updates")
    sp.add_argument("--no-check", action="store_true", help="don't contact GitHub")
    sp.set_defaults(func=cli_version)

    sp = sub.add_parser("reset", help="restore Ghostty defaults (clears your config file)")
    sp.add_argument("--yes", action="store_true", help="confirm the reset (required)")
    sp.set_defaults(func=cli_reset)

    sp = sub.add_parser("reload", help="trigger Ghostty to reload its config")
    sp.set_defaults(func=cli_reload)

    sp = sub.add_parser("validate", help="validate the current config file")
    sp.set_defaults(func=cli_validate)

    sp = sub.add_parser("themes", help="list available themes")
    sp.set_defaults(func=cli_themes)

    sp = sub.add_parser("fonts", help="list available monospace font families")
    sp.set_defaults(func=cli_fonts)

    sp = sub.add_parser("path", help="print the config file path in use")
    sp.set_defaults(func=cli_path)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if GHOSTTY is None:
        print("error: could not find the `ghostty` executable.", file=sys.stderr)
        print("Install Ghostty or add it to your PATH.", file=sys.stderr)
        return 3

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        sess = Session()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 3

    if not getattr(args, "cmd", None):
        if not sys.stdout.isatty():
            print("Refusing to launch the TUI without a terminal. "
                  "Run `spookiui --help` for the scriptable CLI.", file=sys.stderr)
            return 1
        run_tui(sess)
        return 0
    return args.func(sess, args)


if __name__ == "__main__":
    sys.exit(main())
