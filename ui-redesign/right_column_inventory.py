#!/usr/bin/env python3
"""Inventory the right column's contract with the JavaScript.

A restyle is only safe if it changes nothing the code reaches for. This
lists, for the .panel-bandamp region of dashboard/console.html:

  * every id attribute
  * every handler attribute (on*)
  * every data-* attribute
  * every class present in the region that ANY JavaScript in the file
    refers to by name — via querySelector/querySelectorAll, classList.*,
    className, getElementsByClassName, or matches/closest

Run it before a change, save the output, run it after, and diff. The two
must be identical except for classes that were purely added.

    ./venv/bin/python ui-redesign/right_column_inventory.py > before.txt
    ...edit...
    ./venv/bin/python ui-redesign/right_column_inventory.py > after.txt
    diff before.txt after.txt
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "dashboard" / "console.html"

REGION_START = '<div class="panel panel-bandamp"'
REGION_END = "<!-- ── 5. System messages"


def region(src: str) -> str:
    a = src.find(REGION_START)
    b = src.find(REGION_END, a)
    if a == -1 or b == -1:
        sys.exit("could not locate the .panel-bandamp region")
    return src[a:b]


def js_of(src: str) -> str:
    return "\n".join(re.findall(r"<script>\n(.*?)\n</script>", src, re.S))


def classes_js_references(js: str) -> set[str]:
    """Class names the JS names as strings, however it gets at them."""
    found: set[str] = set()

    # classList.add/remove/toggle/contains/replace('name', ...)
    for m in re.finditer(r"classList\.\w+\(\s*([^)]*)\)", js):
        found.update(re.findall(r"['\"]([\w-]+)['\"]", m.group(1)))

    # getElementsByClassName('name')
    found.update(re.findall(r"getElementsByClassName\(\s*['\"]([\w-]+)['\"]", js))

    # querySelector/querySelectorAll/closest/matches with a .class anywhere
    # in the selector string.
    for m in re.finditer(r"(?:querySelectorAll|querySelector|closest|matches)"
                         r"\(\s*['\"]([^'\"]+)['\"]", js):
        found.update(re.findall(r"\.([A-Za-z][\w-]*)", m.group(1)))

    # className = '...' / += '...' — whole-attribute writes, which is how
    # this file sets button state in several places.
    for m in re.finditer(r"className\s*\+?=\s*([^;\n]+)", js):
        found.update(re.findall(r"['\"]([^'\"]*)['\"]", m.group(1)))

    # The above yields multi-word strings like 'btn active'; split them.
    out: set[str] = set()
    for f in found:
        out.update(p for p in f.replace(".", " ").split() if re.fullmatch(r"[\w-]+", p))
    return out


def main() -> None:
    src = CONSOLE.read_text()
    reg = region(src)
    js = js_of(src)

    ids = sorted(set(re.findall(r'\bid="([^"]+)"', reg)))
    handlers = sorted(set(
        f'{name}="{val}"' for name, val in re.findall(r'\b(on\w+)="([^"]*)"', reg)))
    datas = sorted(set(
        f'{name}="{val}"' for name, val in re.findall(r'\b(data-[\w-]+)="([^"]*)"', reg)))

    present = set()
    for m in re.findall(r'class="([^"]*)"', reg):
        present.update(m.split())
    referenced = sorted(present & classes_js_references(js))

    print("== IDS (%d) ==" % len(ids))
    for i in ids:
        print("  " + i)
    print("\n== HANDLER ATTRIBUTES (%d) ==" % len(handlers))
    for h in handlers:
        print("  " + h)
    print("\n== DATA ATTRIBUTES (%d) ==" % len(datas))
    for d in datas:
        print("  " + d)
    print("\n== CLASSES IN REGION THAT JS REFERS TO (%d) ==" % len(referenced))
    for c in referenced:
        print("  " + c)
    print("\n== ALL CLASSES PRESENT IN REGION (%d) ==" % len(sorted(present)))
    for c in sorted(present):
        print("  " + c)


if __name__ == "__main__":
    main()
