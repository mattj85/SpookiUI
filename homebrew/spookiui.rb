# Homebrew formula for SpookiUI.
#
# This file belongs in a Homebrew *tap* — a separate GitHub repo named
# `homebrew-spookiui` — under `Formula/spookiui.rb`. Once pushed there, install
# with:
#
#     brew install mattj85/spookiui/spookiui
#
# It is kept here in the main repo as the canonical source. On each release,
# bump `url` to the new tag and refresh `sha256` with:
#
#     curl -sL <tarball-url> | shasum -a 256
#
class Spookiui < Formula
  desc "Live configurator TUI for the Ghostty terminal"
  homepage "https://github.com/mattj85/SpookiUI"
  url "https://github.com/mattj85/SpookiUI/archive/refs/tags/v1.7.0.tar.gz"
  sha256 "6f999304a8c34181286fe6fe371b2cae1f0bc0dc0affb7fb49046b435b037a3a"
  license "MIT"
  head "https://github.com/mattj85/SpookiUI.git", branch: "main"

  depends_on "python@3.12"

  def install
    libexec.install "spookiui.py"
    (bin/"spookiui").write <<~SH
      #!/bin/bash
      exec "#{formula_opt_bin("python@3.12")}/python3.12" "#{libexec}/spookiui.py" "$@"
    SH
  end

  def caveats
    <<~EOS
      SpookiUI needs the `ghostty` binary at runtime:
        macOS:  brew install --cask ghostty
        Linux:  https://ghostty.org/docs/install

      Installed via Homebrew? Update with `brew upgrade spookiui`
      (the in-app updater defers to Homebrew for brew installs).
    EOS
  end

  test do
    assert_match "SpookiUI v", shell_output("#{bin}/spookiui --version")
  end
end
