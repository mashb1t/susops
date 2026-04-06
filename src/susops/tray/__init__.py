import sys


def main():
    """Auto-detect platform and launch the appropriate tray app."""
    if sys.platform == "darwin":
        from susops.tray.mac import SusOpsMacTray
        SusOpsMacTray().run()
    else:
        from susops.tray.linux import SusOpsLinuxTray
        SusOpsLinuxTray().run()
