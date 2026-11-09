"""Agent-editable browser helpers.

Add task-specific browser primitives here. Core helpers from browser_harness.helpers
load this file when BH_AGENT_WORKSPACE points at this directory, or when this
repo's default agent-workspace exists.

Local addition: claim_tab() and release_tab() keep agents that share Chrome out of
each other's tabs. All agents share one daemon (one Chrome connection, so one
"Allow remote debugging?" prompt). run.py tags each call with the agent session
(BU_CLIENT), the daemon keeps a current tab per client, and claim_tab() picks it:

- Each agent opens its own window in the agent Chrome (see run.py), on its checkout's dev
  server (APP_URL, from `moonbeam-dev url`), and locks it with a lock file. The
  window opens behind the user's windows; its title starts with a label so the user
  can find it in the Window menu and watch.
- Every later call goes back to the claimed tab. No call activates a tab, so no
  agent steals focus.

A lock is free again when release_tab() runs, when the owner's agent process ends,
when the owner has not called claim_tab() for an hour, or when the tab is gone. A
dead owner's window is closed.

Subagents of one session share its id. A subagent that tests at the same time as
another sets its own: BU_CLIENT=<session>-<n> browser-harness -c '...'.
"""
import json, os, time
from pathlib import Path

from browser_harness.helpers import CLIENT, _send, cdp, goto_url, list_tabs, wait_for_load

def _app_url():
    """This checkout's dev server URL from moonbeam-dev (~/ObservePoint/dev-router), else the main one.
    Each checkout has its own host on port 9004: localhost.observepointstaging.com for the main
    checkout, wtN.localhost.observepointstaging.com for the others."""
    import subprocess
    # Ask in this agent's worktree (run.py sets BU_AGENT_CHECKOUT also for a call from another
    # folder). From another folder moonbeam-dev gives no URL, and the agent went to the main
    # checkout's app with a "main" label in its worktree's Chrome (2026-11-09).
    cwd = os.environ.get("BU_AGENT_CHECKOUT")
    try:
        out = subprocess.run(["moonbeam-dev", "url"], capture_output=True, text=True, timeout=10,
                             cwd=cwd if cwd and os.path.isdir(cwd) else None)
        if out.returncode == 0 and out.stdout.strip().startswith("http"):
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "http://localhost.observepointstaging.com:9004/"


APP_URL = _app_url()
_APP_HOST = APP_URL.split("//", 1)[1].split("/", 1)[0]  # e.g. wt1.localhost.observepointstaging.com:9004
LOCAL_PREFIXES = (f"http://{_APP_HOST}", f"http://{_APP_HOST.replace('.observepointstaging.com', '')}")
# One lock folder per agent Chrome. Locks name tab ids of one Chrome, and _locks() deletes every lock
# whose tab its Chrome does not have: with one shared folder, an agent on one Chrome deleted the
# locks of agents on the other Chromes (2026-11-07, two runs at once).
LOCK_DIR = Path("/tmp/bu-tab-locks") / os.environ.get("BU_NAME", "default")
ME = CLIENT or "default"
OWNER_PID = os.environ.get("CLAUDE_PID")  # the agent process; the shell of each call dies at once
STALE_AFTER = 3600
_STATE = LOCK_DIR / f"client-{ME}.state"


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def _read(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _page_ids():
    return {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t["type"] == "page"}


def _locks(page_ids):
    """Live locks by targetId. Deletes locks whose tab or owner is gone."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for f in LOCK_DIR.glob("*.json"):
        lock = _read(f)
        owner_gone = lock and lock.get("name") != ME and (
            (lock.get("pid") and not _alive(lock["pid"])) or time.time() - f.stat().st_mtime > STALE_AFTER)
        if not lock or f.stem not in page_ids or owner_gone:
            if owner_gone and lock.get("owned") and f.stem in page_ids:
                # An agent window left behind. Close it, or it counts as a second localhost tab.
                try: cdp("Target.closeTarget", targetId=f.stem)
                except RuntimeError: pass
            f.unlink(missing_ok=True)
            continue
        out[f.stem] = lock
    return out


def _take_lock(target_id, owned):
    """Atomic create. Returns True when this session holds the lock."""
    path = LOCK_DIR / f"{target_id}.json"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if (_read(path) or {}).get("name") != ME:
            return False
        path.touch()  # still in use
        return True
    with os.fdopen(fd, "w") as f:
        json.dump({"name": ME, "pid": OWNER_PID, "owned": owned}, f)
    return True


# Keeps a label at the start of the tab title, so the user can tell agent windows apart in Chrome's
# Window menu. The app rewrites the title on every page, so an observer puts the label back.
# One observer per page, and it reads the label at each change. On 2026-11-09 a page got a second
# observer with another label ("wt3" and "main"); the two rewrote the title in turn without end, and
# the whole agent Chrome stopped answering 6 times in 20 minutes.
_LABEL_JS = """(() => {
  if (window.__opAgentLabel && !window.__opAgentLabelFix) return;  // the older script, which keeps its own label
  window.__opAgentLabel = %s;
  const fix = () => {
    const label = window.__opAgentLabel;
    if (!document.title.startsWith(label)) document.title = label + document.title.replace(/^(\\u{1F7E2} |\\u{1F916} [^\\u00B7]*\\u00B7 )+/u, '');
  };
  if (window.__opAgentLabelFix) return window.__opAgentLabelFix();
  window.__opAgentLabelFix = fix;
  const watch = () => { fix(); new MutationObserver(fix).observe(document.head, {subtree: true, childList: true, characterData: true}); };
  document.head ? watch() : document.addEventListener('DOMContentLoaded', watch);
})()"""


def _label():
    """'wt1 4f2a': the checkout's slot and the start of the session id."""
    host = _APP_HOST.split(":")[0]
    slot = host.split(".")[0] if host.startswith("wt") else "main"
    return f"\U0001F916 {slot} {ME[:4]} \u00B7 "


def _attach(target_id):
    """Point this daemon at the window without activating it."""
    try:
        if _send({"meta": "connection_status"}).get("target_id") == target_id:
            return
    except RuntimeError:  # not attached yet, or the session went stale
        pass
    sid = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
    _send({"meta": "set_session", "session_id": sid, "target_id": target_id})
    _prepare()


def _prepare():
    """Per CDP session: focus emulation, and the title label on every page this session loads."""
    sid = _send({"meta": "session"}).get("session_id")
    state = _read(_STATE) or {}
    script = _LABEL_JS % json.dumps(_label())
    if state.get("prepared") != sid:
        # A background window is never focused; make the page act as if it were.
        cdp("Emulation.setFocusEmulationEnabled", enabled=True)
        cdp("Page.addScriptToEvaluateOnNewDocument", source=script)
        if state:
            _STATE.write_text(json.dumps({**state, "prepared": sid}))
    # A click on a file input opens the macOS file picker, which brings the agent Chrome to the
    # front (seen on 2026-11-06). Stop the picker; upload_file() sets the files directly instead.
    # Called every time (not only once per session), so windows prepared before this also get it.
    try: cdp("Page.setInterceptFileChooserDialog", enabled=True)
    except RuntimeError: pass
    # Request interception left on by an earlier call pauses the page's requests for good (see
    # run.py). Each call starts with it off; a script that needs it enables it after claim_tab().
    try: cdp("Fetch.disable")
    except RuntimeError: pass
    try: cdp("Runtime.evaluate", expression=script)  # the page already open; a no-op when labelled
    except RuntimeError: pass


def _context(target_id):
    return cdp("Target.getTargetInfo", targetId=target_id)["targetInfo"].get("browserContextId")


_MAIN_AGENT_CHROME = "http://127.0.0.1:9333"  # run.py: the main agent Chrome, ~/.chrome-agents
_STAGING = "observepointstaging.com"


def _token_exp(token):
    import base64
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"]
    except Exception:
        return 0


def _main_cookies():
    import urllib.request
    from websockets.sync.client import connect
    ws_url = json.load(urllib.request.urlopen(f"{_MAIN_AGENT_CHROME}/json/version", timeout=2))["webSocketDebuggerUrl"]
    with connect(ws_url, open_timeout=2, close_timeout=1) as ws:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            msg = json.loads(ws.recv(timeout=5))
            if msg.get("id") == 1:
                return msg["result"]["cookies"]


def _cookie_url(c):
    """The url a host-only cookie came from. Chrome rejects an https url for a cookie with
    sourceScheme NonSecure ("Invalid cookie fields"), such as op_admin that the local app sets on
    http://wtN.localhost.observepointstaging.com:9004 (2026-11-09, WORK-38549)."""
    scheme = "http" if c.get("sourceScheme") == "NonSecure" else "https"
    port = c.get("sourcePort")
    port = f":{port}" if port and port not in (80, 443, -1) else ""
    return f"{scheme}://{c['domain']}{port}{c['path']}"


def _set_cookies(cookies):
    """Storage.setCookies, with an error that names the cookie and the field Chrome rejects.
    Chrome rejects the whole list and does not say which cookie, so on an error set them one by one."""
    if not cookies:
        return
    try:
        cdp("Storage.setCookies", cookies=cookies)
        return
    except Exception as e:
        first = e
    for c in cookies:
        try:
            cdp("Storage.setCookies", cookies=[c])
            continue
        except Exception:
            pass
        where = c.get("domain") or c.get("url")
        for field in [k for k in c if k not in ("name", "value", "domain", "url")]:
            try:
                cdp("Storage.setCookies", cookies=[{k: v for k, v in c.items() if k != field}])
            except Exception:
                continue
            raise RuntimeError(f"{first}: cookie {c['name']} on {where}, field {field}={c[field]!r}") from first
        fields = {k: v for k, v in c.items() if k != "value"}
        raise RuntimeError(f"{first}: cookie {c['name']} on {where}, fields {fields}") from first
    raise first


def _delete_cookies(cookies):
    """Network.deleteCookies does not work on the browser target: write each one again, expired."""
    keep = ("name", "path", "secure", "httpOnly", "sameSite", "sourceScheme", "sourcePort", "partitionKey")
    gone = []
    for c in cookies:
        entry = {**{k: c[k] for k in keep if k in c}, "value": "", "expires": 1}
        if c["domain"].startswith("."):
            entry["domain"] = c["domain"]
        else:  # a host-only cookie (such as __Host-GAPS): a domain would make a different cookie
            entry["url"] = _cookie_url(c)
        gone.append(entry)
    _set_cookies(gone)


def _sync_sign_in():
    """Gives a worktree's agent Chrome the staging sign-in of the main agent Chrome, so one sign-in by
    the user covers every agent Chrome, also the ones that started before it (2026-11-09: three QA
    Chromes started before Jack signed in and stayed signed out).

    - Copies only *.observepointstaging.com cookies, and deletes Google cookies here: Google ends the
      session when its sign-in cookies are in more than one browser (see run.py _copy_sign_in).
    - Writes the cookies without the https-only (Secure) flag. A sign-in on https staging sets an
      https-only accessKey, and Chrome does not send it to the local app on http
      (wtN.localhost.observepointstaging.com:9004), which then goes to Google's sign-in.
    - Never replaces a sign-in this Chrome made itself, such as "Login as" another account."""
    if not os.environ.get("BU_AGENT_CHROME_PROFILE", "").startswith(os.path.expanduser("~/.chrome-agents-checkouts") + os.sep):
        return
    mine = cdp("Storage.getCookies")["cookies"]
    try:
        _delete_cookies([c for c in mine if "google." in c["domain"] or c["domain"].endswith("youtube.com")])
    except Exception as e:  # never let this block the staging copy below
        print(f"claim_tab: could not delete the Google cookies: {e}")
    try:
        main = [c for c in _main_cookies() if c["domain"].endswith(_STAGING) and not c.get("partitionKey")]
    except Exception:
        return  # the main agent Chrome does not run; the copy at start (run.py) is all there is
    soon = time.time() + 600
    main_key = next((c["value"] for c in main if c["name"] == "accessKey" and _token_exp(c["value"]) > soon), None)
    if not main_key:
        return
    valid = [c for c in mine if c["name"] == "accessKey" and _token_exp(c["value"]) > soon]
    if any(not c["secure"] or c["value"] != main_key for c in valid):
        return  # signed in already, for the local app too, or with its own sign-in
    _delete_cookies([c for c in mine if c["domain"].endswith(_STAGING)])
    cookies = []
    for c in main:
        secure = c["name"].startswith(("__Secure-", "__Host-"))
        new = {k: c[k] for k in ("name", "value", "domain", "path", "httpOnly", "priority") if k in c}
        new["secure"] = secure
        if c.get("sameSite") and (secure or c["sameSite"] != "None"):
            new["sameSite"] = c["sameSite"]
        if not c.get("session") and c.get("expires", -1) > 0:
            new["expires"] = c["expires"]
        cookies.append(new)
    _set_cookies(cookies)


def _try_sync_sign_in():
    # After _attach(): cdp() calls run in this session's tab, and fail when that tab is gone.
    try:
        _sync_sign_in()
    except Exception as e:
        print(f"claim_tab: could not copy the staging sign-in from the main agent Chrome: {e}")


def claim_tab(url=None):
    """Attach to this session's own window. Call it first in every browser-harness call.

    Each agent session gets its own window in the agent Chrome (a separate Chrome with its own
    profile, ~/.chrome-agents, which run.py starts). The window opens behind the user's windows
    and never takes focus. Its tab title starts with a label such as
    "\U0001F916 wt1 4f2a \u00B7 ", so the user can find it in the Window menu and watch.
    url: the first page (default: this checkout's dev server, APP_URL).
    Returns {"targetId", "owned": True}. Call release_tab() at the end to close the window.
    Raises when it cannot open the window safely: stop and tell the user the error."""
    page_ids = _page_ids()
    locks = _locks(page_ids)

    state = _read(_STATE)
    if state and locks.get(state["targetId"], {}).get("name") == ME:
        _take_lock(state["targetId"], True)  # refresh
        _attach(state["targetId"])
        _prepare()
        _try_sync_sign_in()
        return {"targetId": state["targetId"], "owned": True}

    # The agent Chrome (run.py) has one profile, so the window always opens in it.
    tid = cdp("Target.createTarget", url="about:blank", newWindow=True, background=True)["targetId"]
    _take_lock(tid, owned=True)
    state = {"targetId": tid, "owned": True}
    _STATE.write_text(json.dumps(state))
    _attach(tid)
    _prepare()
    _try_sync_sign_in()  # before the first page loads, which may need the sign-in
    goto_url(url or APP_URL)
    wait_for_load()
    return state


def release_tab():
    """Free this session's tab. Closes the window if this session opened it."""
    state = _read(_STATE)
    if not state:
        return
    lock = LOCK_DIR / f"{state['targetId']}.json"
    if (_read(lock) or {}).get("name") == ME:
        if state["owned"]:
            try: cdp("Target.closeTarget", targetId=state["targetId"])
            except RuntimeError: pass
        lock.unlink(missing_ok=True)
    _STATE.unlink(missing_ok=True)
