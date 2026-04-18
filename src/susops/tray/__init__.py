import sys


def _ensure_system_site_packages() -> None:
    """Add system site-packages to sys.path so GTK/gi are reachable from a venv."""
    import sysconfig
    system_sp = sysconfig.get_path("purelib", vars={"base": "/usr", "platbase": "/usr"})
    if system_sp and system_sp not in sys.path:
        sys.path.insert(0, system_sp)


def main():
    """Auto-detect platform and launch the appropriate tray app."""
    if sys.platform == "darwin":
        from susops.tray.mac import SusOpsMacTray
        SusOpsMacTray().run()
    else:
        _ensure_system_site_packages()
        from susops.tray.linux import SusOpsLinuxTray
        SusOpsLinuxTray().run()
