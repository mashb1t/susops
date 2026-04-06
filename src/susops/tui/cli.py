"""Non-interactive CLI for SusOps.

Used when stdout is not a TTY (e.g., scripts, cron, tray app subprocess calls).
Provides all susops commands with text output and semantic exit codes:
  0 = success / all running
  1 = error
  2 = partial (some services stopped)
  3 = all stopped
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from susops.core.types import ProcessState


def _manager():
    """Lazy import to avoid circular imports and slow startup when not needed."""
    from susops.facade import SusOpsManager
    return SusOpsManager()


def _print_status(result) -> int:
    """Print status and return the correct exit code."""
    state = result.state
    for cs in result.connection_statuses:
        icon = "●" if cs.running else "○"
        port = f" (:{cs.socks_port})" if cs.socks_port else ""
        pid = f" pid={cs.pid}" if cs.pid else ""
        print(f"  {icon} SSH [{cs.tag}]{port}{pid}")
    pac_icon = "●" if result.pac_running else "○"
    pac_port = f" (:{result.pac_port})" if result.pac_port else ""
    print(f"  {pac_icon} PAC server{pac_port}")
    print(f"State: {state.value}")

    if state == ProcessState.RUNNING:
        return 0
    if state == ProcessState.STOPPED_PARTIALLY:
        return 2
    if state == ProcessState.STOPPED:
        return 3
    return 1


def cmd_start(args, m) -> int:
    result = m.start(tag=args.connection)
    print(result.message)
    for cs in result.connection_statuses:
        icon = "+" if cs.running else "!"
        print(f"  [{icon}] {cs.tag}" + (f" port {cs.socks_port}" if cs.socks_port else ""))
    return 0 if result.success else 1


def cmd_stop(args, m) -> int:
    result = m.stop(keep_ports=args.keep_ports, force=args.force)
    print(result.message)
    return 0 if result.success else 1


def cmd_restart(args, m) -> int:
    result = m.restart(tag=getattr(args, "connection", None))
    print(result.message)
    return 0 if result.success else 1


def cmd_ps(args, m) -> int:
    result = m.status()
    return _print_status(result)


def cmd_ls(args, m) -> int:
    config = m.list_config()
    print(f"pac_server_port: {config.pac_server_port}")
    for conn in config.connections:
        print(f"\nconnection: {conn.tag}")
        print(f"  ssh_host: {conn.ssh_host}")
        print(f"  socks_port: {conn.socks_proxy_port}")
        if conn.pac_hosts:
            print(f"  pac_hosts:")
            for h in conn.pac_hosts:
                print(f"    - {h}")
        if conn.forwards.local:
            print(f"  local_forwards:")
            for f in conn.forwards.local:
                print(f"    - {f.src_addr}:{f.src_port} → {f.dst_addr}:{f.dst_port}" +
                      (f" [{f.tag}]" if f.tag else ""))
        if conn.forwards.remote:
            print(f"  remote_forwards:")
            for f in conn.forwards.remote:
                print(f"    - {f.src_addr}:{f.src_port} → {f.dst_addr}:{f.dst_port}" +
                      (f" [{f.tag}]" if f.tag else ""))
    return 0


def cmd_add_connection(args, m) -> int:
    try:
        conn = m.add_connection(args.tag, args.ssh_host, socks_port=args.socks_port or 0)
        print(f"Added connection '{conn.tag}' → {conn.ssh_host}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_rm_connection(args, m) -> int:
    try:
        m.remove_connection(args.tag)
        print(f"Removed connection '{args.tag}'")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_add(args, m) -> int:
    try:
        if args.local:
            from susops.core.config import PortForward
            fw = PortForward(
                src_port=args.local_port,
                dst_port=args.remote_port,
                src_addr=args.local_addr or "localhost",
                dst_addr=args.remote_addr or "localhost",
                tag=args.forward_tag or "",
            )
            m.add_local_forward(args.connection or m.config.connections[0].tag, fw)
            print(f"Added local forward :{args.local_port} → :{args.remote_port}")
        elif args.remote:
            from susops.core.config import PortForward
            fw = PortForward(
                src_port=args.remote_port,
                dst_port=args.local_port,
                src_addr=args.remote_addr or "localhost",
                dst_addr=args.local_addr or "localhost",
                tag=args.forward_tag or "",
            )
            m.add_remote_forward(args.connection or m.config.connections[0].tag, fw)
            print(f"Added remote forward :{args.remote_port} → :{args.local_port}")
        else:
            m.add_pac_host(args.host, conn_tag=args.connection)
            print(f"Added PAC host '{args.host}'")
        return 0
    except (ValueError, IndexError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_rm(args, m) -> int:
    try:
        if args.local:
            m.remove_local_forward(args.port)
            print(f"Removed local forward on port {args.port}")
        elif args.remote:
            m.remove_remote_forward(args.port)
            print(f"Removed remote forward on port {args.port}")
        else:
            m.remove_pac_host(args.host)
            print(f"Removed PAC host '{args.host}'")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_test(args, m) -> int:
    if args.all:
        results = m.test_all()
        ok = all(r.success for r in results)
        for r in results:
            icon = "✓" if r.success else "✗"
            latency = f" ({r.latency_ms:.0f}ms)" if r.latency_ms else ""
            print(f"  {icon} {r.target}{latency}: {r.message}")
        return 0 if ok else 1
    else:
        result = m.test(args.target)
        icon = "✓" if result.success else "✗"
        latency = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
        print(f"{icon} {result.target}{latency}: {result.message}")
        return 0 if result.success else 1


def cmd_share(args, m) -> int:
    try:
        info = m.share(Path(args.file), password=args.password or None, port=args.port or None)
        print(f"Sharing: {info.file_path}")
        print(f"URL:      {info.url}")
        print(f"Password: {info.password}")
        print(f"Port:     {info.port}")
        print("\nFetch with:")
        print(f"  susops fetch {info.port} {info.password}")
        return 0
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_fetch(args, m) -> int:
    try:
        outfile = Path(args.outfile) if args.outfile else None
        result = m.fetch(port=args.port, password=args.password, outfile=outfile)
        print(f"Downloaded to: {result}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_reset(args, m) -> int:
    if not args.force:
        answer = input("This will reset the entire workspace. Continue? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return 1
    m.reset()
    print("Workspace reset.")
    return 0


def cmd_chrome(args, m) -> int:
    import subprocess, shutil
    pac_url = m.get_pac_url()
    if not pac_url:
        print("Error: PAC server is not running", file=sys.stderr)
        return 1
    for browser in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "brave-browser"):
        if shutil.which(browser):
            subprocess.Popen([browser, f"--proxy-pac-url={pac_url}"])
            return 0
    print("Error: No Chrome/Chromium browser found", file=sys.stderr)
    return 1


def cmd_firefox(args, m) -> int:
    import subprocess, shutil
    pac_url = m.get_pac_url()
    if not pac_url:
        print("Error: PAC server is not running", file=sys.stderr)
        return 1
    profile_dir = m.workspace / "firefox_profile"
    profile_dir.mkdir(exist_ok=True)
    user_js = profile_dir / "user.js"
    user_js.write_text(
        f'user_pref("network.proxy.type", 2);\n'
        f'user_pref("network.proxy.autoconfig_url", "{pac_url}");\n'
        f'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");\n'
    )
    if shutil.which("firefox"):
        subprocess.Popen(["firefox", "-profile", str(profile_dir), "-no-remote"])
        return 0
    print("Error: Firefox not found", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="susops",
        description="SusOps — SSH SOCKS5 proxy manager",
    )
    parser.add_argument(
        "-c", "--connection", metavar="TAG",
        help="Target a specific connection by tag",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # start
    p = sub.add_parser("start", help="Start SSH tunnel(s) and PAC server")
    p.set_defaults(func=cmd_start)

    # stop
    p = sub.add_parser("stop", help="Stop SSH tunnel(s) and PAC server")
    p.add_argument("--keep-ports", action="store_true", help="Preserve port assignments")
    p.add_argument("--force", action="store_true", help="Force kill (SIGKILL)")
    p.set_defaults(func=cmd_stop)

    # restart
    p = sub.add_parser("restart", help="Restart tunnel(s)")
    p.set_defaults(func=cmd_restart)

    # ps
    p = sub.add_parser("ps", help="Show process status")
    p.set_defaults(func=cmd_ps)

    # ls
    p = sub.add_parser("ls", help="List all config")
    p.set_defaults(func=cmd_ls)

    # add-connection
    p = sub.add_parser("add-connection", help="Add a new SSH connection")
    p.add_argument("tag", help="Unique identifier for this connection")
    p.add_argument("ssh_host", metavar="SSH_HOST", help="SSH host string (user@host)")
    p.add_argument("socks_port", metavar="SOCKS_PORT", type=int, nargs="?", default=0,
                   help="SOCKS port (0 = auto-assign)")
    p.set_defaults(func=cmd_add_connection)

    # rm-connection
    p = sub.add_parser("rm-connection", help="Remove an SSH connection")
    p.add_argument("tag", help="Connection tag to remove")
    p.set_defaults(func=cmd_rm_connection)

    # add (PAC host or port forward)
    p = sub.add_parser("add", help="Add PAC host or port forward")
    p.add_argument("host", nargs="?", help="Hostname, wildcard, or CIDR")
    p.add_argument("-l", "--local", action="store_true", help="Add local forward")
    p.add_argument("-r", "--remote", action="store_true", help="Add remote forward")
    p.add_argument("local_port", type=int, nargs="?", metavar="LOCAL_PORT")
    p.add_argument("remote_port", type=int, nargs="?", metavar="REMOTE_PORT")
    p.add_argument("forward_tag", nargs="?", metavar="TAG", help="Forward label")
    p.add_argument("local_addr", nargs="?", metavar="LOCAL_ADDR", default=None)
    p.add_argument("remote_addr", nargs="?", metavar="REMOTE_ADDR", default=None)
    p.set_defaults(func=cmd_add)

    # rm (PAC host or port forward)
    p = sub.add_parser("rm", help="Remove PAC host or port forward")
    p.add_argument("host", nargs="?", help="Hostname to remove")
    p.add_argument("-l", "--local", action="store_true")
    p.add_argument("-r", "--remote", action="store_true")
    p.add_argument("port", type=int, nargs="?", metavar="PORT")
    p.set_defaults(func=cmd_rm)

    # test
    p = sub.add_parser("test", help="Test connectivity")
    p.add_argument("target", nargs="?", default="", help="Hostname or port to test")
    p.add_argument("--all", action="store_true", help="Test all PAC hosts")
    p.set_defaults(func=cmd_test)

    # share
    p = sub.add_parser("share", help="Share an encrypted file")
    p.add_argument("file", help="File to share")
    p.add_argument("password", nargs="?", default=None, help="Encryption password (auto-generated if omitted)")
    p.add_argument("port", type=int, nargs="?", default=0, help="Port to listen on (0 = auto)")
    p.set_defaults(func=cmd_share)

    # fetch
    p = sub.add_parser("fetch", help="Fetch an encrypted shared file")
    p.add_argument("port", type=int, help="Port the share is on")
    p.add_argument("password", help="Decryption password")
    p.add_argument("outfile", nargs="?", default=None, help="Output file path")
    p.set_defaults(func=cmd_fetch)

    # reset
    p = sub.add_parser("reset", help="Reset workspace (destructive)")
    p.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    p.set_defaults(func=cmd_reset)

    # chrome / firefox
    sub.add_parser("chrome", help="Launch Chrome with PAC proxy").set_defaults(func=cmd_chrome)
    sub.add_parser("firefox", help="Launch Firefox with PAC proxy").set_defaults(func=cmd_firefox)

    return parser


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch a parsed command to its handler. Returns exit code."""
    if not hasattr(args, "func") or args.func is None:
        return 1
    m = _manager()
    return args.func(args, m)
