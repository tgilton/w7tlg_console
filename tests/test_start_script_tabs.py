"""tools/start_w7tlg.sh closes console tabs left over from earlier launches
before opening a new one (stale tabs reconnect and each plays the same audio:
the flanging of 2026-10-10). Static checks only: nothing here talks to Chrome
or starts the console.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "start_w7tlg.sh"
TEXT = SCRIPT.read_text()


def _applescript() -> str:
    m = re.search(r"<<'APPLESCRIPT'\n(.*?)\nAPPLESCRIPT\n", TEXT, re.S)
    assert m, "close_console_tabs heredoc not found"
    return m.group(1)


def test_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.skipif(shutil.which("osacompile") is None, reason="needs macOS osacompile")
def test_applescript_compiles(tmp_path):
    src = tmp_path / "close.applescript"
    src.write_text(_applescript())
    subprocess.run(["osacompile", "-o", str(tmp_path / "close.scpt"), str(src)], check=True)


def test_only_console_tabs_are_matched():
    script = _applescript()
    prefixes = re.findall(r'starts with "([^"]+)"', script)
    assert sorted(prefixes) == ["http://127.0.0.1:8000/console", "http://localhost:8000/console"]
    # The only close is the one guarded by those prefixes; Chrome is never launched for it.
    assert script.count("close ") == 1
    assert 'if application "Google Chrome" is not running then return 0' in script


def test_tab_is_opened_whether_or_not_the_close_worked():
    """The open must not sit inside the if/else around close_console_tabs."""
    after = TEXT.split("if CLOSED_TABS=$(close_console_tabs", 1)[1]
    fi_at = after.index("\n    fi\n")
    open_at = after.index('open -a "Google Chrome" http://localhost:8000/console')
    assert fi_at < open_at
    assert TEXT.count('open -a "Google Chrome"') == 1
