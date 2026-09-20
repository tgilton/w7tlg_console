#!/usr/bin/env python3
"""List CSS class and id selectors in console.html that nothing else mentions.

    ./venv/bin/python ui-redesign/dead_css_scan.py [path] > candidates.txt

This produces CANDIDATES, not a delete list, and it is deliberately
conservative — it reports a name only when it appears nowhere at all
outside the stylesheet. Read ui-redesign/reports/dead-css-candidates.md
before acting on any of it. Amendment F19 puts deletion out of scope.

Why it cannot be trusted blindly: class names are constructed at runtime
in this file (`'v2-' + kind`, `'dot-' + name`, `'stat-' + name`,
`'tray-dot-' + tab`), so a name that is never written literally can still
be live. Any candidate that looks like a prefix plus a variable part is
guilty until proven otherwise.
"""
import pathlib
import re
import sys
from collections import defaultdict
from pathlib import Path

PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "dashboard/console.html")
html = PATH.read_text()

styles = [m for m in re.finditer(r"<style>(.*?)</style>", html, re.S)]
css = "".join(m.group(1) for m in styles)
css_nc = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

# Everything that is NOT a <style> block: markup, script, comments.
rest = html
for m in reversed(styles):
    rest = rest[: m.start()] + rest[m.end():]

# Which region of the stylesheet a name lives in, so the report can say
# whether a candidate is v1 legacy or something v2 introduced. Matched
# against the RAW html, not the comment-stripped css, because the markers
# are themselves comments.
V2_CSS = "".join(m.group(0) for m in re.finditer(
    r"/\* V2:BEGIN.*?/\* V2:END \*/", html, re.S))

# The style guide is a second consumer of this stylesheet: a v2 component
# class the console does not use may still be the style guide's vocabulary.
SG = pathlib.Path(__file__).with_name("styleguide.html")
sg_text = SG.read_text() if SG.exists() else ""


def word(name, text):
    return bool(re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text))


def in_v2(name):
    return word("." + name, V2_CSS) or word("#" + name, V2_CSS)


# Selector text = everything before a '{' that is not itself inside a
# declaration block. Walk the CSS tracking brace depth; at depth 0 a run of
# text ending in '{' is a selector list, at depth >0 it is a declaration
# body (or a nested block inside @media, whose selectors we also want).
selectors = []          # (selector_text, offset_in_css_nc)
buf, start, depth = [], 0, 0
i = 0
while i < len(css_nc):
    c = css_nc[i]
    if c == "{":
        text = "".join(buf).strip()
        if text and not text.startswith("@"):
            selectors.append((text, start))
        elif text.startswith("@"):
            pass         # at-rule prelude: its inner blocks are handled below
        buf, start = [], i + 1
        depth += 1
    elif c == "}":
        buf, start = [], i + 1
        depth -= 1
    else:
        if not buf:
            start = i
        buf.append(c)
    i += 1

CLASS_RE = re.compile(r"\.(-?[A-Za-z_][\w-]*)")
ID_RE = re.compile(r"#(-?[A-Za-z_][\w-]*)")

css_classes = defaultdict(list)
css_ids = defaultdict(list)
for text, off in selectors:
    # Don't read a class out of an attribute selector's value.
    clean = re.sub(r"\[[^\]]*\]", "", text)
    for name in CLASS_RE.findall(clean):
        css_classes[name].append(text)
    for name in ID_RE.findall(clean):
        css_ids[name].append(text)


def mentioned_outside(name):
    """Any literal occurrence of the bare word outside the stylesheet."""
    return word(name, rest)


def report(kind, table):
    dead = []
    for name, texts in sorted(table.items()):
        if not mentioned_outside(name):
            dead.append((name, len(texts), in_v2(name), word(name, sg_text)))
    print(f"\n{kind}: {len(table)} distinct, "
          f"{len(dead)} never mentioned outside the stylesheet\n")
    print(f"  {'name':<34} {'rules':>5}  {'origin':<6}  in styleguide.html")
    print(f"  {'-' * 34} {'-' * 5}  {'-' * 6}  ------------------")
    for name, n, v2, sg in dead:
        print(f"  {name:<34} {n:>5}  {'v2' if v2 else 'v1':<6}  "
              f"{'YES — it is that page' + chr(39) + 's vocabulary' if sg else 'no'}")
    return dead


print(f"dead_css_scan.py — {PATH}")
print(f"{len(styles)} <style> block(s), {len(selectors)} selector lists")
print("\nCANDIDATES ONLY. Nothing here is safe to delete on this evidence")
print("alone — see the runtime-constructed-name warning in the docstring.")
report("CLASS SELECTORS", css_classes)
report("ID SELECTORS", css_ids)
