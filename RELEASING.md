# Releasing SpookiUI

SpookiUI's update notifier compares the `__version__` baked into `spookiui.py`
against the **latest GitHub Release** of this repo. So users only get notified
once you *publish a GitHub Release* whose tag is a higher version than the one
they're running. Cutting a release is therefore the one required step to ship an
update — pushing to `main` alone does nothing.

## Versioning

We use [semantic versioning](https://semver.org): `MAJOR.MINOR.PATCH`.

- **PATCH** — bug fixes, no behaviour change (`1.0.0 → 1.0.1`)
- **MINOR** — new options/features, backwards-compatible (`1.0.1 → 1.1.0`)
- **MAJOR** — breaking changes to CLI flags, config handling, etc. (`1.1.0 → 2.0.0`)

The version lives in exactly one place — the `__version__` constant near the top
of `spookiui.py`:

```python
__version__ = "1.0.0"
```

The comparison is numeric (`1.10.0` is correctly newer than `1.9.0`), and any
`-suffix` (e.g. `-beta`) is ignored when comparing.

## Cutting a release

1. **Bump the version** in `spookiui.py`:

   ```bash
   # e.g. going to 1.1.0 — edit __version__ = "1.1.0" in spookiui.py
   ./spookiui.py --version          # sanity-check it prints the new number
   python3 -m py_compile spookiui.py
   ```

2. **Commit** the bump (and update the docs/changelog if you keep one):

   ```bash
   git add spookiui.py
   git commit -m "Release v1.1.0"
   git push origin main
   ```

3. **Tag and publish a GitHub Release.** The tag drives the notifier, so it must
   match the version. Prefix with `v`; the app strips it when comparing.

   With the [`gh` CLI](https://cli.github.com) (recommended — creates the tag and
   the release in one step):

   ```bash
   gh release create v1.1.0 \
       --title "v1.1.0" \
       --notes "What changed in this release…"
   ```

   Or via the web UI: **Releases → Draft a new release →** create tag `v1.1.0`
   on `main`, add notes, **Publish**.

4. **Verify** the notifier sees it (the result is cached for a day, so force a
   fresh check):

   ```bash
   ./spookiui.py version
   # → prints your current version and, if it's older than the release,
   #   "a newer release is available: v1.1.0"
   ```

That's it. Users will see a `⬆ UPDATE v1.1.0` badge in the TUI header the next
time their daily check runs, and `spookiui version` will report it on demand.

## Notes

- **Keep the tag and `__version__` in sync.** The tag is what users compare
  against; `__version__` is what they compare *from*. A release tagged `v1.1.0`
  while `spookiui.py` still says `1.0.0` means the shipped copy will forever
  think an update is available.
- **Pre-releases** (`v1.1.0-rc1`) are treated by the notifier as version
  `1.1.0`. Mark them as *pre-release* in GitHub if you don't want them surfaced
  as the "latest" release (the API's `releases/latest` excludes pre-releases).
- The check hits `api.github.com/repos/<owner>/<repo>/releases/latest`
  unauthenticated (60 req/hour per IP). SpookiUI caches results for 24h under
  `$XDG_CACHE_HOME/spookiui/` and users can disable it with
  `SPOOKIUI_NO_UPDATE_CHECK=1`.
- The repo slug the app checks is the `GITHUB_REPO` constant in `spookiui.py`
  (`mattj85/SpookiUI`). Update it there if the repo ever moves.
