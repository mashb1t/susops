cask "susops" do
  version :latest
  sha256 :no_check

  url "https://github.com/mashb1t/susops/releases/latest/download/SusOps-#{version}-arm64.dmg"
  name "SusOps"
  desc "SSH SOCKS5 proxy manager — macOS tray app"
  homepage "https://github.com/mashb1t/susops"

  app "SusOps.app"

  zap trash: [
    "~/.susops",
    "~/Library/Application Support/SusOps",
    "~/Library/Logs/SusOps",
  ]
end
