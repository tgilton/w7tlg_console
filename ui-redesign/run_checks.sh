#!/usr/bin/env bash
#
# run_checks.sh — everything that must pass before a commit touching
# dashboard/console.html.
#
# USAGE
#   ./ui-redesign/run_checks.sh            # run everything
#   ./ui-redesign/run_checks.sh --quick    # skip the two browser checks
#
# Run it from the repository root. It starts nothing, stops nothing and
# never touches the running console, rigctld or WSJT-X: the browser checks
# load dashboard/console.html from disk as a file:// URL in a headless
# Chrome with its own throwaway profile directory, which cannot see the
# operator's Chrome window or tabs.
#
# WHAT IT CHECKS, and why each one exists
#   1. pytest                 the backend suite (minus the browser marker)
#   2. node unit tests        S-meter maths, AF taper, AF paths, palettes
#   3. node --check           each <script> block parses
#   4. protected lines        five literals two test files match as text
#   5. CSS brace balance      a truncated rule silently eats the rest
#   6. smoke_console.js       the real page loads without a ReferenceError
#   7. test_wheel_real.js     a real WheelEvent still moves every slider
#
# Checks 3-5 are static and catch what a screenshot cannot. Check 6 is the
# one that would have caught the stage 5 outage; Amendment F15 requires it
# before every commit that touches console.html. --quick skips 6 and 7,
# which are the slow ones (~10s each) and the only ones that need Chrome.
#
# Exits non-zero if any check fails. Each check prints PASS or FAIL.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

CONSOLE="dashboard/console.html"
PY="./venv/bin/python"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

FAILED=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=1; }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; }
head_() { printf '\n%s\n' "$1"; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# ---------------------------------------------------------------- 1. pytest
head_ "1. pytest"
if [ -x "$PY" ]; then
  # -m "not browser" deselects tests/test_console_smoke.py, which is the
  # same probe as check 6 below and takes ~90s. Running it twice per commit
  # buys nothing.
  if out=$("$PY" -m pytest -q -m "not browser" 2>&1); then
    pass "pytest — $(printf '%s' "$out" | tail -n 1)"
  else
    fail "pytest"
    printf '%s\n' "$out" | tail -n 25
  fi
else
  fail "pytest — $PY not found (this project always uses the venv)"
fi

# ------------------------------------------------------------ 2. node tests
head_ "2. node unit tests"
if command -v node >/dev/null 2>&1; then
  for t in test_meter_math test_af_taper test_af_paths test_palettes; do
    if out=$(node "ui-redesign/$t.js" 2>&1); then
      pass "$t.js — $(printf '%s' "$out" | tail -n 1)"
    else
      fail "$t.js"
      printf '%s\n' "$out" | tail -n 20
    fi
  done
else
  fail "node not on PATH"
fi

# ----------------------------------------------------- 3. node --check blocks
# node --check parses JavaScript, not HTML. It is worth running anyway (it
# catches a truncated statement), but note what it CANNOT see: attributes.
# A mangled ontouchend="..." is invisible to it. That is what the inventory
# scripts and check 6 are for.
head_ "3. node --check on each <script> block"
if command -v node >/dev/null 2>&1; then
  "$PY" - "$CONSOLE" "$tmp" <<'PYEOF'
import re, sys, pathlib
html = pathlib.Path(sys.argv[1]).read_text()
out = pathlib.Path(sys.argv[2])
for i, m in enumerate(re.finditer(r'<script>(.*?)</script>', html, re.S), 1):
    (out / f'block{i}.js').write_text(m.group(1))
PYEOF
  n=0; bad=0
  for f in "$tmp"/block*.js; do
    [ -e "$f" ] || continue
    n=$((n + 1))
    if ! err=$(node --check "$f" 2>&1); then
      fail "script block $n ($(basename "$f")) — $(printf '%s' "$err" | head -n 3)"
      bad=1
    fi
  done
  if [ "$n" -eq 0 ]; then
    fail "no <script> blocks extracted"
  elif [ "$bad" -eq 0 ]; then
    pass "all $n <script> blocks parse"
  fi
else
  fail "node not on PATH"
fi

# -------------------------------------------------------- 4. protected lines
# tests/test_amp_drive_limit.py and tests/test_tx_guarded_writes.py match
# these as literal source text. Restyle around them freely; do not edit the
# lines themselves without changing those tests, and per Amendment E3 that
# is a stop-and-ask, not a judgement call.
head_ "4. protected literal lines"
missing=0
while IFS= read -r needle; do
  [ -z "$needle" ] && continue
  if ! grep -qF -- "$needle" "$CONSOLE"; then
    fail "missing: $needle"
    missing=1
  fi
done <<'NEEDLES'
const driveLimit = s.drive_limit_w ?? 100;
slider.max = driveLimit;
id="rf-power-slider"
if (msg.message) return msg.message;
showToast('Error: ' + cmdFailureText(msg), 'error')
NEEDLES
[ "$missing" -eq 0 ] && pass "all 5 protected lines present"

# ---------------------------------------------------------- 5. CSS braces
head_ "5. CSS brace balance"
counts=$("$PY" - "$CONSOLE" <<'PYEOF'
import re, sys
html = open(sys.argv[1]).read()
css = ''.join(m.group(1) for m in re.finditer(r'<style>(.*?)</style>', html, re.S))
css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
print(css.count('{'), css.count('}'))
PYEOF
)
set -- $counts
if [ "$1" = "$2" ]; then pass "braces balanced ($1/$2)"; else fail "braces $1/$2"; fi

# ------------------------------------------------------ 6/7. browser checks
if [ "$QUICK" -eq 1 ]; then
  head_ "6-7. browser checks"
  skip "--quick given"
elif [ ! -x "$CHROME" ]; then
  head_ "6-7. browser checks"
  skip "Chrome not found at $CHROME"
else
  head_ "6. smoke_console.js (the real page, headless)"
  if out=$(node ui-redesign/smoke_console.js 2>&1); then
    pass "smoke_console.js — $(printf '%s' "$out" | tail -n 1)"
  else
    fail "smoke_console.js"
    printf '%s\n' "$out" | tail -n 30
  fi

  head_ "7. test_wheel_real.js (a real WheelEvent at every slider)"
  if out=$(node ui-redesign/test_wheel_real.js 2>&1); then
    pass "test_wheel_real.js — $(printf '%s' "$out" | tail -n 1)"
  else
    fail "test_wheel_real.js"
    printf '%s\n' "$out" | tail -n 30
  fi
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mALL CHECKS PASSED\033[0m\n'
else
  printf '\033[31mSOME CHECKS FAILED\033[0m\n'
fi
exit "$FAILED"
