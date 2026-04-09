"""SusOps TUI / CLI entrypoint.

When stdout is a TTY and no subcommand is given: launch the Textual TUI.
Otherwise: dispatch the CLI command and exit with the appropriate code.
"""
from __future__ import annotations

import sys


def main() -> None:
    from susops.tui.cli import build_parser, dispatch

    parser = build_parser()
    args = parser.parse_args()

    # Launch interactive TUI if:
    # - running in a terminal (isatty)
    # - no subcommand was given
    if sys.stdout.isatty() and args.command is None:
        try:
            from susops.tui.app import SusOpsTuiApp
            app = SusOpsTuiApp()
            app.run()
        except ImportError as exc:
            print(
                f"Error: Textual is not installed ({exc}).\n"
                "Install with: pip install 'susops[tui]'\n"
                "Or use a subcommand directly, e.g.: susops ps",
                file=sys.stderr,
            )
            sys.exit(1)
    elif args.command is None:
        # Non-TTY with no subcommand: print status
        args.command = "ps"
        from susops.tui.cli import cmd_ps
        args.func = cmd_ps
        sys.exit(dispatch(args))
    else:
        sys.exit(dispatch(args))


if __name__ == "__main__":
    main()
