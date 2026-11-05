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
    try:
        out = subprocess.run(["moonbeam-dev", "url"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip().startswith("http"):
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "http://localhost.observepointstaging.com:9004/"


APP_URL = _app_url()
_APP_HOST = APP_URL.split("//", 1)[1].split("/", 1)[0]  # e.g. wt1.localhost.observepointstaging.com:9004
LOCAL_PREFIXES = (f"http://{_APP_HOST}", f"http://{_APP_HOST.replace('.observepointstaging.com', '')}")
LOCK_DIR = Path("/tmp/bu-tab-locks")
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
    LOCK_DIR.mkdir(exist_ok=True)
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
_LABEL_JS = """(() => {
  const label = %s;
  if (window.__opAgentLabel === label) return;
  window.__opAgentLabel = label;
  const fix = () => { if (!document.title.startsWith(label)) document.title = label + document.title.replace(/^(\\u{1F7E2} |\\u{1F916} [^\\u00B7]*\\u00B7 )+/u, ''); };
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
    try: cdp("Runtime.evaluate", expression=script)  # the page already open; a no-op when labelled
    except RuntimeError: pass


def _context(target_id):
    return cdp("Target.getTargetInfo", targetId=target_id)["targetInfo"].get("browserContextId")


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
        return {"targetId": state["targetId"], "owned": True}

    # The agent Chrome (run.py) has one profile, so the window always opens in it.
    tid = cdp("Target.createTarget", url="about:blank", newWindow=True, background=True)["targetId"]
    _take_lock(tid, owned=True)
    state = {"targetId": tid, "owned": True}
    _STATE.write_text(json.dumps(state))
    _attach(tid)
    _prepare()
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
