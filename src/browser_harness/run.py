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
MAIN_AGENT_CHROME_PROFILE = os.path.expanduser("~/.chrome-agents")
CHECKOUT_CHROME_DIR = os.path.expanduser("~/.chrome-agents-checkouts")


def _checkout_label():
    """The worktree folder name when the call runs inside an agent worktree (.aikit/worktrees/<name>
    or .claude/worktrees/<name>), else None."""
    import subprocess
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=3).stdout.strip()
    except Exception:
        return None
    parent = os.path.basename(os.path.dirname(top))
    grand = os.path.basename(os.path.dirname(os.path.dirname(top)))
    # An agent's calls must reach one Chrome from any folder. On 2026-11-07 a QA agent ran some calls
    # from its scratchpad and some from its worktree, reached two Chromes and lost its tab. So a call
    # inside a worktree records the worktree for this client, and a call outside uses that record.
    client = os.environ.get("BU_CLIENT", "")
    record = os.path.join(CHECKOUT_CHROME_DIR, "clients", client) if client else None
    if parent == "worktrees" and grand in (".aikit", ".claude"):
        if record:
            try:
                os.makedirs(os.path.dirname(record), exist_ok=True)
                open(record, "w").write(top + "\n")
            except OSError:
                pass
    elif record and os.path.exists(record) and sys.argv[1:2] != ["--agent-chrome"]:
        # Admin commands (--agent-chrome restart, headed, headless) act on the Chrome of the folder they
        # run in, never on a remembered one.
        try:
            top = open(record).read().strip()
        except OSError:
            return None
        if not os.path.isdir(top):
            return None
    else:
        return None
    os.environ["BU_AGENT_CHECKOUT"] = top
    return os.path.basename(top)


# Each agent worktree gets its own agent Chrome (own port, profile and daemon), so a page that hangs
# or crashes Chrome stops only that worktree's agents, and shows which ticket caused it (on
# 2026-11-07 one shared Chrome hung and crashed many times and stopped every agent). Calls outside a
# worktree use the main agent Chrome. BU_AGENT_CHROME_SHARED=1 forces the main one.
_LABEL = None if (os.environ.get("BU_AGENT_CHROME_PORT") or os.environ.get("BU_AGENT_CHROME_PROFILE")
                  or os.environ.get("BU_AGENT_CHROME_SHARED")) else _checkout_label()
if _LABEL:
    import zlib
    os.environ["BU_AGENT_CHROME_PORT"] = str(9400 + zlib.crc32(_LABEL.encode()) % 500)
    os.environ["BU_AGENT_CHROME_PROFILE"] = os.path.join(CHECKOUT_CHROME_DIR, _LABEL)
    os.environ.setdefault("BU_NAME", "agents-" + _LABEL)

AGENT_CHROME_PORT = int(os.environ.get("BU_AGENT_CHROME_PORT", "9333"))
AGENT_CHROME_PROFILE = os.path.expanduser(os.environ.get("BU_AGENT_CHROME_PROFILE", "~/.chrome-agents"))
# The agent Chrome runs headless (no windows) unless this file says "headed". Headless never takes
# focus and other apps cannot read its pages. `browser-harness --agent-chrome headed` switches.
# One mode for all agent Chromes: the main profile's.
AGENT_CHROME_MODE_FILE = (MAIN_AGENT_CHROME_PROFILE if _LABEL else AGENT_CHROME_PROFILE) + "-mode"
CHROME_APP = "/Applications/Google Chrome.app"
SIGN_IN_URL = "https://master.observepointstaging.com/login"
# Agents run a renamed copy of Chrome ("Agent Chrome", its own bundle id). With the real app, macOS
# treats the agent Chrome as the user's Chrome when theirs is closed: the Dock icon then opens the
# agent Chrome. The copy cannot read the real Chrome's keychain key, so it uses --use-mock-keychain
# and keeps its own staging sign-in. _agent_chrome_app() makes the copy again when Chrome updates.
AGENT_CHROME_APP = os.path.expanduser("~/Applications/Agent Chrome.app")
CHROME_BIN = AGENT_CHROME_APP + "/Contents/MacOS/Google Chrome"


def _chrome_version(app):
    import plistlib
    try:
        return plistlib.load(open(app + "/Contents/Info.plist", "rb"))["CFBundleShortVersionString"]
    except (OSError, KeyError):
        return None


def _agent_chrome_app():
    """Makes ~/Applications/Agent Chrome.app from Chrome when it is missing or older than Chrome.
    A copy on the same disk is a clone: fast, and it takes almost no space."""
    import shutil, subprocess
    if os.path.exists(CHROME_BIN) and _chrome_version(AGENT_CHROME_APP) == _chrome_version(CHROME_APP):
        return
    tmp = AGENT_CHROME_APP + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.dirname(AGENT_CHROME_APP), exist_ok=True)
    subprocess.run(["cp", "-cR", CHROME_APP, tmp], check=True)
    plist = tmp + "/Contents/Info.plist"
    subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Set :CFBundleIdentifier com.jackparker.agent-chrome",
                    "-c", "Set :CFBundleName Agent Chrome", "-c", "Set :CFBundleDisplayName Agent Chrome",
                    plist], check=True, stdout=subprocess.DEVNULL)
    # The new name breaks Google's signature, and macOS kills an app with a broken one. Sign the outer
    # app again, without --deep: the framework inside keeps Google's signature.
    subprocess.run(["xattr", "-cr", tmp], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", tmp], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(AGENT_CHROME_APP, ignore_errors=True)
    os.rename(tmp, AGENT_CHROME_APP)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
                    "/Support/lsregister", "-f", AGENT_CHROME_APP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _agent_chrome_mode():
    try:
        return "headed" if open(AGENT_CHROME_MODE_FILE).read().strip() == "headed" else "headless"
    except OSError:
        return "headless"


def _agent_chrome_pids():
    """Main process ids of the agent Chrome (the one that runs with its profile), with their args."""
    import subprocess
    out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
    flag = f"--user-data-dir={AGENT_CHROME_PROFILE} "  # the space: ~/.chrome-agents is a prefix of the worktree profiles
    return {int(l.split(None, 1)[0]): l.split(None, 1)[1] for l in out.splitlines()
            if flag in l and "--type=" not in l and l.split(None, 1)[1].startswith((CHROME_BIN, CHROME_APP))}  # CHROME_APP: one started before the copy


def _agent_env():
    """Points the daemon at the agent Chrome. Returns False when the caller chose another browser:
    an explicit BU_CDP_URL or BU_CDP_WS wins (set BU_CDP_URL= to an empty value to use the user's Chrome)."""
    if "BU_CDP_URL" in os.environ or os.environ.get("BU_CDP_WS"):
        return False
    os.environ["BU_CDP_URL"] = f"http://127.0.0.1:{AGENT_CHROME_PORT}"
    os.environ.setdefault("BU_NAME", "agents")  # its own daemon, apart from one on the user's Chrome
    os.environ.setdefault("BU_NO_BLANK_PAGE", "1")  # claim_tab() opens the windows; no blank one at start
    return True


def _copy_sign_in():
    """A worktree's agent Chrome starts with the staging sign-in of the main agent Chrome: its
    cookies and local storage, copied at each start (Chrome is not running then)."""
    import shutil
    src = os.path.join(MAIN_AGENT_CHROME_PROFILE, "Default")
    dst = os.path.join(AGENT_CHROME_PROFILE, "Default")
    os.makedirs(dst, exist_ok=True)
    # Copy only the staging cookies, never the Google sign-in. Google ends the whole Google session
    # when its rotating sign-in cookies show up in more than one browser: on 2026-11-09 Jack signed in
    # at 9:02 AM, three worktree Chromes got a copy at 9:04 to 9:06 AM, and at 9:09 AM Google was
    # signed out in every Chrome. Turning off device-bound sign-in (see the flags below) did not stop
    # it. Worktree Chromes do not need Google: the staging sign-in is the accessKey cookie.
    import sqlite3
    try:
        os.remove(os.path.join(dst, "Cookies-journal"))
    except OSError:
        pass
    if os.path.exists(os.path.join(src, "Cookies")):
        shutil.copy2(os.path.join(src, "Cookies"), os.path.join(dst, "Cookies"))
        db = sqlite3.connect(os.path.join(dst, "Cookies"))
        db.execute("DELETE FROM cookies WHERE host_key NOT LIKE '%observepointstaging.com'")
        db.commit()
        db.close()
    if os.path.isdir(os.path.join(src, "Local Storage")):
        shutil.rmtree(os.path.join(dst, "Local Storage"), ignore_errors=True)
        shutil.copytree(os.path.join(src, "Local Storage"), os.path.join(dst, "Local Storage"))
    # The watchdog stops this Chrome and deletes its profile once the worktree is gone.
    open(os.path.join(AGENT_CHROME_PROFILE, "checkout.txt"), "w").write(os.environ.get("BU_AGENT_CHECKOUT", "") + "\n")
    if os.path.exists(os.path.join(MAIN_AGENT_CHROME_PROFILE, "Local State")):
        shutil.copy2(os.path.join(MAIN_AGENT_CHROME_PROFILE, "Local State"),
                     os.path.join(AGENT_CHROME_PROFILE, "Local State"))


def _keep_session_cookies():
    """Chrome deletes session cookies (no expiry) each time it starts, and the ObservePoint sign-in
    (accessKey, user_id, ...) is a session cookie: on 2026-11-10 every sign-in in the agent Chrome was
    gone after the next start (a switch to headed or headless, a restart). Chrome saved the cookies on
    exit, so before it starts, give them an expiry of 7 days: it then keeps them as normal cookies.
    Chrome is not running here, so the file is not in use."""
    import sqlite3, time
    path = os.path.join(AGENT_CHROME_PROFILE, "Default", "Cookies")
    if not os.path.exists(path):
        return
    try:
        db = sqlite3.connect(path, timeout=5)
        expires = int((time.time() + 7 * 86400 + 11644473600) * 1000000)  # Chrome time: microseconds since 1601
        db.execute("UPDATE cookies SET has_expires=1, is_persistent=1, expires_utc=? WHERE has_expires=0 "
                   "AND (host_key LIKE '%observepoint.com' OR host_key LIKE '%observepointstaging.com')", (expires,))
        db.commit()
        db.close()
    except sqlite3.Error:
        pass  # a locked file only costs the sign-in, as before


def _agent_chrome():
    """Starts the agent Chrome, in the background, when it does not run."""
    import socket, subprocess, time

    def up():
        try:
            socket.create_connection(("127.0.0.1", AGENT_CHROME_PORT), timeout=0.5).close()
            return True
        except OSError:
            return False

    if up():
        return
    _agent_chrome_app()
    if AGENT_CHROME_PROFILE.startswith(CHECKOUT_CHROME_DIR + os.sep):
        _copy_sign_in()
    _keep_session_cookies()
    # Chrome empties chrome_debug.log when it starts. Keep the log of the run before (it holds
    # the cause when the agent Chrome crashed), as chrome_debug-<time>.log, and keep the last 5.
    import glob, shutil
    debug_log = os.path.join(AGENT_CHROME_PROFILE, "chrome_debug.log")
    if os.path.exists(debug_log) and os.path.getsize(debug_log) > 0:
        shutil.move(debug_log, os.path.join(AGENT_CHROME_PROFILE, time.strftime("chrome_debug-%Y%m%d-%H%M%S.log")))
    for old_log in sorted(glob.glob(os.path.join(AGENT_CHROME_PROFILE, "chrome_debug-*.log")))[:-5]:
        os.remove(old_log)
    flags = [f"--user-data-dir={AGENT_CHROME_PROFILE}", f"--remote-debugging-port={AGENT_CHROME_PORT}",
                    "--no-first-run", "--no-default-browser-check",
                    "--use-mock-keychain",  # see AGENT_CHROME_APP: no keychain prompt, own sign-in
                    # Google binds its sign-in to a device key that Chrome keeps in the keychain. The
                    # renamed copy with a mock keychain cannot keep that key, and Google ended the agent
                    # Chrome's sign-in within 2 hours, twice on 2026-11-07. Turn the binding off.
                    "--disable-features=EnableBoundSessionCredentials,DeviceBoundSessionCredentials",
                    "--no-startup-window",  # no unused "New Tab" window; claim_tab() opens them
                    # Agent windows sit behind the user's windows, and a locked screen covers every window.
                    # Chrome then treats a page as hidden and slows its timers and drawing; tests stall.
                    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    # Chrome's own log, in ~/.chrome-agents/chrome_debug.log: names the failed check
                    # when the agent Chrome crashes (seen on 2026-11-05, cause not yet known).
                    "--enable-logging", "--v=0",
                    # Apps that use the macOS accessibility API (window managers, dictation) make Chrome
                    # build accessibility trees for every page. On 2026-11-06 a renderer logged "Accessibility
                    # tree update queue is too big", then the browser hung and crashed. Agents do not need it:
                    # CDP's Accessibility domain still works with this flag (tested).
                    "--disable-renderer-accessibility"]
    if _agent_chrome_mode() == "headless":
        version = _chrome_version(AGENT_CHROME_APP)
        flags += ["--headless=new",
                  # Gives a 1707 x 885 page, the size of a normal browser window on this Mac (headless starts at 800 x 600).
                  "--window-size=1707,972",
                  # Headless says "HeadlessChrome" in its user agent, which some sign-in pages block.
                  f"--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  f"(KHTML, like Gecko) Chrome/{version.split('.')[0]}.0.0.0 Safari/537.36"]
        # Started directly, not through `open`: no Dock icon and no app to bring to the front.
        subprocess.Popen([CHROME_BIN] + flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        # -g: do not bring it to the front. -n: a second Chrome, separate from the user's.
        subprocess.run(["open", "-gna", AGENT_CHROME_APP, "--args"] + flags, check=True)
    for _ in range(100):
        if up():
            return
        time.sleep(0.2)
    sys.exit(f"browser-harness: the agent Chrome did not open port {AGENT_CHROME_PORT}")


if sys.platform == "darwin" and _agent_env() and sys.argv[1:2] != ["--agent-chrome"]:
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
  browser-harness --agent-chrome [headless|headed|status|restart]
                                   run the agent Chrome without windows (default) or with windows;
                                   restart stops a hung agent Chrome and starts it again
"""


def agent_chrome_command(want):
    """Switch the agent Chrome between headless (no windows) and headed (windows you can watch,
    and sign in again when the staging login expires).
    headed: restarts at once, even when agents use it (a person needs the window, for example to
    sign in), and opens a window at the staging sign-in page.
    headless: restarts only when no agent uses it; otherwise at its next start.
    restart: stops the agent Chrome at once (force-stops it when it hangs) and starts it again in
    its mode. Agents that used it get a new window on their next call."""
    import glob, json, time, urllib.request
    pids = _agent_chrome_pids()
    running = "not running" if not pids else ("headless" if any("--headless" in a for a in pids.values()) else "headed")
    if want == "status":
        print(f"agent Chrome: set to {_agent_chrome_mode()}, now {running}")
        return 0
    def stop(pids):
        # Chrome saves new cookies to disk up to 30 seconds late, and a SIGTERM stop drops the
        # unsaved ones: on 2026-11-09 a staging sign-in made just before a switch to headless was
        # gone after it. Browser.close shuts Chrome down the normal way, which saves them first.
        try:
            from websockets.sync.client import connect
            ws_url = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{AGENT_CHROME_PORT}/json/version", timeout=3))["webSocketDebuggerUrl"]
            with connect(ws_url, open_timeout=3, close_timeout=1) as ws:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            for _ in range(50):
                if not _agent_chrome_pids():
                    break
                time.sleep(0.2)
        except Exception:
            pass  # a hung Chrome does not answer; the signals below stop it
        # A hung Chrome ignores SIGTERM (seen on 2026-11-06), so force-stop it after 10 seconds.
        for pid in _agent_chrome_pids():
            os.kill(pid, 15)
        for _ in range(50):
            if not _agent_chrome_pids():
                break
            time.sleep(0.2)
        for pid in _agent_chrome_pids():
            os.kill(pid, 9)
        for _ in range(25):
            if not _agent_chrome_pids():
                break
            time.sleep(0.2)
        # The port can stay open for a moment after the process ends; _agent_chrome() would then
        # think Chrome still runs and not start it (seen on 2026-11-06).
        import socket
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", AGENT_CHROME_PORT), timeout=0.5).close()
            except OSError:
                return
            time.sleep(0.2)
    if want == "restart":
        if pids:
            stop(pids)
        restart_daemon()
        _agent_chrome()
        print(f"agent Chrome: restarted, {_agent_chrome_mode()}. Agents that used it get a new window on their next call.")
        return 0
    if want not in ("headless", "headed"):
        print("usage: browser-harness --agent-chrome [headless|headed|status|restart]")
        return 2
    open(AGENT_CHROME_MODE_FILE, "w").write(want + "\n")
    if not pids or running == want:
        print(f"agent Chrome: set to {want}" + ("" if not pids else ", already running that way"))
        return 0
    def alive(pid):
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False
    try:
        pages = {t["id"] for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{AGENT_CHROME_PORT}/json/list", timeout=3))}
    except Exception:
        pages = set()
    # A lock file per window that an agent claimed (agent_helpers.claim_tab), named by its target id,
    # in one folder per agent Chrome (/tmp/bu-tab-locks/<BU_NAME>/).
    busy = sorted({l.get("name", "?") for f in glob.glob(f"/tmp/bu-tab-locks/{os.environ.get('BU_NAME', 'default')}/*.json")
                   if os.path.basename(f)[:-5] in pages and alive((l := json.load(open(f))).get("pid"))})
    if busy and want == "headless":
        print(f"agent Chrome: set to {want}. It restarts that way when it next starts. "
              f"Not restarted now, because these agents use it: {', '.join(busy)}")
        return 0
    stop(pids)
    restart_daemon()
    if want == "headed":
        _agent_chrome()
        req = urllib.request.Request(f"http://127.0.0.1:{AGENT_CHROME_PORT}/json/new?{SIGN_IN_URL}", method="PUT")
        urllib.request.urlopen(req, timeout=10).read()
        # The agent Chrome starts with `open -g`, so the sign-in window opened behind the user's windows
        # and they could not find it (2026-11-09). A person asked for this window: bring it to the front.
        import subprocess
        for pid in _agent_chrome_pids():
            subprocess.run(["osascript", "-l", "JavaScript", "-e",
                            f"ObjC.import('AppKit'); $.NSRunningApplication"
                            f".runningApplicationWithProcessIdentifier({pid}).activateWithOptions(3)"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        print(f"agent Chrome: set to headed. Restarted it with a window at {SIGN_IN_URL}."
              + (f" These agents lost their window and get a new one on their next call: {', '.join(busy)}." if busy else "")
              + " When done: browser-harness --agent-chrome headless")
        return 0
    print(f"agent Chrome: set to {want}. Closed the {running} one; the next browser-harness call starts it {want}.")
    return 0


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
    if args and args[0] == "--agent-chrome":
        sys.exit(agent_chrome_command(args[1] if len(args) > 1 else "status"))
    if args and args[0] == "--debug-clicks":
        os.environ["BH_DEBUG_CLICKS"] = "1"
        args = args[1:]
    if not args or args[0] != "-c":
        sys.exit("Usage: browser-harness -c \"print(page_info())\"")
    if len(args) < 2:
        sys.exit("Usage: browser-harness -c \"print(page_info())\"")
    print_update_banner()
    ensure_daemon()
    try:
        exec(args[1], globals())
    finally:
        # Request interception (CDP Fetch) that outlives the script pauses every matching request
        # in the page for good. On 2026-11-06 a QA script that faked 5xx errors ended without
        # Fetch.disable; the app kept retrying, and the agent Chrome grew to 12 GB and hung.
        if "Fetch.enable" in args[1]:
            # Turning interception off while the page still runs sends its paused and later
            # requests to the server for real (2026-11-07: 26 real staging items were paused that
            # way). So first leave the page (about:blank stops its scripts), failing every request
            # that is still paused, and only then turn interception off. The page is lost: a test
            # that intercepts requests must finish its checks in the same call.
            def fail_paused():
                for e in drain_events():
                    if e.get("method") == "Fetch.requestPaused":
                        try:
                            cdp("Fetch.failRequest", requestId=e["params"]["requestId"], errorReason="Aborted")
                        except Exception:
                            pass
            try:
                fail_paused()
                cdp("Page.navigate", url="about:blank")
                import time as _time
                _time.sleep(0.5)
                fail_paused()
            except Exception:
                pass
            try:
                cdp("Fetch.disable")
            except Exception:
                pass


if __name__ == "__main__":
    main()
