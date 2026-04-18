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

import susops

from susops.core.types import ProcessState


def _manager(args=None):
    """Lazy import to avoid circular imports and slow startup when not needed."""
    from susops.facade import SusOpsManager
    verbose = getattr(args, "verbose", False)
    # Disable background threads so CLI invocations (ps, ls, test, …) do not
    # kill the reconnect daemon that may be running from a previous start.
    # start/stop/restart call detach_reconnect_monitor() explicitly when needed.
    return SusOpsManager(verbose=verbose, _enable_background_threads=False, process_name="susops-cli")


def _print_status(result) -> int:
    """Print status and return the correct exit code."""
    state = result.state
    for cs in result.connection_statuses:
        icon = "●" if cs.running else "○"
        port = f" ({cs.socks_port})" if cs.socks_port else ""
        pid = f" pid={cs.pid}" if cs.pid else ""
        print(f"  {icon} SSH [{cs.tag}]{port}{pid}")
    pac_icon = "●" if result.pac_running else "○"
    pac_port = f" ({result.pac_port})" if result.pac_port else ""
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
    if any(cs.running for cs in result.connection_statuses):
        m.detach_reconnect_monitor()
        m.detach_pac()
    return 0 if result.success else 1


def cmd_stop(args, m) -> int:
    result = m.stop(tag=args.connection, keep_ports=args.keep_ports)
    print(result.message)
    if result.success and args.connection:
        # Partial stop — respawn daemon so it no longer tries to reconnect the
        # stopped connection, but still watches any other live connections.
        m.detach_reconnect_monitor(force=True)
    return 0 if result.success else 1


def cmd_restart(args, m) -> int:
    result = m.restart(tag=getattr(args, "connection", None))
    print(result.message)
    if any(cs.running for cs in result.connection_statuses):
        m.detach_reconnect_monitor()
        m.detach_pac()
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
            for host in conn.pac_hosts:
                print(f"    - {host}")
        if conn.forwards.local:
            print(f"  local_forwards:")
            for forward in conn.forwards.local:
                print(f"    - {forward.src_addr}:{forward.src_port} → {forward.dst_addr}:{forward.dst_port}" +
                      (f" [{forward.tag}]" if forward.tag else ""))
        if conn.forwards.remote:
            print(f"  remote_forwards:")
            for forward in conn.forwards.remote:
                print(f"    - {forward.src_addr}:{forward.src_port} → {forward.dst_addr}:{forward.dst_port}" +
                      (f" [{forward.tag}]" if forward.tag else ""))
        if conn.file_shares:
            print("  file_shares:")
            for share in conn.file_shares:
                print(f"    - file_path: {share.file_path}")
                print(f"      password: {share.password}")
                print(f"      port: {share.port}")
                print(f"      stopped: {share.stopped}")
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
        if args.local or args.remote:
            # argparse fills positionals left-to-right, so 'host' grabs the
            # first integer; shift everything back when adding forwards.
            from susops.core.config import PortForward
            try:
                local_port = int(args.host) if args.local_port is None else args.local_port
                remote_port = args.local_port if args.remote_port is None and args.local_port != local_port else args.remote_port
            except (TypeError, ValueError):
                local_port = args.local_port
                remote_port = args.remote_port
            conn_tag = args.connection or m.config.connections[0].tag
            if args.local:
                fw = PortForward(
                    src_port=local_port,
                    dst_port=remote_port if remote_port is not None else local_port,
                    src_addr=args.local_addr or "localhost",
                    dst_addr=args.remote_addr or "localhost",
                    tag=args.forward_tag or "",
                )
                m.add_local_forward(conn_tag, fw)
                print(f"Added local forward {fw.src_port} → {fw.dst_port}")
            else:
                fw = PortForward(
                    src_port=remote_port if remote_port is not None else local_port,
                    dst_port=local_port,
                    src_addr=args.remote_addr or "localhost",
                    dst_addr=args.local_addr or "localhost",
                    tag=args.forward_tag or "",
                )
                m.add_remote_forward(conn_tag, fw)
                print(f"Added remote forward {fw.src_port} → {fw.dst_port}")
        else:
            m.add_pac_host(args.host, conn_tag=args.connection)
            print(f"Added PAC host '{args.host}'")
        return 0
    except (ValueError, IndexError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_rm(args, m) -> int:
    try:
        if args.local or args.remote:
            # 'host' positional grabs the port value; shift back
            try:
                port = int(args.host) if args.port is None else args.port
            except (TypeError, ValueError):
                port = args.port
            if args.local:
                m.remove_local_forward(port)
                print(f"Removed local forward on port {port}")
            else:
                m.remove_remote_forward(port)
                print(f"Removed remote forward on port {port}")
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
        conn_tag = args.connection
        if not conn_tag:
            conns = m.list_config().connections
            if not conns:
                print("Error: no connections configured", file=sys.stderr)
                return 1
            conn_tag = conns[0].tag
        info = m.share(Path(args.file), conn_tag=conn_tag, password=args.password or None, port=args.port or None)
        print(f"Sharing: {info.file_path}")
        print(f"URL:      {info.url}")
        print(f"Password: {info.password}")
        print(f"Port:     {info.port}")
        print("\nFetch with:")
        print(f"  susops fetch {info.port} {info.password}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_fetch(args, m) -> int:
    try:
        conn_tag = args.connection
        if not conn_tag:
            conns = m.list_config().connections
            if not conns:
                print("Error: no connections configured", file=sys.stderr)
                return 1
            conn_tag = conns[0].tag
        outfile = Path(args.outfile) if args.outfile else None
        result = m.fetch(port=args.port, password=args.password, conn_tag=conn_tag, outfile=outfile)
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


def cmd_config(args, m) -> int:
    import subprocess, shutil, os
    config_path = m.workspace / "config.yaml"
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    for candidate in (editor, "nano", "vim", "vi"):
        if shutil.which(candidate):
            subprocess.call([candidate, str(config_path)])
            return 0
    print(f"Config file: {config_path}", file=sys.stderr)
    print("Error: no editor found (set $EDITOR)", file=sys.stderr)
    return 1


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


def cmd_chrome_proxy_settings(args, m) -> int:
    import subprocess, shutil
    url = "chrome://settings/system"
    for browser in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "brave-browser"):
        if shutil.which(browser):
            subprocess.Popen([browser, url])
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
    parser.add_argument(
        "--version", action="version", version=f"susops {susops.__version__}",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging (events, state changes)",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # start
    p = sub.add_parser("start", help="Start SSH tunnel(s) and PAC server")
    p.set_defaults(func=cmd_start)

    # stop
    p = sub.add_parser("stop", help="Stop SSH tunnel(s) and PAC server")
    p.add_argument("--keep-ports", action="store_true", help="Preserve port assignments")
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

    # config
    sub.add_parser("config", help="Open config file in $EDITOR").set_defaults(func=cmd_config)

    # chrome / firefox
    sub.add_parser("chrome", help="Launch Chrome with PAC proxy").set_defaults(func=cmd_chrome)
    sub.add_parser("chrome-proxy-settings", help="Open Chrome proxy settings").set_defaults(func=cmd_chrome_proxy_settings)
    sub.add_parser("firefox", help="Launch Firefox with PAC proxy").set_defaults(func=cmd_firefox)

    return parser


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch a parsed command to its handler. Returns exit code."""
    if not hasattr(args, "func") or args.func is None:
        return 1
    m = _manager(args)
    return args.func(args, m)
