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
`reset`, `version`, `update`, `profile`, `doctor`, `fix-ssh`, `treats`,
`reload`, `validate`, `themes`, `fonts`, `path`. Run `./spookiui.py --help`.

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

__version__ = "1.9.1"
GITHUB_REPO = "mattj85/SpookiUI"


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
CAN_RELOAD = IS_MACOS or IS_LINUX
INSIDE_GHOSTTY = os.environ.get("TERM_PROGRAM") == "ghostty"
CURRENT_PLATFORM = "macos" if IS_MACOS else ("linux" if IS_LINUX else None)


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )


UPDATE_CHECK_TTL = 24 * 60 * 60
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
                return None
            latest = cache.get("latest")
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


def self_path() -> str:
    return os.path.realpath(os.path.abspath(__file__))


def _git_checkout_root(path: str) -> str | None:
    """If `path` lives inside a git working tree, return that tree's root."""
    d = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _is_homebrew_install(path: str) -> bool:
    """Whether `path` is a Homebrew-managed copy (in a Cellar / brew prefix).
    Such installs must be updated with `brew upgrade`, not in place."""
    rp = os.path.realpath(path)
    if f"{os.sep}Cellar{os.sep}" in rp:
        return True
    prefix = os.environ.get("HOMEBREW_PREFIX")
    if prefix and rp.startswith(os.path.realpath(prefix) + os.sep):
        return True
    return False


def _download_release_source(tag: str, timeout: float = 20.0) -> str | None:
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{tag}/spookiui.py"
    req = urllib.request.Request(
        url, headers={"User-Agent": f"SpookiUI/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _verify_source(text: str) -> tuple[bool, str]:
    """Make sure the download really is a compilable SpookiUI before we trust it."""
    if "__version__" not in text or "def main(" not in text:
        return False, "downloaded file doesn't look like SpookiUI"
    import tempfile
    import py_compile
    fd, tmp = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError:
            return False, "downloaded file failed to compile — update aborted"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return True, ""


def _replace_self(path: str, text: str) -> tuple[bool, str]:
    """Atomically replace `path`, keeping a .prev backup. Returns (ok, info)."""
    d = os.path.dirname(path) or "."
    if not os.access(path, os.W_OK) or not os.access(d, os.W_OK):
        return False, f"no write permission for {path} — re-run with sudo or reinstall"
    import tempfile
    try:
        shutil.copy2(path, path + ".prev")
    except OSError:
        pass
    try:
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, os.stat(path).st_mode)
        os.replace(tmp, path)
    except OSError as e:
        return False, f"failed to write update: {e}"
    return True, path + ".prev"


def self_update() -> tuple[bool, str]:
    """Update this file in place to the latest release. Returns (ok, message)."""
    info = check_for_update(force=True)
    if info is None:
        return False, "could not reach GitHub to check for updates"
    if not info["outdated"]:
        return True, f"already up to date (v{__version__})"
    tag = info["latest"]
    path = self_path()

    if _is_homebrew_install(path):
        return False, (f"installed via Homebrew — run `brew upgrade spookiui` to "
                       f"get {tag}")

    repo = _git_checkout_root(path)
    if repo:
        proc = _run(["git", "-C", repo, "pull", "--ff-only"], timeout=60)
        if proc.returncode == 0:
            return True, f"updated to {tag} via git pull — restart SpookiUI to run it"
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        hint = detail[-1] if detail else "git pull failed"
        return False, (f"this is a git checkout; run `git pull` yourself in {repo}\n"
                       f"  ({hint})")

    text = _download_release_source(tag)
    if not text:
        return False, f"failed to download {tag} from GitHub"
    ok, msg = _verify_source(text)
    if not ok:
        return False, msg
    ok, res = _replace_self(path, text)
    if not ok:
        return False, res
    return True, (f"updated to {tag} — restart SpookiUI to run it "
                  f"(previous version saved as {os.path.basename(res)})")


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
    return candidates[0]


FORCE_LIST = {
    "font-family", "font-family-bold", "font-family-italic",
    "font-family-bold-italic", "font-feature", "font-variation",
    "font-variation-bold", "font-variation-italic", "font-variation-bold-italic",
    "font-codepoint-map", "keybind", "palette", "config-file",
    "config-default-files", "env", "clipboard-codepoint-map", "key-remap",
    "command-palette-entry", "link", "custom-shader",
}

COLOR_HINTS = (
    "background", "foreground", "cursor-color", "cursor-text", "bold-color",
    "split-divider-color", "unfocused-split-fill", "window-padding-color",
    "window-titlebar-background", "window-titlebar-foreground",
)

SLIDER_RANGES = {
    "minimum-contrast": (1.0, 21.0, 0.5),
    "bell-audio-volume": (0.0, 1.0, 0.05),
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
    defaults: list[str] = field(default_factory=list)
    doc: str = ""
    values: list[str] = field(default_factory=list)
    kind: str = "text"
    is_list: bool = False
    reload_note: str = ""
    category: str = "Advanced"
    platform: str | None = None

    @property
    def is_color(self) -> bool:
        return self.kind == "color"


_ENUM_RE = re.compile(r"\*\s+`([^`]+)`")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d*\.\d+$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _enum_values_from_doc(doc: str) -> list[str]:
    """Pull enum choices from a bulleted doc block. Ghostty sometimes packs
    several values onto one bullet (`macos-icon`: `blueprint`, `chalkboard`, …)
    and wraps the list onto continuation lines, so we gather backtick tokens
    from each bullet *and* its continuation lines — but only the part before the
    ` - `/` — ` description, so backticked terms in prose aren't mistaken for
    values."""
    vals: list[str] = []
    in_item = False
    for raw in doc.split("\n"):
        stripped = raw.strip()
        if stripped.startswith("*"):
            in_item = True
            content = stripped[1:]
        elif in_item and stripped.startswith("`"):
            content = stripped
        else:
            in_item = False
            continue
        left = re.split(r"\s[-—]\s", content, maxsplit=1)[0]
        vals.extend(_BACKTICK_RE.findall(left))
    return vals


def _classify(opt: Option) -> None:
    name, dflt = opt.name, opt.default
    doc_low = opt.doc.lower()

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

    cands = _enum_values_from_doc(opt.doc)
    cands = [c for c in cands if " " not in c and len(c) <= 32]
    if cands and dflt and dflt in cands:
        opt.kind = "enum"
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
        if not opt.values:
            opt.kind = "color"
            return

    opt.kind = "text"


CATEGORY_ORDER = [
    "Colors & Theme", "Font", "Cursor", "Window", "Spacing & Metrics",
    "Mouse", "Clipboard & Selection", "Quick Terminal", "Shell & Commands",
    "Keybindings", "macOS", "Linux / GTK", "Advanced",
]

# A synthetic left-pane category that isn't schema-backed: it lists one-shot
# maintenance actions (e.g. Fix SSH) instead of Ghostty options. Opening it
# (Enter/→) launches the Utils menu overlay.
UTILS_CATEGORY = "⚙ Utils"

# Another synthetic category: "Treats" are fun, purely-cosmetic animated
# background shaders (a Matrix rain, neon pipes, fireworks) that SpookiUI
# bundles and toggles via Ghostty's `custom-shader`. All default OFF, and only
# one runs at a time. Opening it (Enter/→) launches the Treats overlay. See the
# TREATS registry below.
TREATS_CATEGORY = "🍬 Treats"

# Nerd Font glyphs shown beside each root category when a Nerd Font is in use
# (see icons_available()). Codepoints are FontAwesome-range nf-fa-* icons; if a
# Nerd Font isn't the terminal font they'd render as tofu, so icons stay off.
CATEGORY_ICONS = {
    "Colors & Theme": "",       # paint brush
    "Font": "",                 # font
    "Cursor": "",               # i-cursor
    "Window": "",               # window
    "Spacing & Metrics": "",    # arrows
    "Mouse": "",                # mouse pointer
    "Clipboard & Selection": "",  # clipboard
    "Quick Terminal": "",       # terminal
    "Shell & Commands": "",     # code
    "Keybindings": "",          # keyboard
    "macOS": "",                # apple
    "Linux / GTK": "",          # linux
    "Advanced": "",             # cogs
    UTILS_CATEGORY: "",         # cog
    TREATS_CATEGORY: "",       # magic wand
}
DEFAULT_CATEGORY_ICON = ""      # folder


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


class ConfigFile:
    """A Ghostty config file that can be edited while preserving layout."""

    KEY_RE = re.compile(r"^(\s*)([a-z0-9][a-z0-9-]*)(\s*=\s*)(.*?)(\s*)$")
    MANAGED_HEADER = "# ─────────── added by SpookiUI ───────────"
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


def validate(text: str) -> tuple[bool, list[str]]:
    """Validate a full config file text. Returns (ok, error_lines)."""
    if not GHOSTTY:
        return True, []
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
    except Exception as e:
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
        except Exception:
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
    except Exception:
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
    seen, out = set(), []
    for f in fams:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


_ACTIONS_CACHE: list[str] | None = None
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*$")

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
        if not self.cfg.indices_of(name):
            return False
        opt = self.schema.get(name)
        if opt is None:
            return True
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

    def save_profile(self, name: str) -> tuple[bool, str]:
        """Snapshot the current config to a named profile."""
        name = name.strip()
        if not _PROFILE_NAME_RE.match(name):
            return False, "invalid name — use letters, numbers, . _ -"
        text = self.cfg.render()
        if not text.endswith("\n"):
            text += "\n"
        try:
            os.makedirs(profiles_dir(), exist_ok=True)
            with open(profile_path(name), "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as e:
            return False, f"could not save profile: {e}"
        return True, f"saved profile '{name}'"

    def load_profile(self, name: str) -> tuple[bool, str]:
        """Apply a named profile: validate, back up, write, reload, rollback on
        failure — the same discipline as every other mutation path."""
        path = profile_path(name)
        if not os.path.isfile(path):
            return False, f"no profile named '{name}'"
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            return False, f"could not read profile: {e}"
        ok, errs = validate(text)
        if not ok:
            return False, "profile invalid: " + (errs[0] if errs else "validation failed")
        self.ensure_backup()
        self.cfg.lines = text.split("\n")
        self.cfg.write(text)
        self.dirty = False
        if self.auto_apply and CAN_RELOAD:
            r_ok, msg = reload_ghostty()
            return True, f"loaded profile '{name}'" + ("" if r_ok else f" (reload: {msg})")
        return True, f"loaded profile '{name}'"

    def delete_profile(self, name: str) -> tuple[bool, str]:
        path = profile_path(name)
        if not os.path.isfile(path):
            return False, f"no profile named '{name}'"
        try:
            os.remove(path)
        except OSError as e:
            return False, f"could not delete profile: {e}"
        return True, f"deleted profile '{name}'"

    def toggle_light_dark(self) -> tuple[bool, str]:
        """Flip between the profiles named 'light' and 'dark'."""
        if not (os.path.isfile(profile_path("light"))
                and os.path.isfile(profile_path("dark"))):
            return False, "save profiles named 'light' and 'dark' first"
        try:
            with open(profile_path("dark"), encoding="utf-8") as fh:
                dark_text = fh.read().strip()
        except OSError:
            dark_text = None
        target = "light" if self.cfg.render().strip() == dark_text else "dark"
        return self.load_profile(target)


def spookiui_data_dir() -> str:
    """SpookiUI's own data dir (cross-platform), for profiles and small markers."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "spookiui")


def profiles_dir() -> str:
    """Where named config snapshots live, outside Ghostty's own config dir."""
    return os.path.join(spookiui_data_dir(), "profiles")


def icons_available(sess: "Session") -> bool:
    """Whether to show Nerd Font category icons. We can't ask the terminal if a
    glyph will render, so we key off the strongest signal we have: the terminal
    (Ghostty) font. `SPOOKIUI_ICONS=1/0` forces it on/off."""
    env = os.environ.get("SPOOKIUI_ICONS", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    for fam in sess.effective_list("font-family"):
        low = fam.lower()
        if "nerd font" in low or "nerdfont" in low:
            return True
    return False


def _icon_notice_marker() -> str:
    return os.path.join(spookiui_data_dir(), "icon-notice-shown")


def icon_notice_text() -> str:
    """Platform-specific guidance for enabling Nerd Font icons."""
    if IS_MACOS:
        install = ("  macOS:  brew install --cask font-symbols-only-nerd-font\n"
                   "          (or a full one, e.g. font-jetbrains-mono-nerd-font)")
    elif IS_LINUX:
        install = ("  Linux:  install a Nerd Font from https://www.nerdfonts.com\n"
                   "          (or your distro's package), then run `fc-cache -f`")
    else:
        install = "  Install a Nerd Font from https://www.nerdfonts.com"
    return (
        "SpookiUI can show an icon beside each category, but that needs a Nerd Font\n"
        "as your terminal font. None is set, so icons are off for now.\n\n"
        "To enable them:\n"
        f"{install}\n"
        "  Then set it in SpookiUI: open the \"Font\" category and set font-family\n"
        "  to your Nerd Font (e.g. \"JetBrainsMono Nerd Font\").\n"
        "  Or set SPOOKIUI_ICONS=1 to force icons on.\n")


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def profile_path(name: str) -> str:
    return os.path.join(profiles_dir(), name)


def list_profiles() -> list[str]:
    try:
        return sorted(
            f for f in os.listdir(profiles_dir())
            if _PROFILE_NAME_RE.match(f)
            and os.path.isfile(os.path.join(profiles_dir(), f)))
    except OSError:
        return []


_DEFAULT_KEYBINDS_CACHE: dict[str, str] | None = None


def _split_keybind(entry: str) -> tuple[str, str]:
    """Split a `trigger=action` keybind. Actions never contain `=`, so the last
    `=` is the separator — this handles triggers that include the `=` key
    (e.g. `super+==increase_font_size`)."""
    trigger, sep, action = entry.rpartition("=")
    return (trigger, action) if sep else (entry, "")


def list_default_keybinds() -> dict[str, str]:
    """Ghostty's built-in keybinds as {trigger: action} (for conflict checks)."""
    global _DEFAULT_KEYBINDS_CACHE
    if _DEFAULT_KEYBINDS_CACHE is not None:
        return _DEFAULT_KEYBINDS_CACHE
    res: dict[str, str] = {}
    if GHOSTTY:
        proc = _run([GHOSTTY, "+list-keybinds"], timeout=20)
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("keybind"):
                continue
            _, _, rhs = line.partition("=")
            trig, act = _split_keybind(rhs.strip())
            if trig:
                res[trig] = act
    _DEFAULT_KEYBINDS_CACHE = res
    return res


def run_doctor(sess: "Session") -> list[tuple[str, str]]:
    """Health-check the config. Returns (severity, message) pairs where
    severity is error / warn / info / ok, most serious first."""
    cfg, schema = sess.cfg, sess.schema
    errors, warns, infos = [], [], []

    ok, errs = validate(cfg.render())
    if not ok:
        for e in errs:
            errors.append(("error", "invalid config: " + e))

    present: dict[str, list[int]] = {}
    for i in range(len(cfg.lines)):
        kv = cfg._key_at(i)
        if kv:
            present.setdefault(kv[0], []).append(i)

    for name in sorted(present):
        if name not in schema:
            warns.append(("warn", f"unknown option `{name}` — not recognised by "
                          "your Ghostty (a typo or removed option?)"))

    for name in sorted(present):
        opt = schema.get(name)
        if opt and not opt.is_list and len(present[name]) > 1:
            warns.append(("warn", f"`{name}` is set {len(present[name])}× — only the "
                          "last takes effect; the earlier ones are dead"))

    for name in sorted(present):
        opt = schema.get(name)
        if opt is not None and not sess.is_overridden(name):
            infos.append(("info", f"`{name}` is set to its default — redundant, "
                          "can be removed"))

    triggers: dict[str, list[str]] = {}
    for entry in cfg.get_values("keybind"):
        trig, _ = _split_keybind(entry)
        triggers.setdefault(trig, []).append(entry)
    for trig, entries in triggers.items():
        if len(entries) > 1:
            warns.append(("warn", f"keybind trigger `{trig}` is bound {len(entries)}× "
                          "— the later binding shadows the earlier"))
    defaults = list_default_keybinds()
    for trig, entries in triggers.items():
        if trig in defaults:
            _, act = _split_keybind(entries[-1])
            if act and act != defaults[trig]:
                infos.append(("info", f"keybind `{trig}` overrides Ghostty's default "
                              f"(default is `{defaults[trig]}`)"))

    findings = errors + warns + infos
    if not findings:
        findings = [("ok", "no issues found — config looks healthy")]
    return findings


# ── Utils: SSH terminfo fix ─────────────────────────────────────────────────
#
# Ghostty advertises itself to programs with TERM=xterm-ghostty. When you SSH
# into another host, that host looks "xterm-ghostty" up in *its own* terminfo
# database — and most remote boxes have never heard of it. The remote shell
# then misbehaves: garbled or dead keys, no colour, broken `clear`/`tput`, or
# the classic `Error opening terminal: xterm-ghostty`. Forcing the `ssh`
# command to use a TERM every host already ships (xterm-256color) sidesteps
# this without touching the remote. The alias below does exactly that.

SSH_ALIAS_LINE = 'alias ssh="TERM=xterm-256color ssh"'
SSH_FIX_MARKER = "# added by SpookiUI — force a portable TERM over SSH (see fix-ssh)"
_SSH_ALIAS_RE = re.compile(r"""^\s*alias\s+ssh\s*=\s*['"]?\s*TERM=xterm-256color\s+ssh""")

SSH_FIX_EXPLANATION = [
    "Fix SSH — terminfo over SSH",
    "",
    "Ghostty tells programs it is `xterm-ghostty` (via the TERM variable).",
    "When you SSH into another machine, that machine looks xterm-ghostty up",
    "in its own terminfo database — and most remote hosts have never heard",
    "of it. The remote shell then misbehaves: garbled or dead keys, missing",
    "colour, broken `clear`/`tput`, or the classic error:",
    "    Error opening terminal: xterm-ghostty",
    "",
    "What this does",
    "  Adds one line to your shell rc (~/.zshrc or ~/.bashrc):",
    f"      {SSH_ALIAS_LINE}",
    "  so the `ssh` command runs with TERM=xterm-256color — a terminfo entry",
    "  essentially every host already ships. Your local Ghostty session keeps",
    "  its full xterm-ghostty features; only the outbound SSH connection is",
    "  downgraded to the universally-understood xterm-256color.",
    "",
    "Safe & idempotent",
    "  If the alias is already present it does nothing. Nothing on the remote",
    "  host is changed. To undo, delete the alias line from your rc file.",
    "  (A more thorough alternative is copying Ghostty's terminfo to each host,",
    "   but this alias is the quick fix that needs no remote access.)",
]


def _home() -> str:
    return os.path.expanduser("~")


def _tilde(path: str) -> str:
    """Render an absolute path under $HOME as ~/… for friendlier messages."""
    home = _home()
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def ssh_rc_scan_files() -> list[str]:
    """Existing shell rc files that might already carry the ssh alias."""
    names = (".zshrc", ".bashrc", ".bash_profile", ".zprofile",
             ".profile", ".bash_aliases")
    out = []
    for n in names:
        p = os.path.join(_home(), n)
        if os.path.isfile(p):
            out.append(p)
    return out


def ssh_rc_target() -> str:
    """Which rc file to add the alias to: the current login shell's primary rc.
    Defaults to ~/.zshrc (the macOS/Ghostty default shell) when unsure."""
    shell = os.path.basename(os.environ.get("SHELL", ""))
    home = _home()
    if shell == "bash":
        for n in (".bashrc", ".bash_profile"):
            p = os.path.join(home, n)
            if os.path.exists(p):
                return p
        return os.path.join(home, ".bashrc")
    return os.path.join(home, ".zshrc")


def find_ssh_alias() -> str | None:
    """Path of the first shell rc that already defines the TERM ssh alias."""
    for path in ssh_rc_scan_files():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if _SSH_ALIAS_RE.match(line):
                        return path
        except OSError:
            continue
    return None


def _verify_rc(path: str) -> tuple[bool, str]:
    """The 'source refresh' step. We deliberately *syntax-check* the rc with the
    shell's `-n` flag rather than fully sourcing it: a child process cannot
    change the parent shell's environment anyway (so a real source would be a
    no-op for the caller's terminal), and fully executing someone's rc
    non-interactively can hang or spawn things. This confirms our edit didn't
    break the file; the caller still tells the user to reload their shell."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        proc = _run([shell, "-n", path], timeout=10)
    except Exception as e:
        return False, str(e)
    if proc.returncode == 0:
        return True, "rc parses cleanly"
    return False, (proc.stderr or proc.stdout).strip() or "shell reported an error"


def apply_ssh_fix() -> tuple[bool, str]:
    """Add the ssh TERM alias to the user's shell rc if it isn't already there.
    Idempotent: a second run finds the alias and does nothing."""
    existing = find_ssh_alias()
    if existing:
        return True, (f"already fixed — an ssh alias forcing TERM=xterm-256color "
                      f"is present in {_tilde(existing)}; nothing to do")
    target = ssh_rc_target()
    block = "\n".join(["", SSH_FIX_MARKER, SSH_ALIAS_LINE]) + "\n"
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError as e:
        return False, f"could not write {_tilde(target)}: {e}"
    reload_hint = (f"run `source {_tilde(target)}` or open a new terminal "
                   "for it to take effect now")
    ok, note = _verify_rc(target)
    if ok:
        return True, f"added the ssh alias to {_tilde(target)} — {reload_hint}"
    return True, (f"added the ssh alias to {_tilde(target)} "
                  f"(warning: {note}) — {reload_hint}")


# ── Treats: fun background shaders ──────────────────────────────────────────
#
# Ghostty can run a "custom shader" behind the terminal grid: a ShaderToy-style
# GLSL fragment shader (`void mainImage(out vec4, in vec2)`) with `iResolution`,
# `iTime`, and `iChannel0` (the rendered terminal) available. `custom-shader` is
# a list, so several can be layered, but SpookiUI keeps at most ONE treat active
# at a time. `custom-shader-animation = true` animates only the focused window.
# A "treat" is one of these shaders that SpookiUI bundles, writes to
# `<ghostty-config-dir>/shaders/spookiui/<slug>.glsl`, and toggles for you.
#
# Every treat composites *additively* over the terminal and only brightens the
# darkest background pixels (a tight luminance mask), so the effect fades into the
# background and your text, cursor, and borders stay readable. Brightness and
# iteration counts are deliberately kept low so treats are subtle and cheap to
# render. All treats are OFF by default — nothing is enabled unless you ask.


# All treats are original SpookiUI shaders written to the same convention: they
# composite additively, gated by a tight luminance mask so only the darkest
# background pixels are touched and your text always stays legible.
_GLSL_MATRIX_RAIN = """\
// SpookiUI treat: Matrix Rain.
// Falling green glyph columns in the spirit of `cmatrix`. Drawn only over dark
// background pixels so your text stays readable. Original SpookiUI shader.

const float COLUMNS = 46.0;

float rand(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);

    float cw = iResolution.x / COLUMNS;          // column width (px)
    float col = floor(fragCoord.x / cw);
    float chH = cw * 1.5;                         // glyph cell height (px)
    float rows = iResolution.y / chH;

    // Each column's stream head sweeps top->bottom at its own speed/phase.
    float speed = mix(0.12, 0.45, rand(vec2(col, 2.0)));
    float seed = rand(vec2(col, 9.0));
    float phase = fract(iTime * speed + seed);
    float head = 1.0 - phase;                     // uv.y of the head
    float trail = mix(0.25, 0.60, rand(vec2(col, 5.0)));
    float d = head - uv.y;                         // >0 for the trailing tail
    float body = (d >= 0.0 && d <= trail) ? (1.0 - d / trail) : 0.0;
    body = pow(body, 1.4);

    // Per-cell glyph flicker + a sub-cell mask to read as characters.
    float cellRow = floor(uv.y * rows);
    float g = rand(vec2(col, cellRow) + floor(iTime * mix(6.0, 14.0, seed)));
    float glyph = step(0.35, g);
    vec2 f = fract(vec2(fragCoord.x / cw, uv.y * rows));
    float mask = step(0.12, f.x) * step(f.x, 0.88)
               * step(0.10, f.y) * step(f.y, 0.90);
    float lit = body * glyph * mask;

    // The leading glyph glows near-white; the tail is green.
    float headGlow = smoothstep(0.05, 0.0, abs(d)) * glyph * mask;
    vec3 green = vec3(0.15, 1.0, 0.30);
    vec3 rain = green * lit * 0.42 + vec3(0.80, 1.0, 0.85) * headGlow * 0.45;

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.14, lum);

    vec3 res = term.rgb + rain * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_PIPES = """\
// SpookiUI treat: Pipes.
// A neon 2D homage to the Windows 95 "3D Pipes" screensaver: a lattice of
// glowing pipe segments that pulse and cycle colour. A true 3D pipes maze needs
// real geometry a terminal shader can't do, so this evokes the vibe in 2D.
// Original SpookiUI shader; drawn only over dark background pixels.

float h2(vec2 p) {
    p = fract(p * vec2(127.1, 311.7));
    p += dot(p, p + 34.1);
    return fract(p.x * p.y);
}

float seg(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    float t = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    return length(pa - ba * t);
}

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);

    float s = iResolution.y / 9.0;      // grid cell size (px) -> ~9 rows
    vec2 g = fragCoord / s;             // grid-space coords (cell units)
    vec2 cell = floor(g);

    float best = 1e9;
    vec2 bestNode = vec2(0.0);
    // Test the pipe segments radiating from the 3x3 nearest lattice nodes.
    for (int oy = -1; oy <= 1; oy++) {
        for (int ox = -1; ox <= 1; ox++) {
            vec2 node = cell + vec2(float(ox), float(oy));
            if (h2(node + vec2(0.3, 0.7)) > 0.45) {          // edge going right
                float d = seg(g, node, node + vec2(1.0, 0.0));
                if (d < best) { best = d; bestNode = node + vec2(0.5, 0.0); }
            }
            if (h2(node + vec2(0.8, 0.2)) > 0.45) {          // edge going up
                float d = seg(g, node, node + vec2(0.0, 1.0));
                if (d < best) { best = d; bestNode = node + vec2(0.0, 0.5); }
            }
        }
    }

    float pipeR = 0.16;                 // pipe radius in cell units
    float core = 1.0 - smoothstep(pipeR * 0.5, pipeR, best);
    float glow = 1.0 - smoothstep(pipeR, pipeR * 2.6, best);

    float hue = fract(0.05 * iTime + 0.15 * (bestNode.x + bestNode.y));
    vec3 col = 0.5 + 0.5 * cos(6.2831 * (hue + vec3(0.0, 0.33, 0.67)));
    float pulse = 0.6 + 0.4 * sin(iTime * 3.0 + (bestNode.x + bestNode.y) * 1.7);
    vec3 pipe = col * (core * 0.55 + glow * 0.22) * pulse;

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);

    vec3 res = term.rgb + pipe * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_MYSTIFY = """\
// SpookiUI treat: Mystify.
// The Windows 3.x/9x "Mystify" screensaver: a couple of polygons whose corners
// drift and bounce off the edges, trailing colour. Original SpookiUI shader;
// drawn faintly over dark background pixels only.

const float THICK = 0.0016;   // line half-width (uv units)

// Corner position for polygon `poly`, vertex `v`, bouncing in [0,1]^2.
vec2 corner(float poly, float v, float aspect) {
    float s = poly * 17.0 + v * 3.0;
    vec2 speed = vec2(0.13 + 0.05 * fract(s * 1.7),
                      0.11 + 0.05 * fract(s * 2.3));
    vec2 phase = vec2(fract(s * 5.1), fract(s * 8.9));
    // triangle wave -> smooth bounce between 0 and 1
    vec2 t = fract(iTime * speed + phase);
    vec2 p = abs(t * 2.0 - 1.0);
    p.x *= aspect;                 // keep motion square-ish, not stretched
    return p;
}

float seg(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    float t = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    return length(pa - ba * t);
}

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);
    float aspect = iResolution.x / iResolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);

    vec3 acc = vec3(0.0);
    const int POLYS = 2;
    const int VERTS = 4;
    for (int k = 0; k < POLYS; k++) {
        float poly = float(k);
        float hue = fract(0.04 * iTime + poly * 0.5);
        vec3 col = 0.5 + 0.5 * cos(6.2831 * (hue + vec3(0.0, 0.33, 0.67)));
        for (int i = 0; i < VERTS; i++) {
            vec2 a = corner(poly, float(i), aspect);
            vec2 b = corner(poly, float((i + 1) % VERTS), aspect);
            float d = seg(p, a, b);
            acc += col * (1.0 - smoothstep(THICK, THICK * 3.5, d));
        }
    }

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);
    vec3 res = term.rgb + acc * 0.5 * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_PLASMA = """\
// SpookiUI treat: Plasma.
// The classic demoscene / After Dark plasma field — layered sines that roll and
// interfere. Original SpookiUI shader; kept dim and drawn over dark pixels only.

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);

    vec2 p = uv * 6.0;
    float t = iTime * 0.5;
    float v = sin(p.x + t)
            + sin(p.y + t * 1.3)
            + sin((p.x + p.y) * 0.7 + t * 0.9)
            + sin(length(p - 3.0) - t * 1.6);
    v *= 0.25;                                   // back into ~[-1,1]

    vec3 col = 0.5 + 0.5 * cos(6.2831 * (v + vec3(0.0, 0.33, 0.67)));

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);
    vec3 res = term.rgb + col * 0.14 * bgmask;   // very faint wash
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_BUBBLES = """\
// SpookiUI treat: Lava Lamp.
// Slow metaball blobs rising and merging like a 70s lava lamp / the old "Bubbles"
// screensaver. Original SpookiUI shader; a soft, dim glow over dark pixels only.

const int BLOBS = 5;

float h(float n) { return fract(sin(n) * 43758.5453); }

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);
    float aspect = iResolution.x / iResolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);

    float field = 0.0;
    for (int i = 0; i < BLOBS; i++) {
        float fi = float(i);
        float x = (0.15 + 0.7 * h(fi * 3.7)) * aspect;
        x += 0.06 * sin(iTime * (0.3 + 0.2 * h(fi)) + fi);
        float speed = 0.05 + 0.05 * h(fi * 9.1);
        float y = fract(h(fi * 5.3) + iTime * speed);       // rise, wrap
        float r = 0.10 + 0.06 * h(fi * 2.1);
        float d = length(p - vec2(x, y));
        field += r * r / (d * d + 0.0007);                  // metaball falloff
    }

    float blob = smoothstep(0.9, 1.8, field);
    float hue = fract(0.03 * iTime);
    vec3 col = 0.5 + 0.5 * cos(6.2831 * (hue + vec3(0.0, 0.28, 0.55)));

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);
    vec3 res = term.rgb + col * blob * 0.22 * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_FIREWORKS = """\
// SpookiUI treat: Fireworks.
// Rockets burst into fading, gravity-pulled sparks. Original SpookiUI shader;
// the sparks are added over dark background pixels only.

float h(float n) { return fract(sin(n) * 43758.5453); }

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);
    float aspect = iResolution.x / iResolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);

    vec3 acc = vec3(0.0);
    const int BURSTS = 4;
    const int SPARKS = 14;
    for (int b = 0; b < BURSTS; b++) {
        float fb = float(b);
        float period = 2.5 + h(fb) * 1.5;
        float t = mod(iTime + fb * 1.7, period) / period;       // 0..1 burst life
        vec2 centre = vec2(h(fb * 3.1) * aspect, 0.35 + 0.4 * h(fb * 5.3));
        vec3 col = 0.5 + 0.5 * cos(6.2831 * (h(fb * 7.7) + vec3(0.0, 0.33, 0.67)));
        for (int s = 0; s < SPARKS; s++) {
            float fs = float(s);
            float ang = 6.2831 * (fs / float(SPARKS)) + h(fb + fs);
            float sp = 0.18 * (0.6 + 0.4 * h(fb * 2.0 + fs));
            vec2 pos = centre + vec2(cos(ang), sin(ang)) * sp * t;
            pos.y -= 0.15 * t * t;                               // gravity droop
            float fade = 1.0 - t;
            acc += col * (1.0 - smoothstep(0.002, 0.010, length(p - pos)))
                       * fade * fade;
        }
    }

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);
    vec3 res = term.rgb + acc * 0.6 * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_CHOMPER = """\
// SpookiUI treat: Chomper.
// A yellow chomping wedge munches a row of pellets while a ghost gives chase — a
// fond wink at the 1980 maze arcade classic. Original SpookiUI shader: plain
// shapes, no game artwork. Drawn over dark background pixels only.

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);
    float aspect = iResolution.x / iResolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);

    float row = 0.5;
    float r = 0.05;
    float loop = aspect + 0.4;
    float px = mod(iTime * 0.22, loop) - 0.2;          // chomper enters from left
    vec3 acc = vec3(0.0);

    // Pellets: evenly spaced dots, eaten once the chomper has passed them.
    float spacing = 0.14;
    float gx = floor(p.x / spacing) * spacing + spacing * 0.5;
    if (gx > px + r) {
        acc += vec3(1.0, 0.85, 0.55)
             * (1.0 - smoothstep(0.010, 0.016, length(p - vec2(gx, row))));
    }

    // Chomper: a disc with an opening/closing mouth wedge facing right.
    vec2 rel = p - vec2(px, row);
    float dc = length(rel);
    float mouth = 0.62 * (0.5 + 0.5 * sin(iTime * 10.0));   // half-angle of the gap
    float body = (dc < r && abs(atan(rel.y, rel.x)) > mouth) ? 1.0 : 0.0;
    body *= smoothstep(r, r - 0.006, dc);
    acc += vec3(1.0, 0.92, 0.15) * body;

    // Ghost: a bobbing disc trailing behind, with two eyes.
    vec2 gr = p - vec2(px - 0.16, row + 0.012 * sin(iTime * 6.0));
    float ghost = 1.0 - smoothstep(r - 0.006, r, length(gr));
    acc += vec3(1.0, 0.4, 0.7) * ghost * 0.8;
    float eyes = max(1.0 - smoothstep(0.006, 0.010, length(gr - vec2(-0.012, 0.010))),
                     1.0 - smoothstep(0.006, 0.010, length(gr - vec2( 0.014, 0.010))));
    acc = mix(acc, vec3(0.05, 0.05, 0.2), eyes * ghost);   // eye holes in the ghost

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);
    vec3 res = term.rgb + acc * 0.7 * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_BARRELS = """\
// SpookiUI treat: Barrels.
// Barrels tumble down slanted girders — a wink at the 1981 platform arcade
// classic. Original SpookiUI shader: plain shapes, no game artwork. Drawn over
// dark background pixels only.

float h(float n) { return fract(sin(n) * 43758.5453); }

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);
    float aspect = iResolution.x / iResolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);

    vec3 acc = vec3(0.0);
    const int LEVELS = 4;
    float slope = 0.06;

    // Girders: tilted bands, alternating slope direction on each level.
    for (int i = 0; i < LEVELS; i++) {
        float fi = float(i);
        float dir = (mod(fi, 2.0) < 1.0) ? 1.0 : -1.0;
        float gy = 0.12 + 0.24 * fi + slope * dir * (p.x / aspect);
        acc += vec3(0.9, 0.3, 0.25)
             * (1.0 - smoothstep(0.006, 0.014, abs(p.y - gy))) * 0.5;
    }

    // Barrels: roll along a level, then drop to the next, staggered in time.
    const int BARRELS = 5;
    for (int b = 0; b < BARRELS; b++) {
        float fb = float(b);
        float lvlF = fract(iTime * 0.12 + h(fb * 3.0)) * float(LEVELS - 1);
        float lvl = floor(lvlF);
        float frac = fract(lvlF);
        float dir = (mod(lvl, 2.0) < 1.0) ? 1.0 : -1.0;
        float x = ((dir > 0.0) ? frac : 1.0 - frac) * aspect;
        // Sit on TOP of the girder. Screen y grows downward, so "above the
        // slope" is a smaller y — subtract the offset.
        float y = 0.12 + 0.24 * lvl + slope * dir * (x / aspect) - 0.03;
        vec2 bp = p - vec2(x, y);
        float barrel = 1.0 - smoothstep(0.022, 0.028, length(bp));
        float roll = 0.5 + 0.5 * sin(atan(bp.y, bp.x) * 3.0 + iTime * 8.0 * dir);
        acc += mix(vec3(0.8, 0.55, 0.2), vec3(0.5, 0.3, 0.1), roll) * barrel;
    }

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);
    vec3 res = term.rgb + acc * 0.6 * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""

_GLSL_JUMPER = """\
// SpookiUI treat: Jumper.
// A little hero hops along the ground beneath a row of scrolling question-blocks
// — a nostalgic nod to side-scrolling platformers. Original SpookiUI shader:
// plain shapes, no game artwork. Drawn over dark background pixels only.

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec4 term = texture(iChannel0, uv);
    float aspect = iResolution.x / iResolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);

    vec3 acc = vec3(0.0);
    // Screen y grows downward, so the ground sits near the bottom (large y) and
    // the block row above it is at a smaller y.
    float ground = 0.82;
    acc += vec3(0.4, 0.25, 0.15)
         * (1.0 - smoothstep(0.004, 0.012, abs(p.y - ground)));

    // Question-blocks: a scrolling row floating above the ground.
    float bspace = 0.30;
    float scroll = mod(iTime * 0.15, bspace);
    float bx = floor((p.x + scroll) / bspace) * bspace - scroll + bspace * 0.5;
    vec2 bc = p - vec2(bx, 0.5);
    float block = step(abs(bc.x), 0.035) * step(abs(bc.y), 0.035);
    acc += vec3(0.95, 0.7, 0.1) * block * 0.8;
    acc += vec3(0.1) * (1.0 - smoothstep(0.006, 0.010, length(bc))) * block;

    // Hero: bounces in parabolic hops on the ground, staying below the blocks.
    // "Up" is toward a smaller y, so the hop and the head both subtract.
    float hopT = fract(iTime * 0.5);
    float hop = 4.0 * hopT * (1.0 - hopT) * 0.14;      // parabola
    vec2 hp = p - vec2(0.35 * aspect, ground - 0.05 - hop);
    acc += vec3(0.9, 0.2, 0.15) * step(abs(hp.x), 0.024) * step(abs(hp.y), 0.030);
    acc += vec3(0.95, 0.8, 0.6)
         * (1.0 - smoothstep(0.016, 0.020, length(hp - vec2(0.0, -0.036))));   // head

    float lum = dot(term.rgb, vec3(0.2126, 0.7152, 0.0722));
    float bgmask = 1.0 - smoothstep(0.05, 0.15, lum);
    vec3 res = term.rgb + acc * 0.6 * bgmask;
    fragColor = vec4(min(res, vec3(1.0)), term.a);
}
"""


@dataclass
class Treat:
    slug: str          # file/CLI name, e.g. "matrix-rain"
    name: str          # display name, e.g. "Matrix Rain"
    desc: str          # one-line summary
    glsl: str          # the full fragment-shader source
    note: str = ""     # extra guidance shown in the detail pane


TREATS: list[Treat] = [
    Treat("matrix-rain", "Matrix Rain",
          "Falling green glyph columns, cmatrix-style.",
          _GLSL_MATRIX_RAIN,
          "Drawn only over dark background pixels, so text stays readable."),
    Treat("pipes", "Pipes",
          "A neon homage to the Win95 3D Pipes screensaver.",
          _GLSL_PIPES,
          "A 2D shader can't do true 3D pipes — this evokes the vibe with a "
          "glowing, colour-cycling pipe lattice."),
    Treat("mystify", "Mystify",
          "Bouncing, colour-trailing polygons — the Windows Mystify saver.",
          _GLSL_MYSTIFY,
          "Two polygons whose corners drift and bounce off the edges."),
    Treat("plasma", "Plasma",
          "A rolling demoscene plasma field, After Dark style.",
          _GLSL_PLASMA,
          "Kept as a very faint colour wash so your text stays legible."),
    Treat("lava-lamp", "Lava Lamp",
          "Slow rising metaball blobs — a 70s lava lamp / Bubbles saver.",
          _GLSL_BUBBLES,
          "Soft merging blobs drift upward and wrap around."),
    Treat("fireworks", "Fireworks",
          "Rockets bursting into fading, gravity-pulled sparks.",
          _GLSL_FIREWORKS,
          "A few staggered bursts drift down and dim out."),
    Treat("chomper", "Chomper",
          "A chomping wedge eats a row of pellets, chased by a ghost.",
          _GLSL_CHOMPER,
          "A fond nod to the 1980 maze arcade classic — plain shapes, no game "
          "artwork."),
    Treat("barrels", "Barrels",
          "Barrels tumble down slanted girders.",
          _GLSL_BARRELS,
          "A wink at the 1981 platform arcade classic — plain shapes, no game "
          "artwork."),
    Treat("jumper", "Jumper",
          "A little hero hops beneath scrolling question-blocks.",
          _GLSL_JUMPER,
          "A nostalgic nod to side-scrolling platformers — plain shapes, no game "
          "artwork."),
]

TREAT_BY_SLUG: dict[str, Treat] = {t.slug: t for t in TREATS}


def shaders_dir() -> str:
    """Where SpookiUI writes its bundled treat shaders (namespaced so we never
    disturb any `custom-shader` you added yourself)."""
    return os.path.join(os.path.dirname(config_path()), "shaders", "spookiui")


def treat_shader_path(t: Treat) -> str:
    return os.path.join(shaders_dir(), t.slug + ".glsl")


def write_treat_shader(t: Treat) -> str:
    """Write a treat's GLSL to disk (idempotent), returning the absolute path."""
    path = treat_shader_path(t)
    os.makedirs(shaders_dir(), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == t.glsl:
                return path
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(t.glsl)
    return path


def _is_treat_path(value: str) -> bool:
    """Whether a `custom-shader` value points at one of SpookiUI's own treats."""
    base = os.path.abspath(shaders_dir()) + os.sep
    return os.path.abspath(os.path.expanduser(value.strip())).startswith(base)


def enabled_treat_slugs(sess: "Session") -> list[str]:
    """Slugs of the treats currently enabled in the config (registry order)."""
    active = set()
    for v in sess.cfg.get_values("custom-shader"):
        if _is_treat_path(v):
            slug = os.path.splitext(os.path.basename(v.strip()))[0]
            if slug in TREAT_BY_SLUG:
                active.add(slug)
    return [t.slug for t in TREATS if t.slug in active]


def apply_treat_lines(sess: "Session", slugs) -> None:
    """Mutate `sess.cfg.lines` so at most ONE treat is active. Only one treat may
    run at a time; if `slugs` names several, the first real one wins (pass `[]` to
    turn treats off). Writes the GLSL files (harmless — not config) and rewrites
    `custom-shader` + `custom-shader-animation`, preserving any `custom-shader`
    entries you added yourself. Does NOT validate/write/reload — callers do that
    (rollback-safe)."""
    chosen = next((s for s in slugs if s in TREAT_BY_SLUG), None)
    want = [TREAT_BY_SLUG[chosen]] if chosen else []
    for t in want:
        write_treat_shader(t)
    foreign = [v for v in sess.cfg.get_values("custom-shader")
               if not _is_treat_path(v)]
    new_list = foreign + [treat_shader_path(t) for t in want]
    if new_list:
        sess.cfg.set_list("custom-shader", new_list)
    else:
        sess.cfg.unset("custom-shader")
    if want:
        # `true` animates only the focused/active window; unfocused ones pause. So
        # opening (and focusing) a new terminal freezes the treat in the others —
        # only one window animates at a time, which also keeps the GPU idle on the
        # windows you're not looking at. (`always` would animate every window at
        # once.) Combined with the single-treat rule, at most one shader ever runs.
        sess.cfg.set_scalar("custom-shader-animation", "true")
    elif not new_list:
        sess.cfg.unset("custom-shader-animation")
    # else: only your own shaders remain — leave custom-shader-animation alone.


def set_treats(sess: "Session", slugs) -> tuple[bool, str]:
    """Enable exactly `slugs`, following the validate → back up → write → reload
    → rollback discipline every mutation path uses."""
    snap = list(sess.cfg.lines)
    try:
        apply_treat_lines(sess, slugs)
    except OSError as e:
        sess.cfg.lines = snap
        return False, f"could not write shader file: {e}"
    ok, errs = validate(sess.cfg.render())
    if not ok:
        sess.cfg.lines = snap
        return False, "invalid: " + (errs[0] if errs else "validation failed")
    sess.ensure_backup()
    sess.cfg.write()
    sess.dirty = False
    if sess.auto_apply and CAN_RELOAD:
        r_ok, m = reload_ghostty()
        return True, ("treats applied + reloaded live" if r_ok
                      else f"treats applied (reload: {m})")
    return True, "treats applied"


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
    avg = (r + g + b) // 3
    if avg < 8:
        gray = 232
    elif avg > 238:
        gray = 255
    else:
        gray = 232 + (avg - 8) // 10
    def cube_val(i):
        return 0 if i == 0 else 55 + i * 40
    cr, cg, cb = cube_val(ri), cube_val(gi), cube_val(bi)
    gl = 8 + (gray - 232) * 10
    d_cube = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    d_gray = (gl - r) ** 2 + (gl - g) ** 2 + (gl - b) ** 2
    return ci if d_cube <= d_gray else gray


def run_tui(sess: "Session") -> None:
    import curses
    import locale
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    icons = icons_available(sess)
    if not icons:
        _maybe_show_icon_notice()

    try:
        curses.set_escdelay(25)
    except Exception:
        pass
    try:
        curses.wrapper(lambda scr: App(scr, sess, icons=icons).run())
    except KeyboardInterrupt:
        pass


def _maybe_show_icon_notice() -> None:
    """Once, before entering the TUI, tell the user how to enable category icons.
    Never blocks the app — on any hiccup we just continue into the fallback view."""
    marker = _icon_notice_marker()
    try:
        if os.path.exists(marker):
            return
    except OSError:
        return
    try:
        print(icon_notice_text())
        input("Press Enter to continue… ")
    except (EOFError, KeyboardInterrupt):
        pass
    except Exception:  # noqa: BLE001 — a notice must never stop the app launching
        return
    try:
        os.makedirs(spookiui_data_dir(), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("shown\n")
    except OSError:
        pass


class App:
    def __init__(self, stdscr, sess: Session, icons: bool = False):
        import curses
        self.curses = curses
        self.scr = stdscr
        self.sess = sess
        self.icons = icons
        self.status = ""
        self.status_kind = "info"
        self.focus = "cats"
        self.cat_idx = 0
        self.opt_idx = 0
        self.opt_scroll = 0
        self.doc_scroll = 0
        self.search = ""
        self.search_mode = False

        self.by_cat: dict[str, list[str]] = {}
        for c in CATEGORY_ORDER:
            names = sorted(n for n, o in sess.schema.items()
                           if o.category == c and platform_visible(o))
            if names:
                self.by_cat[c] = names
        self.categories = [c for c in CATEGORY_ORDER if c in self.by_cat]
        self.categories.append(UTILS_CATEGORY)
        self.categories.append(TREATS_CATEGORY)

        self._swatch_cache: dict[int, int] = {}
        self._pair_cache: dict[tuple, int] = {}
        self._next_pair = 32
        self._init_colors()

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
            except Exception:
                self._update_info = None

        threading.Thread(target=worker, daemon=True).start()

    def _init_colors(self):
        c = self.curses
        c.start_color()
        try:
            c.use_default_colors()
            bg = -1
        except Exception:
            bg = c.COLOR_BLACK
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

        if theme_override is None:
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

    def cur_cat(self) -> str | None:
        if self.search_mode or not self.categories:
            return None
        return self.categories[self.cat_idx]

    def current_names(self) -> list[str]:
        if self.search_mode:
            return self._search_results
        if not self.categories:
            return []
        cat = self.categories[self.cat_idx]
        if cat in (UTILS_CATEGORY, TREATS_CATEGORY):
            return []
        return self.by_cat[cat]

    def current_option(self) -> Option | None:
        names = self.current_names()
        if not names:
            return None
        self.opt_idx = max(0, min(self.opt_idx, len(names) - 1))
        return self.sess.schema[names[self.opt_idx]]

    def run(self):
        c = self.curses
        while True:
            self.draw()
            try:
                ch = self.scr.getch()
            except KeyboardInterrupt:
                ch = 3
            if ch == c.KEY_RESIZE:
                continue
            if not self.handle_key(ch):
                break

    def draw(self):
        c = self.curses
        info = self._update_info
        if info and info.get("outdated") and not self._update_announced:
            self._update_announced = True
            if not self.status:
                self._msg(f"SpookiUI {info['latest']} is available — press U to update", "warn")
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
        cat_w = 24 if self.icons else 22
        opt_w = max(28, min(40, (w - cat_w) // 2))
        det_x = cat_w + opt_w + 1

        for i, cat in enumerate(self.categories):
            y = top + i
            if y > bottom:
                break
            attr = c.A_NORMAL
            if self.icons:
                icon = CATEGORY_ICONS.get(cat, DEFAULT_CATEGORY_ICON)
                name = {UTILS_CATEGORY: "Utils", TREATS_CATEGORY: "Treats"}.get(cat, cat)
                label = f" {icon}  {name}"
            else:
                label = f" {cat}"
            if self.search_mode:
                attr = c.color_pair(4)
            elif i == self.cat_idx:
                attr = c.color_pair(9) | c.A_BOLD if self.focus == "cats" \
                    else c.color_pair(5) | c.A_BOLD
            self.safe(y, 0, label.ljust(cat_w)[:cat_w], attr)
        for y in range(top, bottom + 1):
            self.safe(y, cat_w, "│", c.color_pair(4))
            self.safe(y, cat_w + opt_w, "│", c.color_pair(4))

        if self.cur_cat() == UTILS_CATEGORY:
            self._draw_utils_menu(top, bottom, cat_w, opt_w)
            self._draw_utils_detail(top, bottom, det_x, w - det_x - 1)
            return
        if self.cur_cat() == TREATS_CATEGORY:
            self._draw_treats_menu(top, bottom, cat_w, opt_w)
            self._draw_treats_detail(top, bottom, det_x, w - det_x - 1)
            return

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
        if len(names) > rows:
            self.safe(top, cat_w + opt_w - 1, "↕", c.color_pair(5))

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
        if (opt.name == "theme" or opt.kind in ("color", "theme", "palette")
                or opt.category == "Colors & Theme") and y + 3 <= bottom:
            self.safe(y, x, "─ preview " + "─" * max(0, width - 10), c.color_pair(4)); y += 1
            used = self._draw_color_preview(y, x, width, self._effective_colors())
            if used:
                y += used + 1
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
        kindmap = {"ok": 6, "error": 7, "warn": 8, "info": 2}
        self.safe(sy, 0, self.status[:w], c.color_pair(kindmap.get(self.status_kind, 2)) | c.A_BOLD)
        if self.search_mode:
            hints = " type to filter · ↑↓ move · Enter edit · Esc exit search "
        elif self.focus == "cats":
            hints = " ↑↓ category · →/Enter options · / search · a auto-apply · v utils · t treats · d changes · ? help · q quit "
        else:
            hints = " ↑↓ option · Enter/→ edit · ← back · u reset · s save · r reload · / search · ? help · q quit "
        bar = hints + " " * max(0, w - len(hints))
        self.safe(h - 1, 0, bar[:w], c.color_pair(1))

    def handle_key(self, ch) -> bool:
        c = self.curses
        if self.search_mode:
            return self._handle_search_key(ch)

        if ch in (ord("q"), ord("Q"), 3, 24):
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
        if ch in (ord("U"),):
            self._do_self_update()
            return True
        if ch in (ord("p"), ord("P")):
            self._profiles_overlay()
            return True
        if ch in (ord("c"), ord("C")):
            self._doctor_overlay()
            return True
        if ch in (ord("v"), ord("V")):
            self._utils_overlay()
            return True
        if ch in (ord("t"), ord("T")):
            self._treats_overlay()
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
            if self.cur_cat() == UTILS_CATEGORY:
                self._utils_overlay()
            elif self.cur_cat() == TREATS_CATEGORY:
                self._treats_overlay()
            else:
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
        elif ch in (ord("u"),):
            self._reset_current()
        elif ch in (ord("\n"), c.KEY_ENTER, 10, 13, c.KEY_RIGHT, ord("l")):
            if self.cur_cat() == UTILS_CATEGORY:
                self._utils_overlay()
            elif self.cur_cat() == TREATS_CATEGORY:
                self._treats_overlay()
            else:
                self.edit_current()
        return True

    def _enter_search(self):
        self.search = ""
        self.search_mode = True
        self._search_results = sorted(
            n for n, o in self.sess.schema.items() if platform_visible(o))
        self.opt_idx = self.opt_scroll = 0

    def _handle_search_key(self, ch) -> bool:
        c = self.curses
        if ch in (27,):
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
        else:
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
            self.sess.cfg.lines = snap
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

    def _do_self_update(self):
        info = self._update_info
        if not (info and info.get("outdated")):
            self._msg(f"SpookiUI v{__version__} is already up to date", "info")
            return
        if not self._confirm(f"Update SpookiUI to {info['latest']}? (a backup is kept)"):
            return
        self._msg(f"updating to {info['latest']}…", "info"); self.draw()
        ok, m = self_update()
        self._msg(m, "ok" if ok else "error")

    def _edit_bool(self, opt: Option):
        cur = self.sess.effective(opt.name)
        new = "false" if cur == "true" else "true"
        ok, errs = self._commit_scalar(opt, new)
        if ok:
            self._msg(f"{opt.name} = {new}" + (" (live)" if self.sess.auto_apply else " (staged)"), "ok")
        else:
            self._msg("invalid: " + (errs[0] if errs else "?"), "error")

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
        cur = self.sess.effective(opt.name)
        try:
            start = float(cur) if cur not in ("", None) else lo
        except ValueError:
            start = lo
        idx = max(0, min(nsteps, int(round((start - lo) / step))))

        def value(i):
            return hi if i >= nsteps else lo + i * step

        pending = True
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
        NROWS = 5
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
            if row == 0:
                if ch in (c.KEY_LEFT,):
                    modsel = (modsel - 1) % len(KEYBIND_MODS)
                elif ch in (c.KEY_RIGHT,):
                    modsel = (modsel + 1) % len(KEYBIND_MODS)
                elif ch in (ord(" "),):
                    m = KEYBIND_MODS[modsel]; state["mods"][m] = not state["mods"][m]
            elif row == 1:
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    pick = self._picker("special key", KEYBIND_NAMED_KEYS, state["key"])
                    if pick:
                        state["key"] = pick
                elif ch in (c.KEY_BACKSPACE, 127, 8, c.KEY_DC):
                    state["key"] = ""
                elif 32 < ch < 127:
                    k = chr(ch)
                    state["key"] = k.lower() if k.isalpha() else k
            elif row == 2:
                if ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    acts = list_actions()
                    if acts:
                        pick = self._picker("action", acts, state["action"])
                        if pick:
                            state["action"] = pick
                    else:
                        error = "could not load Ghostty actions"
            elif row == 3:
                if ch in (c.KEY_BACKSPACE, 127, 8):
                    state["args"] = state["args"][:-1]
                elif ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                    row = 4
                elif 32 <= ch < 127:
                    state["args"] += chr(ch)
            elif row == 4:
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

    def _profiles_overlay(self):
        c = self.curses
        sel = 0
        while True:
            profiles = list_profiles()
            sel = max(0, min(sel, len(profiles) - 1)) if profiles else 0
            self.scr.erase()
            h, w = self.dims()
            self.safe(0, 0, " profiles ".ljust(w), c.color_pair(1) | c.A_BOLD)
            self.safe(1, 0, (" saved in " + profiles_dir())[:w], c.color_pair(4))
            top = 3
            if not profiles:
                self.safe(top, 2, "(no profiles yet — press 's' to save the current config)",
                          c.color_pair(4))
            for i, p in enumerate(profiles):
                y = top + i
                if y >= h - 2:
                    break
                attr = c.color_pair(3) | c.A_BOLD if i == sel else c.A_NORMAL
                self.safe(y, 2, ("→ " if i == sel else "  ") + p, attr)
            self.safe(h - 1, 0,
                      " s save · Enter/l load · d delete · t light↔dark · Esc close ".ljust(w),
                      c.color_pair(1))
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (27,):
                return
            if ch in (c.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (c.KEY_DOWN, ord("j")):
                sel = min(max(0, len(profiles) - 1), sel + 1)
            elif ch in (ord("s"),):
                name = self._line_editor(
                    "save profile as › ", "",
                    hint="name (letters/numbers/._-) · Enter save · Esc cancel")
                if name:
                    ok, m = self.sess.save_profile(name)
                    self._msg(m, "ok" if ok else "error")
            elif ch in (ord("d"), c.KEY_DC) and profiles:
                if self._confirm(f"Delete profile '{profiles[sel]}'?"):
                    ok, m = self.sess.delete_profile(profiles[sel])
                    self._msg(m, "ok" if ok else "error")
            elif ch in (ord("t"),):
                ok, m = self.sess.toggle_light_dark()
                self._msg(m, "ok" if ok else "warn")
                if ok:
                    return
            elif ch in (ord("\n"), c.KEY_ENTER, 10, 13, ord("l")) and profiles:
                if self._confirm(f"Load '{profiles[sel]}'? (current config backed up)"):
                    ok, m = self.sess.load_profile(profiles[sel])
                    self._msg(m, "ok" if ok else "error")
                    if ok:
                        return

    def _doctor_overlay(self):
        c = self.curses
        self._msg("running config check…", "info"); self.draw()
        findings = run_doctor(self.sess)
        colors = {"error": 7, "warn": 8, "info": 4, "ok": 6}
        icons = {"error": "✗", "warn": "!", "info": "·", "ok": "✓"}
        scroll = 0
        while True:
            self.scr.erase()
            h, w = self.dims()
            n_err = sum(1 for s, _ in findings if s == "error")
            n_warn = sum(1 for s, _ in findings if s == "warn")
            self.safe(0, 0, f" config check · {n_err} error(s), {n_warn} warning(s) ".ljust(w),
                      c.color_pair(1) | c.A_BOLD)
            wrapped = []
            for sev, msg in findings:
                for j, seg in enumerate(self._wrap(msg, w - 6)):
                    wrapped.append((sev, (icons.get(sev, " ") + " " if j == 0 else "  ") + seg))
            rows = h - 3
            for r in range(rows):
                i = scroll + r
                if i >= len(wrapped):
                    break
                sev, text = wrapped[i]
                attr = c.color_pair(colors.get(sev, 4))
                if sev in ("error", "warn"):
                    attr |= c.A_BOLD
                self.safe(2 + r, 2, text[:w - 3], attr)
            self.safe(h - 1, 0, " ↑↓ scroll · any other key to close ".ljust(w), c.color_pair(1))
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (c.KEY_DOWN,):
                scroll = min(max(0, len(wrapped) - rows), scroll + 1)
            elif ch in (c.KEY_UP,):
                scroll = max(0, scroll - 1)
            elif ch in (c.KEY_NPAGE,):
                scroll = min(max(0, len(wrapped) - rows), scroll + rows)
            elif ch in (c.KEY_PPAGE,):
                scroll = max(0, scroll - rows)
            else:
                return

    def _draw_utils_menu(self, top, bottom, cat_w, opt_w):
        c = self.curses
        x0 = cat_w + 1
        utils = self._utils()
        for i, u in enumerate(utils):
            y = top + i
            if y > bottom:
                break
            self.safe(y, x0, ("• " + u["name"])[: opt_w - 2],
                      c.color_pair(5) | c.A_BOLD)
        y = top + len(utils) + 1
        if y <= bottom:
            self.safe(y, x0, "Enter → open", c.color_pair(6))

    def _draw_utils_detail(self, top, bottom, x, width):
        c = self.curses
        if width < 10:
            return
        y = top
        self.safe(y, x, "Utils", c.color_pair(10) | c.A_BOLD); y += 1
        self.safe(y, x, "one-shot maintenance actions", c.color_pair(2)); y += 2
        self.safe(y, x, "Press → or Enter to open the Utils menu.",
                  c.color_pair(4)); y += 2
        utils = self._utils()
        if not utils:
            return
        u = utils[0]
        try:
            status = u["status"]()
        except Exception:
            status = ""
        self.safe(y, x, ("─ " + u["name"] + " ")
                  + "─" * max(0, width - len(u["name"]) - 3), c.color_pair(4)); y += 1
        if status:
            self.safe(y, x, ("status: " + status)[:width], c.color_pair(6)); y += 1
        for ln in u.get("explain", [])[2:]:
            done = False
            for seg in self._wrap(ln, width):
                if y > bottom:
                    done = True
                    break
                heading = bool(ln) and not ln.startswith(" ") and ":" not in ln
                attr = c.color_pair(5) | c.A_BOLD if heading else c.color_pair(4)
                self.safe(y, x, seg[:width], attr); y += 1
            if done:
                break

    def _utils(self) -> list[dict]:
        """The Utils menu registry. Each entry is a one-shot maintenance action
        with an explanation pane; add future utilities here."""
        return [
            {
                "name": "Fix SSH",
                "explain": SSH_FIX_EXPLANATION,
                "status": lambda: (f"applied · alias in {_tilde(find_ssh_alias())}"
                                   if find_ssh_alias() else "not applied yet"),
                "run": apply_ssh_fix,
            },
        ]

    def _utils_overlay(self):
        c = self.curses
        utils = self._utils()
        sel = 0
        while True:
            self.scr.erase()
            h, w = self.dims()
            self.safe(0, 0, " utils · one-shot fixes ".ljust(w),
                      c.color_pair(1) | c.A_BOLD)
            list_w = 20
            top = 2
            for i, u in enumerate(utils):
                y = top + i
                if y >= h - 2:
                    break
                attr = c.color_pair(3) | c.A_BOLD if i == sel else c.A_NORMAL
                self.safe(y, 2, ("→ " if i == sel else "  ") + u["name"], attr)
            for y in range(top, h - 2):
                self.safe(y, list_w, "│", c.color_pair(4))

            u = utils[sel]
            dx, dw = list_w + 2, w - list_w - 3
            y = top
            try:
                status = u["status"]()
            except Exception:
                status = ""
            if status:
                self.safe(y, dx, ("status: " + status)[:dw], c.color_pair(6)); y += 1
                y += 1
            for ln in u.get("explain", []):
                for seg in self._wrap(ln, dw):
                    if y >= h - 2:
                        break
                    heading = bool(ln) and not ln.startswith(" ") and ":" not in ln
                    attr = c.color_pair(5) | c.A_BOLD if heading else c.color_pair(4)
                    self.safe(y, dx, seg[:dw], attr); y += 1
                if y >= h - 2:
                    break
            self.safe(h - 1, 0, " ↑↓ move · Enter run · Esc close ".ljust(w),
                      c.color_pair(1))
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (27,):
                return
            if ch in (c.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (c.KEY_DOWN, ord("j")):
                sel = min(len(utils) - 1, sel + 1)
            elif ch in (ord("\n"), c.KEY_ENTER, 10, 13):
                if self._confirm(f"Run '{u['name']}' now?"):
                    self._msg(f"running {u['name']}…", "info"); self.draw()
                    try:
                        ok, m = u["run"]()
                    except Exception as e:
                        ok, m = False, str(e)
                    self._utils_result(u["name"], ok, m)

    def _utils_result(self, name, ok, msg):
        c = self.curses
        self.scr.erase()
        h, w = self.dims()
        self.safe(0, 0, f" {name} ".ljust(w),
                  c.color_pair(6 if ok else 7) | c.A_BOLD)
        y = 2
        for seg in self._wrap(("✓ " if ok else "✗ ") + msg, w - 4):
            if y >= h - 2:
                break
            self.safe(y, 2, seg[:w - 3], c.color_pair(6 if ok else 7) | c.A_BOLD)
            y += 1
        self.safe(h - 1, 0, " any key to return ".ljust(w), c.color_pair(1))
        self.scr.refresh()
        self.scr.getch()

    # ── Treats (background shaders) ─────────────────────────────────────────

    def _draw_treats_menu(self, top, bottom, cat_w, opt_w):
        c = self.curses
        x0 = cat_w + 1
        active = set(enabled_treat_slugs(self.sess))
        for i, t in enumerate(TREATS):
            y = top + i
            if y > bottom:
                break
            box = "[x]" if t.slug in active else "[ ]"
            attr = (c.color_pair(6) if t.slug in active else c.color_pair(5)) | c.A_BOLD
            self.safe(y, x0, (box + " " + t.name)[: opt_w - 2], attr)
        y = top + len(TREATS) + 1
        if y <= bottom:
            self.safe(y, x0, "Enter → open", c.color_pair(6))

    def _draw_treats_detail(self, top, bottom, x, width):
        c = self.curses
        if width < 10:
            return
        y = top
        self.safe(y, x, "Treats", c.color_pair(10) | c.A_BOLD); y += 1
        self.safe(y, x, "fun animated background shaders", c.color_pair(2)); y += 2
        self.safe(y, x, "Press → or Enter to open, then Space to toggle.",
                  c.color_pair(4)); y += 2
        active = enabled_treat_slugs(self.sess)
        status = ("on: " + ", ".join(TREAT_BY_SLUG[s].name for s in active)
                  if active else "all off (default)")
        self.safe(y, x, status[:width], c.color_pair(6) if active else c.color_pair(4))

    def _commit_treats(self, slugs):
        """Toggle treats live if auto-apply, else stage. Mirrors _commit_list:
        snapshot, mutate, validate, roll back on failure."""
        snap = list(self.sess.cfg.lines)
        try:
            apply_treat_lines(self.sess, slugs)
        except OSError as e:
            self.sess.cfg.lines = snap
            return False, [str(e)]
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

    def _treats_overlay(self):
        c = self.curses
        sel = 0
        note = ""
        note_kind = "info"
        while True:
            self.scr.erase()
            h, w = self.dims()
            self.safe(0, 0, " treats · fun background shaders ".ljust(w),
                      c.color_pair(1) | c.A_BOLD)
            active = set(enabled_treat_slugs(self.sess))
            list_w = 24
            top = 2
            for i, t in enumerate(TREATS):
                y = top + i
                if y >= h - 3:
                    break
                box = "[x]" if t.slug in active else "[ ]"
                on = t.slug in active
                if i == sel:
                    attr = c.color_pair(3) | c.A_BOLD
                else:
                    attr = (c.color_pair(6) if on else c.A_NORMAL)
                self.safe(y, 2, ("→ " if i == sel else "  ") + box + " " + t.name, attr)
            for y in range(top, h - 3):
                self.safe(y, list_w, "│", c.color_pair(4))

            t = TREATS[sel]
            dx, dw = list_w + 2, w - list_w - 3
            y = top
            self.safe(y, dx, t.name, c.color_pair(10) | c.A_BOLD); y += 1
            state = "ENABLED" if t.slug in active else "off"
            self.safe(y, dx, "state: " + state,
                      (c.color_pair(6) if t.slug in active else c.color_pair(4))
                      | c.A_BOLD); y += 2
            for para in (t.desc, t.note):
                if not para:
                    continue
                for seg in self._wrap(para, dw):
                    if y >= h - 3:
                        break
                    self.safe(y, dx, seg[:dw], c.color_pair(4)); y += 1
                y += 1
            y += 0
            path = treat_shader_path(t)
            self.safe(y, dx, ("shader: " + _tilde(path))[:dw], c.color_pair(2)); y += 2
            if not CAN_RELOAD:
                self.safe(y, dx, "(reload manually on this platform)",
                          c.color_pair(8)); y += 1
            if note:
                self.safe(h - 3, 0, (" " + note).ljust(w),
                          c.color_pair({"ok": 6, "error": 7, "warn": 8}.get(
                              note_kind, 2)) | c.A_BOLD)
            self.safe(h - 1, 0,
                      " ↑↓ move · Space/Enter toggle · Esc close ".ljust(w),
                      c.color_pair(1))
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (27,):
                return
            if ch in (c.KEY_UP, ord("k")):
                sel = max(0, sel - 1); note = ""
            elif ch in (c.KEY_DOWN, ord("j")):
                sel = min(len(TREATS) - 1, sel + 1); note = ""
            elif ch in (ord(" "), ord("\n"), c.KEY_ENTER, 10, 13):
                t = TREATS[sel]
                active_now = enabled_treat_slugs(self.sess)
                turning_on = t.slug not in active_now
                # Only one treat at a time: turning one on replaces any other;
                # toggling the active one off leaves none.
                want = [t.slug] if turning_on else []
                ok, errs = self._commit_treats(want)
                if ok:
                    tag = "live" if self.sess.auto_apply else "staged"
                    note = f"{t.name} {'ON' if turning_on else 'off'} ({tag})"
                    note_kind = "ok"
                    self._msg(note, "ok")
                else:
                    note = "failed: " + (errs[0] if errs else "?")
                    note_kind = "error"

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
            "  U   update SpookiUI in place to the latest release",
            "  p   profiles — save / load / delete named configs, light↔dark",
            "  c   config check — health-check for issues (doctor)",
            "  v   utils — one-shot fixes (e.g. Fix SSH for garbled remote shells)",
            "  t   treats — toggle fun background shaders (stars, matrix, pipes)",
            "  d   show what you've changed",
            "  q   quit",
            "",
            "Options that only apply to the other OS are hidden automatically.",
            "Category icons need a Nerd Font terminal font (SPOOKIUI_ICONS=1 forces them).",
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
        print("  run `spookiui update` to upgrade in place")
    else:
        print("you're on the latest release")
    return 0


def cli_update(sess: Session, args) -> int:
    print(f"SpookiUI v{__version__} — checking for updates…")
    ok, msg = self_update()
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


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


def cli_profile(sess: Session, args) -> int:
    act = args.action
    if act == "list":
        ps = list_profiles()
        if not ps:
            print("no profiles saved", file=sys.stderr)
            return 0
        for p in ps:
            print(p)
        return 0
    if act == "toggle":
        ok, m = sess.toggle_light_dark()
        print(m, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if not args.name:
        print(f"'{act}' needs a profile name", file=sys.stderr)
        return 2
    if act == "show":
        path = profile_path(args.name)
        if not os.path.isfile(path):
            print(f"no profile named '{args.name}'", file=sys.stderr)
            return 1
        with open(path, encoding="utf-8") as fh:
            sys.stdout.write(fh.read())
        return 0
    fn = {"save": sess.save_profile, "load": sess.load_profile,
          "delete": sess.delete_profile}[act]
    ok, m = fn(args.name)
    print(m, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cli_doctor(sess: Session, args) -> int:
    findings = run_doctor(sess)
    icons = {"error": "✗", "warn": "!", "info": "·", "ok": "✓"}
    n_err = sum(1 for s, _ in findings if s == "error")
    n_warn = sum(1 for s, _ in findings if s == "warn")
    for sev, msg in findings:
        print(f"{icons.get(sev, ' ')} {msg}",
              file=sys.stderr if sev == "error" else sys.stdout)
    print(f"\n{n_err} error(s), {n_warn} warning(s)")
    return 1 if n_err else 0


def cli_fix_ssh(sess: Session, args) -> int:
    if args.explain:
        for line in SSH_FIX_EXPLANATION:
            print(line)
        return 0
    if args.check:
        existing = find_ssh_alias()
        if existing:
            print(f"ssh alias present in {_tilde(existing)}")
            return 0
        print("ssh alias not found in any shell rc "
              f"(would be added to {_tilde(ssh_rc_target())})", file=sys.stderr)
        return 1
    ok, m = apply_ssh_fix()
    print(m, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cli_treats(sess: Session, args) -> int:
    """Toggle SpookiUI's fun background shaders ('treats')."""
    action = args.action or "list"
    active = enabled_treat_slugs(sess)

    if action == "list":
        for t in TREATS:
            box = "[x]" if t.slug in active else "[ ]"
            print(f"{box} {t.slug:12} {t.desc}")
        return 0

    slugs = list(getattr(args, "name", None) or [])
    if action == "clear":
        target: list[str] = []
    else:
        unknown = [s for s in slugs if s not in TREAT_BY_SLUG]
        if unknown:
            print(f"unknown treat(s): {', '.join(unknown)}", file=sys.stderr)
            print("available: " + ", ".join(t.slug for t in TREATS), file=sys.stderr)
            return 2
        if not slugs:
            print(f"'{action}' needs at least one treat name "
                  f"(one of: {', '.join(t.slug for t in TREATS)})", file=sys.stderr)
            return 2
        if action in ("enable", "only"):
            # Only one treat runs at a time, so enabling one replaces any other.
            if len(slugs) > 1:
                print(f"only one treat can be active at a time; using '{slugs[0]}'",
                      file=sys.stderr)
            target = [slugs[0]]
        else:  # "disable"
            target = [s for s in active if s not in slugs]

    ok, m = set_treats(sess, target)
    now = enabled_treat_slugs(sess) if ok else active
    print(m + " · on: " + (", ".join(now) if now else "none"),
          file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


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

    sp = sub.add_parser("update", help="update SpookiUI in place to the latest release")
    sp.set_defaults(func=cli_update)

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

    sp = sub.add_parser("profile", help="save/load named config snapshots")
    sp.add_argument("action",
                    choices=["save", "load", "list", "delete", "toggle", "show"])
    sp.add_argument("name", nargs="?", help="profile name (not needed for list/toggle)")
    sp.set_defaults(func=cli_profile)

    sp = sub.add_parser("doctor", help="health-check the config for issues")
    sp.set_defaults(func=cli_doctor)

    sp = sub.add_parser(
        "fix-ssh",
        help="fix garbled SSH sessions (force TERM=xterm-256color via a shell alias)")
    sp.add_argument("--check", action="store_true",
                    help="report whether the alias is present; change nothing")
    sp.add_argument("--explain", action="store_true",
                    help="explain what the fix does and why, then exit")
    sp.set_defaults(func=cli_fix_ssh)

    sp = sub.add_parser(
        "treats",
        help="toggle fun background shaders (stars, matrix, pipes); all off by default")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "enable", "disable", "only", "clear"],
                    help="list (default), enable/disable/only <name…>, or clear")
    sp.add_argument("name", nargs="*", help="treat slug(s) for enable/disable/only")
    sp.set_defaults(func=cli_treats)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    parser = build_parser()
    args = parser.parse_args(argv)

    if GHOSTTY is None:
        print("error: could not find the `ghostty` executable.", file=sys.stderr)
        print("Install Ghostty or add it to your PATH.", file=sys.stderr)
        return 3

    try:
        sess = Session()
    except Exception as e:
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
