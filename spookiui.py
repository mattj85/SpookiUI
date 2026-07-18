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
`reload`, `validate`, `themes`, `fonts`, `path`. Run `./spookiui.py --help`.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

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


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )


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

        self.categories = [c for c in CATEGORY_ORDER
                           if any(o.category == c for o in sess.schema.values())]
        # options per category, sorted (overridden first, then alpha)
        self.by_cat: dict[str, list[str]] = {}
        for c in self.categories:
            names = sorted(n for n, o in sess.schema.items() if o.category == c)
            self.by_cat[c] = names

        self._swatch_cache: dict[int, int] = {}
        self._next_pair = 32
        self._init_colors()

        curses.curs_set(0)
        stdscr.keypad(True)

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
        self._search_results = sorted(self.sess.schema.keys())
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
            if q in n.lower() or q in o.doc.lower()
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
                              preview=lambda v: self._commit_scalar(opt, v, preview=True))
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
                nv = self._line_editor("add › ", "", hint=hint_add)
                if nv:
                    values.append(nv.strip()); sel = len(values) - 1
            elif ch in (ord("e"),) and values:
                nv = self._line_editor("edit › ", values[sel], hint=hint_add)
                if nv is not None:
                    values[sel] = nv.strip()
            elif ch in (ord("d"), c.KEY_DC) and values:
                del values[sel]
                sel = max(0, min(sel, len(values) - 1))

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

    def _picker(self, title, items, current, preview=None):
        """Scrollable, type-to-filter picker. Live-previews the highlighted
        item (debounced) when auto-apply is on. Returns choice or None."""
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
                self._draw_picker(title, query, filtered, sel, current)
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

    def _draw_picker(self, title, query, filtered, sel, current):
        c = self.curses
        self.scr.erase()
        h, w = self.dims()
        self.safe(0, 0, f" select {title}  ({len(filtered)}) ".ljust(w), c.color_pair(1) | c.A_BOLD)
        self.safe(1, 0, f" filter: {query}", c.color_pair(5) | c.A_BOLD)
        top = 3
        rows = h - 5
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
                self.safe(y, x + 5, item[: w - x - 6], attr)
            else:
                self.safe(y, x + 2, item[: w - x - 3], attr)
        hint = " type to filter · ↑↓ move · Enter select · Esc cancel "
        if self.sess.auto_apply:
            hint = " ● LIVE PREVIEW ·" + hint
        self.safe(h - 1, 0, hint.ljust(w), c.color_pair(1))
        self.scr.refresh()

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
        lines = [
            "SpookiUI — live Ghostty configurator",
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
            "   • numbers: ↑↓ or +/- to step, or type a value",
            "   • colors/text: type a value (#hex or name)",
            "   • lists (keybind/palette/env): a add, e edit, d delete",
            "  u             reset the selected option to its default",
            "",
            "Session",
            "  a   toggle auto-apply (live vs. staged)",
            "  s   save + reload now      r   re-trigger reload",
            "  R   revert everything to session start",
            "  d   show what you've changed",
            "  q   quit",
            "",
            "Live reload works by clicking Ghostty's 'Reload Configuration'",
            "menu item on macOS, or sending it SIGUSR2 on Linux. A timestamped",
            "backup of your config is made on the first change of each session.",
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
    for cat in cats:
        names = sorted(n for n, o in sess.schema.items() if o.category == cat)
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
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("list", help="list options (optionally by category)")
    sp.add_argument("category", nargs="?", help="category name to filter by")
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
