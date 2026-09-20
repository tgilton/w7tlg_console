#!/usr/bin/env python3
"""Inventory a console column's contract with the JavaScript.

Lists, for a named panel region of console.html: every id, every on*
handler, every data-* attribute, and every class present in the region
that any JS in the file names (querySelector*, classList.*, className,
getElementsByClassName, matches, closest).

    ./venv/bin/python ui-redesign/column_inventory.py <panel-id> [path]

e.g.  ./venv/bin/python ui-redesign/column_inventory.py panel-modedsp
"""
import pathlib, re, sys

PANEL_END = ('<!-- ── 3.', '<!-- ── 4.', '<!-- ── 4b.', '<!-- ── 5.')


def region(src, panel_id):
    a = src.index(f'id="{panel_id}"')
    a = src.rindex('<div', 0, a)
    ends = [src.index(m, a) for m in PANEL_END if m in src[a:]]
    return src[a:min(ends)] if ends else src[a:]


def js_classes(js):
    found = set()
    for m in re.finditer(r"classList\.\w+\(\s*([^)]*)\)", js):
        found.update(re.findall(r"['\"]([\w-]+)['\"]", m.group(1)))
    found.update(re.findall(r"getElementsByClassName\(\s*['\"]([\w-]+)['\"]", js))
    for m in re.finditer(r"(?:querySelectorAll|querySelector|closest|matches)"
                         r"\(\s*['\"]([^'\"]+)['\"]", js):
        found.update(re.findall(r"\.([A-Za-z][\w-]*)", m.group(1)))
    for m in re.finditer(r"className\s*\+?=\s*([^;\n]+)", js):
        found.update(re.findall(r"['\"]([^'\"]*)['\"]", m.group(1)))
    out = set()
    for f in found:
        out.update(p for p in f.replace(".", " ").split() if re.fullmatch(r"[\w-]+", p))
    return out


def main():
    panel = sys.argv[1] if len(sys.argv) > 1 else "panel-modedsp"
    path = sys.argv[2] if len(sys.argv) > 2 else "dashboard/console.html"
    src = pathlib.Path(path).read_text()
    reg = region(src, panel)
    js = "\n".join(re.findall(r"<script>\n(.*?)\n</script>", src, re.S))

    ids = sorted(set(re.findall(r'\bid="([^"]+)"', reg)))
    handlers = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(on\w+)="([^"]*)"', reg)))
    datas = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(data-[\w-]+)="([^"]*)"', reg)))
    present = set()
    for m in re.findall(r'class="([^"]*)"', reg):
        present.update(m.split())
    referenced = sorted(present & js_classes(js))

    print(f"== {panel} ==")
    for title, items in (("IDS", ids), ("HANDLERS", handlers),
                         ("DATA ATTRS", datas), ("JS-REFERENCED CLASSES", referenced)):
        print(f"\n-- {title} ({len(items)}) --")
        for i in items:
            print("  " + i)


if __name__ == "__main__":
    main()
