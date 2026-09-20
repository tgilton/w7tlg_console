"""The console actually loads: run ui-redesign/smoke_console.js from pytest.

Every other check on dashboard/console.html is static. `node --check` sees
syntax, the inventory scripts see markup, a screenshot sees layout. None of
them can see a ReferenceError thrown inside Panadapter.init() — which is
exactly what shipped on 2026-09-20 and left the operator with no audio, no
spectrum, no waterfall and dead controls, on a page that still rendered a
perfectly good-looking screenshot.

smoke_console.js loads the real file in headless Chrome and asserts that
nothing throws, that Panadapter / Panadapter2 / RigControl all exist, that
every inline on* handler reference resolves, and that the required element
ids are present. This wrapper exists so that `pytest` alone catches that
class of fault on a machine that has Chrome, without anyone having to
remember a second command.

It SKIPS — it does not fail — when node or Chrome is missing, so the suite
still runs on a machine that is not the operator's Mac. That is deliberate:
a skip here means "unverified", and ui-redesign/run_checks.sh is the thing
that must be run before a commit.

One page load, ~90 seconds — by far the slowest test in the suite, which is
why it carries the `browser` marker. `pytest -m "not browser"` gives the
fast suite back, and ui-redesign/run_checks.sh deselects it there because
check 6 of that script runs the same probe directly.

Nothing is started, stopped or restarted: the page is loaded from disk as a
file:// URL in a throwaway Chrome profile, so it cannot touch the running
console, rigctld, WSJT-X or the operator's own browser window.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SMOKE = REPO / "ui-redesign" / "smoke_console.js"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@pytest.mark.browser
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome not installed")
@pytest.mark.skipif(not SMOKE.exists(), reason="smoke_console.js not present")
def test_console_html_loads_without_errors():
    proc = subprocess.run(
        ["node", str(SMOKE)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    # The script's own output is the useful failure message — it names the
    # exception, the missing id or the unresolved handler.
    assert proc.returncode == 0, (
        "smoke_console.js failed — dashboard/console.html does not load "
        "cleanly:\n\n" + proc.stdout + proc.stderr
    )
