cask "susops" do
  version "3.0.0"
  sha256 "PLACEHOLDER"

  url "https://github.com/yourusername/susops/releases/download/v#{version}/SusOps-#{version}.dmg"
  name "SusOps"
  desc "SSH SOCKS5 proxy manager — macOS tray app"
  homepage "https://github.com/yourusername/susops"

  app "SusOps.app"

  zap trash: [
    "~/.susops",
    "~/Library/Application Support/SusOps",
    "~/Library/Logs/SusOps",
  ]
end
