#!/usr/bin/env python3
"""Send one command to a running tray debug server, print the JSON reply.

Usage: python tools/tray_debug.py <port> <command> [args...]
"""
import socket
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    port = int(sys.argv[1])
    line = " ".join(sys.argv[2:])
    with socket.create_connection(("127.0.0.1", port), timeout=15) as s:
        f = s.makefile("rw", encoding="utf-8")
        f.write(line + "\n")
        f.flush()
        print(f.readline().strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
