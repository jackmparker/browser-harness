import sys
from io import StringIO
import unittest.mock
from unittest.mock import patch

from browser_harness import run


def test_c_flag_executes_code():
    stdout = StringIO()
    with patch.object(sys, "argv", ["browser-harness", "-c", "print('hello from -c')"]), \
         patch("browser_harness.run.ensure_daemon"), \
         patch("browser_harness.run.print_update_banner"), \
         patch("sys.stdout", stdout):
        run.main()
    assert stdout.getvalue().strip() == "hello from -c"


def test_c_flag_does_not_read_stdin():
    stdin_read = []
    fake_stdin = StringIO("should not be read")
    fake_stdin.read = lambda: stdin_read.append(True) or ""

    with patch.object(sys, "argv", ["browser-harness", "-c", "x = 1"]), \
         patch("browser_harness.run.ensure_daemon"), \
         patch("browser_harness.run.print_update_banner"), \
         patch("sys.stdin", fake_stdin):
        run.main()

    assert not stdin_read, "stdin should not be read when -c is passed"


def _start_flags(tmp_path, mode):
    with patch("browser_harness.run.AGENT_CHROME_PROFILE", str(tmp_path)), \
         patch("browser_harness.run._agent_chrome_app"), \
         patch("browser_harness.run._keep_session_cookies"), \
         patch("browser_harness.run._agent_chrome_mode", return_value=mode), \
         patch("browser_harness.run._chrome_version", return_value="140.0.1.2"), \
         patch("socket.create_connection", side_effect=[OSError(), unittest.mock.MagicMock()]), \
         patch("time.sleep"), \
         patch("subprocess.Popen") as popen, \
         patch("subprocess.run") as srun:
        run._agent_chrome()
    return popen.call_args[0][0] if mode == "headless" else srun.call_args[0][0]


def test_agent_chrome_starts_muted(tmp_path):
    # Agent Chrome pages must never play sound on the user's speakers or headphones.
    assert "--mute-audio" in _start_flags(tmp_path, "headless")
    assert "--mute-audio" in _start_flags(tmp_path, "headed")


def test_agent_chrome_has_no_profile_flag_without_the_setting(tmp_path, monkeypatch):
    # Everyone without the opt-in setting keeps the Default profile and the same flags as before.
    monkeypatch.delenv("BU_AGENT_CHROME_PROFILE_DIRECTORY", raising=False)
    with patch("browser_harness.run.AGENT_CHROME_PROFILE_DIRECTORY_FILE", str(tmp_path / "missing")):
        for mode in ("headless", "headed"):
            assert not [f for f in _start_flags(tmp_path, mode) if f.startswith("--profile-directory")]
        assert run._profile_directory() == "Default"


def test_agent_chrome_opens_the_opt_in_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("BU_AGENT_CHROME_PROFILE_DIRECTORY", raising=False)
    setting = tmp_path / "setting"
    setting.write_text("Profile 2\n")
    with patch("browser_harness.run.AGENT_CHROME_PROFILE_DIRECTORY_FILE", str(setting)):
        assert "--profile-directory=Profile 2" in _start_flags(tmp_path, "headless")
        assert "--profile-directory=Profile 2" in _start_flags(tmp_path, "headed")


def test_worktree_chrome_uses_default_with_the_opt_in_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("BU_AGENT_CHROME_PROFILE_DIRECTORY", "Profile 2")
    checkouts = tmp_path / "checkouts"
    with patch("browser_harness.run.CHECKOUT_CHROME_DIR", str(checkouts)), \
         patch("browser_harness.run._copy_sign_in"):
        flags = _start_flags(checkouts / "wt1", "headless")
    assert "--profile-directory=Default" in flags


def test_copy_sign_in_reads_the_opt_in_profile(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("BU_AGENT_CHROME_PROFILE_DIRECTORY", "Profile 2")
    main, wt = tmp_path / "main", tmp_path / "wt"
    (main / "Profile 2").mkdir(parents=True)
    db = sqlite3.connect(main / "Profile 2" / "Cookies")
    db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
    db.executemany("INSERT INTO cookies VALUES (?, ?)",
                   [("app.observepointstaging.com", "accessKey"), (".google.com", "SID")])
    db.commit(); db.close()
    with patch("browser_harness.run.MAIN_AGENT_CHROME_PROFILE", str(main)), \
         patch("browser_harness.run.AGENT_CHROME_PROFILE", str(wt)):
        run._copy_sign_in()
    rows = sqlite3.connect(wt / "Default" / "Cookies").execute("SELECT name FROM cookies").fetchall()
    assert rows == [("accessKey",)]
