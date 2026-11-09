import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "agent-workspace" / "agent_helpers.py"
_spec = importlib.util.spec_from_file_location("agent_helpers_under_test", _PATH)
ah = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ah)


def _chrome_like_cdp(calls):
    """Rejects what Chrome rejects: a NonSecure source scheme on an https url."""
    def cdp(method, cookies):
        calls.append(cookies)
        for c in cookies:
            if c.get("sourceScheme") == "NonSecure" and c.get("url", "").startswith("https://"):
                raise RuntimeError("Invalid cookie fields")
        return {}
    return cdp


def test_delete_of_a_cookie_the_local_app_set_over_http_uses_its_http_url(monkeypatch):
    calls = []
    monkeypatch.setattr(ah, "cdp", _chrome_like_cdp(calls))
    local = {"name": "op_admin", "domain": "wt1.localhost.observepointstaging.com", "path": "/", "secure": False,
             "httpOnly": False, "sourceScheme": "NonSecure", "sourcePort": 9004}
    ah._delete_cookies([local])
    assert calls[-1][0]["url"] == "http://wt1.localhost.observepointstaging.com:9004/"


def test_delete_of_an_https_cookie_keeps_the_https_url_without_a_port(monkeypatch):
    calls = []
    monkeypatch.setattr(ah, "cdp", _chrome_like_cdp(calls))
    staging = {"name": "op_admin", "domain": "master.observepointstaging.com", "path": "/", "secure": False,
               "httpOnly": False, "sourceScheme": "Secure", "sourcePort": 443}
    ah._delete_cookies([staging])
    assert calls[-1][0]["url"] == "https://master.observepointstaging.com/"


def test_rejected_cookie_error_names_the_cookie_and_the_field(monkeypatch):
    monkeypatch.setattr(ah, "cdp", _chrome_like_cdp([]))
    good = {"name": "user_id", "value": "1", "domain": ".observepointstaging.com", "path": "/"}
    bad = {"name": "op_admin", "value": "", "path": "/", "sourceScheme": "NonSecure", "sourcePort": 9004,
           "url": "https://wt1.localhost.observepointstaging.com/"}
    with pytest.raises(RuntimeError, match=r"cookie op_admin on https://wt1\.localhost.*field sourceScheme='NonSecure'"):
        ah._set_cookies([good, bad])
