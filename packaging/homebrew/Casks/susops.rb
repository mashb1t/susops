cask "susops" do
  # version + sha256 are rewritten on every tag by
  # scripts/update_homebrew_sha.py (called from .github/workflows/release.yml).
  version "0.0.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  url "https://github.com/mashb1t/susops/releases/download/v#{version}/SusOps-#{version}-arm64.dmg"
  name "SusOps"
  desc "SSH SOCKS5 proxy manager — macOS tray app"
  homepage "https://github.com/mashb1t/susops"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: ">= :big_sur"
  depends_on arch: :arm64

  app "SusOps.app"

  zap trash: [
    "~/.susops",
    "~/Library/Application Support/SusOps",
    "~/Library/Logs/SusOps",
  ]
end
