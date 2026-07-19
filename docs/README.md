# Docs

`guide.html` is the source for the **SpookiUI User Guide**. The rendered PDF is
not committed (it's a generated binary) — download it from the
[latest release](https://github.com/mattj85/SpookiUI/releases/latest), or
regenerate it locally with headless Chrome:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="docs/SpookiUI-User-Guide.pdf" \
  "file://$PWD/docs/guide.html"
```

On Linux use `google-chrome`/`chromium` in place of the macOS app path. Any
Chromium-based browser works.
