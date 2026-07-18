# SpookiUI

A **live configurator for the [Ghostty](https://ghostty.org) terminal**. Browse
and edit *every* option Ghostty supports from an interactive terminal UI, and
watch your changes apply **live** — when you run it inside a Ghostty window, the
very terminal you're in repaints as you edit.

![SpookiUI screenshot](demo/spookyui.png)

```
./spookiui.py
```

<sub>Requires Python 3.8+ (standard library only — no pip installs) and the
`ghostty` binary on your `PATH` (or in `/Applications/Ghostty.app`).</sub>

---

## Installation

SpookiUI has **no third-party Python dependencies** — it's a single script that
runs on the Python 3.8+ standard library. You can run it straight from the repo:

```bash
git clone https://github.com/mattj85/SpookiUI.git
cd SpookiUI
./spookiui.py
```

Or run the installer (macOS & Linux) to check prerequisites and put a `spookiui`
command on your `PATH`:

```bash
./install.sh                     # installs to ~/.local/bin
PREFIX=/usr/local ./install.sh   # system-wide (may need sudo)
```

The installer verifies Python 3.8+, checks for the `ghostty` binary (warning
with install hints if it's missing), and symlinks `spookiui.py` into your bin
directory. After it runs, `spookiui` and `spookiui --help` work from anywhere.

## Why it exists

Ghostty is configured through a plain-text file (`~/.config/ghostty/config`) and
**cannot auto-reload on file change** — you have to trigger a reload yourself.
SpookiUI closes that loop: it writes the config file *and* triggers a Ghostty
reload for you — clicking the **Reload Configuration** menu item on macOS, or
sending the running process the **SIGUSR2** signal it reloads on under Linux —
so editing feels live. It runs on both macOS and Linux.

Every option is discovered **dynamically** from your installed Ghostty
(`ghostty +show-config --default --docs`), so the tool always matches your
version — nothing is hard-coded. On this machine that's ~200 options across 13
categories. Options that only apply to the *other* operating system (macOS-only
settings on Linux, GTK/X11 settings on macOS) are **hidden automatically** so you
only ever see what's relevant to the machine you're on.

## The live loop

```
 you edit a value ─▶ SpookiUI writes the config file
                          │
                          ├─▶ validates it with `ghostty +validate-config`
                          │       (an invalid value is rejected & rolled back)
                          │
                          └─▶ reloads Ghostty ─▶ your terminal repaints
                                  (macOS: "Reload Configuration" menu item via
                                   AppleScript · Linux: SIGUSR2 to the process)
```

- **Safe:** every change is validated by Ghostty itself before it's saved. Bad
  values never reach your config.
- **Reversible:** a dated backup (`config.spookiui.YYYYMMDD.bak`) is made on the
  first change of the day, the TUI can revert an entire session with `R`, and you
  can wipe the config back to Ghostty's built-in defaults with `X` (a backup is
  still kept).
- **Live preview:** while picking a theme, font, or enum value, each highlighted
  option is applied as you scroll — cancel and it snaps back to where you were.

## Live reload by platform

Ghostty can't watch its config file for changes, so SpookiUI triggers the reload
for you. How that happens — and what it needs — depends on your OS:

| Platform | How the reload fires | Requirements |
| --- | --- | --- |
| **macOS** | Clicks the **Reload Configuration** menu item via AppleScript (`osascript`) | Ghostty must be running; your terminal needs **Accessibility** permission (*System Settings → Privacy & Security → Accessibility*). Ghostty is located on `PATH` or at `/Applications/Ghostty.app`. |
| **Linux** | Sends **`SIGUSR2`** to the running Ghostty process(es), which Ghostty reloads on | Ghostty must be running; `pgrep` (from `procps`/`procps-ng`, present on essentially every distro) is used to find it. No extra permission needed. Works on any distribution — detection is generic (`sys.platform`), with no distro-specific code. |
| **Other** | *No auto-reload* — the file is still written and validated | Trigger your own `reload_config` keybind in Ghostty to apply. |

On **Linux**, Ghostty is found via `PATH` (`shutil.which`), falling back to
`/usr/bin/ghostty` and `/usr/local/bin/ghostty`. Only Python 3.8+ (standard
library) and the `ghostty` binary are required; live reload additionally needs
`pgrep` and a running Ghostty instance.

If a reload can't be triggered (Ghostty isn't running, missing permission, or an
unsupported platform), your change is **still written and validated safely** —
SpookiUI just tells you to reload manually. A few options (e.g. `language`) can't
be applied without a restart at all; the UI flags these as *needs restart* /
*new windows only* so there are no surprises.

## The TUI

```
 SpookiUI · live Ghostty configurator          AUTO-APPLY:ON · live
 Colors & Theme    │ ● theme            Catppuccin Mocha │ theme
 Font              │   background        #1e1e2e          │ type: theme
 Cursor            │   foreground        #cdd6f4          │ value: Catppuccin…
 Window            │ ● background-opacity 0.95            │ ─ docs ───────────
 …                 │   …                                  │ Set the color …
```

| Key | Action |
| --- | --- |
| `↑`/`↓` or `j`/`k` | move · `Tab` switch pane |
| `→`/`Enter` | into options / **edit** the selected option |
| `←` | back to categories |
| `/` | search all options by name or documentation |
| `u` | reset the selected option to its default |
| `a` | toggle **auto-apply** (live ↔ staged) |
| `s` | save + reload now · `r` re-trigger reload |
| `R` | revert everything to session start |
| `X` | wipe config & restore **all** Ghostty defaults (backup kept) |
| `U` | update SpookiUI in place to the latest release |
| `p` | **profiles** — save / load / delete named configs · `t` toggles light↔dark |
| `c` | **config check** (doctor) — health-check for issues |
| `d` | show everything you've changed |
| `?` | help · `q` quit |

Editors are typed to each option:

- **booleans** toggle instantly
- **enums / font** open a searchable picker with live preview, listing *every*
  valid choice Ghostty documents (e.g. all 11 `macos-icon` styles)
- **theme** opens the picker with a **live colour card** for the highlighted
  theme — its 16-colour palette and a foreground-on-background sample, rendered
  right beside the list so you see a theme before applying it
- **bounded numbers** (opacity, `minimum-contrast`, …) open a **visual slider** —
  `←`/`→` to adjust, `PgUp`/`PgDn` for larger jumps, `Home`/`End` for the ends,
  all previewed live
- **other numbers** step with `↑`/`↓` or `+`/`-`, or type a value
- **colors / text** take a typed value (`#rrggbb` or a named color); colours show
  a swatch, and colour options preview the active palette in the detail pane
- **keybindings** open a **guided builder** — toggle modifiers (`super`/`ctrl`/
  `alt`/`shift`, where `super` is ⌘ on macOS), press or pick the key, and choose
  the action from Ghostty's own action list; the result is validated before it's
  added
- **other lists** (`palette`, `env`, font fallbacks, …) get an add/edit/delete
  editor

**Auto-apply off** stages your edits in memory instead of touching disk; press
`s` to write + reload them all at once.

On macOS you can also restyle the **app icon** from here: pick a `macos-icon`
style (`official`, `blueprint`, `chalkboard`, `microchip`, `glass`,
`holographic`, `paper`, `retro`, `xray`, …) and, with `custom-style`, tweak
`macos-icon-frame` plus the `macos-icon-ghost-color` / `macos-icon-screen-color`
(which get live swatches). See the icon gallery at
<https://noahskelton.github.io/ghostty-icons/>.

## Profiles & the config doctor

**Profiles** are named snapshots of your whole config — press `p` in the TUI (or
use `spookiui profile …`) to save the current setup, then load it back later.
Save a `light` and a `dark` profile and the `t` key (or `spookiui profile
toggle`) flips between them instantly. Loading a profile is validated and backs
up your current config first, like every other change. Profiles live in
`$XDG_DATA_HOME/spookiui/profiles` (`~/.local/share/…` by default), outside
Ghostty's own config dir so it never reads them.

**`spookiui doctor`** (or `c` in the TUI) health-checks your config and reports:
invalid settings, unknown/typo'd options, options set more than once (dead
lines), settings that just repeat a default, and keybind triggers bound twice or
shadowing a Ghostty default. Findings are grouped by severity; it exits non-zero
when there are errors, so it drops cleanly into a pre-commit hook for dotfiles.

## Scriptable CLI

Everything the TUI does is also available non-interactively:

```bash
./spookiui.py list [category]      # list options (＊ = changed from default)
./spookiui.py list [category] --all # include options for the other OS
./spookiui.py get   <key>          # print an option's current value
./spookiui.py doc   <key>          # show an option's documentation + choices
./spookiui.py set   <key> <value>… # set (writes + reloads live); repeat value for lists
./spookiui.py set   <key> <v> --no-reload   # write without reloading
./spookiui.py reset --yes          # clear config & restore all Ghostty defaults (backup kept)
./spookiui.py version              # print version & check GitHub for a newer release
./spookiui.py update               # update in place to the latest release (git pull or download)
./spookiui.py profile save <name>  # snapshot the current config as a named profile
./spookiui.py profile load <name>  # apply a saved profile (validated, backed up)
./spookiui.py profile list         # list saved profiles  (also: show / delete / toggle)
./spookiui.py profile toggle       # flip between the 'light' and 'dark' profiles
./spookiui.py doctor               # health-check the config (duplicates, unknown keys, keybind clashes…)
./spookiui.py reload               # trigger a live reload
./spookiui.py validate             # validate the current config
./spookiui.py themes               # list installed themes
./spookiui.py fonts                # list monospace font families
./spookiui.py path                 # print the config file in use
```

Examples:

```bash
./spookiui.py set theme "Catppuccin Latte"
./spookiui.py set font-size 15
./spookiui.py set font-family "JetBrains Mono" "Symbols Nerd Font"   # primary + fallback
./spookiui.py doc background-opacity
```

## Notes & limitations

- **Live reload works on macOS and Linux** (and degrades safely elsewhere) — see
  [Live reload by platform](#live-reload-by-platform) above for the per-OS
  mechanism and requirements.
- Edits to single-value options are made **in place**, preserving your file's
  comments and layout. New options and list options are written under a
  clearly-marked `# added by SpookiUI` section.
- The config path is auto-detected (`$XDG_CONFIG_HOME/ghostty/config`, then
  `~/.config/ghostty/config`, then the macOS app-support path).

## Updates

On startup SpookiUI quietly checks GitHub for a newer release. If one exists, the
TUI shows a `⬆ UPDATE vX.Y.Z` badge in the header (and *press `U` to update* on the
status line); the help screen (`?`) always shows your current version. Run
`spookiui version` any time to check on demand.

The check is **best-effort and non-blocking** — it runs on a background thread,
times out quickly, and stays silent if you're offline or GitHub is unreachable.
The result is cached for a day (under `$XDG_CACHE_HOME/spookiui/`) so it never
hammers the API. To turn it off entirely, set `SPOOKIUI_NO_UPDATE_CHECK=1`.

### Updating in place — no `git pull` needed

Press `U` in the TUI, or run `spookiui update`. No update server is involved —
GitHub is the source, and SpookiUI is a single file, so updating just swaps that
file:

- **Git checkout** (the default `install.sh` layout) → it runs `git pull` for you.
- **Standalone copy** → it downloads the latest release's `spookiui.py`, *verifies
  it compiles*, then atomically replaces the file (keeping a `.prev` backup). A
  truncated or bad download can never leave you with a broken tool.
- **No write permission** (e.g. installed system-wide as root) → it tells you the
  exact command to run instead of failing silently.

Restart SpookiUI afterwards to run the new version.

Maintainers: notifications and updates only pick up a version once a matching
**GitHub Release** is published — see [`RELEASING.md`](RELEASING.md) for the
bump-and-release flow.
