class Susops < Formula
  desc "SSH SOCKS5 proxy manager — Python TUI + PAC server"
  homepage "https://github.com/yourusername/susops"
  url "https://github.com/yourusername/susops/archive/v3.0.0.tar.gz"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "autossh"
  depends_on "python@3.12"

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/pydantic-2.7.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "ruamel.yaml" do
    url "https://files.pythonhosted.org/packages/ruamel.yaml-0.18.6.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "psutil" do
    url "https://files.pythonhosted.org/packages/psutil-5.9.8.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "textual" do
    url "https://files.pythonhosted.org/packages/textual-0.80.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "cryptography" do
    url "https://files.pythonhosted.org/packages/cryptography-42.0.5.tar.gz"
    sha256 "PLACEHOLDER"
  end

  def install
    virtualenv_install_with_resources
    # Entry points are installed by virtualenv_install_with_resources
  end

  test do
    output = shell_output("#{bin}/susops ps 2>&1", 3)
    assert_match "stopped", output
  end
end
