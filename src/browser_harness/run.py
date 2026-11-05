import os, sys

# Windows default stdout encoding is cp1252, which can't encode the 🟢 marker
# helpers prepend to tab titles (or anything else outside Latin-1). Force UTF-8
# so `print(page_info())` doesn't UnicodeEncodeError on Windows. Issue #124(4).
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

def _session_client():
    """Tag every call with the agent session, so the shared daemon keeps one tab per agent.
    All agents share one Chrome connection, so Chrome asks "Allow remote debugging?"
    once per daemon start, not once per agent. An explicit BU_CLIENT wins."""
    if os.environ.get("BU_CLIENT"):
        return
    import re
    for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "T3CODE_THREAD_ID"):
        value = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get(key, ""))[:60]
        if value:
            os.environ["BU_CLIENT"] = value
            return


_session_client()

# Agents drive their own Chrome, not the user's: started with remote debugging on, so Chrome never
# asks "Allow remote debugging?", and with one profile, so new windows never land in the wrong one.
AGENT_CHROME_PORT = 9333
AGENT_CHROME_PROFILE = os.path.expanduser("~/.chrome-agents")


def _agent_chrome():
    """Points the daemon at the agent Chrome and starts that Chrome, in the background, when needed.
    An explicit BU_CDP_URL or BU_CDP_WS wins (set BU_CDP_URL= to an empty value to use the user's Chrome)."""
    if "BU_CDP_URL" in os.environ or os.environ.get("BU_CDP_WS"):
        return
    os.environ["BU_CDP_URL"] = f"http://127.0.0.1:{AGENT_CHROME_PORT}"
    os.environ.setdefault("BU_NAME", "agents")  # its own daemon, apart from one on the user's Chrome
    os.environ.setdefault("BU_NO_BLANK_PAGE", "1")  # claim_tab() opens the windows; no blank one at start
    import socket, subprocess, time

    def up():
        try:
            socket.create_connection(("127.0.0.1", AGENT_CHROME_PORT), timeout=0.5).close()
            return True
        except OSError:
            return False

    if up():
        return
    # -g: do not bring it to the front. -n: a second Chrome, separate from the user's.
    subprocess.run(["open", "-gna", "Google Chrome", "--args",
                    f"--user-data-dir={AGENT_CHROME_PROFILE}", f"--remote-debugging-port={AGENT_CHROME_PORT}",
                    "--no-first-run", "--no-default-browser-check",
                    "--no-startup-window",  # no unused "New Tab" window; claim_tab() opens them
                    # Agent windows sit behind the user's windows, and a locked screen covers every window.
                    # Chrome then treats a page as hidden and slows its timers and drawing; tests stall.
                    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling"], check=True)
    for _ in range(100):
        if up():
            return
        time.sleep(0.2)
    sys.exit(f"browser-harness: the agent Chrome did not open port {AGENT_CHROME_PORT}")


if sys.platform == "darwin":
    _agent_chrome()

from .admin import (
    _version,
    ensure_daemon,
    list_cloud_profiles,
    list_local_profiles,
    print_update_banner,
    restart_daemon,
    run_doctor,
    run_setup,
    run_update,
    start_remote_daemon,
    stop_remote_daemon,
    sync_local_profile,
)
from .helpers import *

HELP = """Browser Harness

Read SKILL.md for the default workflow and examples.

Typical usage:
  browser-harness -c '
  ensure_real_tab()
  print(page_info())
  '

Helpers are pre-imported. The daemon auto-starts and connects to the running browser.

Commands:
  browser-harness --version        print the installed version
  browser-harness --doctor         diagnose install, daemon, and browser state
  browser-harness --setup          interactively attach to your running browser
  browser-harness --update [-y]    pull the latest version (agents: pass -y)
  browser-harness --reload         stop the daemon so next call picks up code changes
"""


def main():
    args = sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        print(HELP)
        return
    if args and args[0] == "--version":
        print(_version() or "unknown")
        return
    if args and args[0] == "--doctor":
        sys.exit(run_doctor())
    if args and args[0] == "--setup":
        sys.exit(run_setup())
    if args and args[0] == "--update":
        yes = any(a in {"-y", "--yes"} for a in args[1:])
        sys.exit(run_update(yes=yes))
    if args and args[0] == "--reload":
        restart_daemon()
        print("daemon stopped — will restart fresh on next call")
        return
    if args and args[0] == "--debug-clicks":
        os.environ["BH_DEBUG_CLICKS"] = "1"
        args = args[1:]
    if not args or args[0] != "-c":
        sys.exit("Usage: browser-harness -c \"print(page_info())\"")
    if len(args) < 2:
        sys.exit("Usage: browser-harness -c \"print(page_info())\"")
    print_update_banner()
    ensure_daemon()
    exec(args[1], globals())


if __name__ == "__main__":
    main()
