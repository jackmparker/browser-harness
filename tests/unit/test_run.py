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
