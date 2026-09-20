#!/usr/bin/env python3
"""Inventory the page shell (stage 6 scope): top status bar, system
messages bar, and every collapse tab / resize handle.

    ./venv/bin/python ui-redesign/shell_inventory.py [path]
"""
import pathlib, re, sys


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
    path = sys.argv[1] if len(sys.argv) > 1 else "dashboard/console.html"
    src = pathlib.Path(path).read_text()
    js = "\n".join(re.findall(r"<script>\n(.*?)\n</script>", src, re.S))
    jsc = js_classes(js)

    regions = []
    a = src.index('<div class="panel panel-status">')
    regions.append(("TOP BAR", src[a:src.index("</div>", src.index("nav-link", a)) + 6]))
    a = src.index('<div class="panel panel-sysmsgs">')
    regions.append(("SYSTEM BAR", src[a:src.index('</div>', src.index('sysmsg-log', a)) + 6]))
    tabs = "".join(m.group(0) for m in re.finditer(
        r'<(?:button|div) class="panel-(?:collapse-tab|resize-handle)"[^>]*>', src))
    regions.append(("TABS + HANDLES", tabs))

    for label, reg in regions:
        ids = sorted(set(re.findall(r'\bid="([^"]+)"', reg)))
        handlers = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(on\w+)="([^"]*)"', reg)))
        datas = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(data-[\w-]+)="([^"]*)"', reg)))
        hrefs = sorted(set(re.findall(r'\bhref="([^"]+)"', reg)))
        present = set()
        for m in re.findall(r'class="([^"]*)"', reg):
            present.update(m.split())
        print(f"== {label} ==")
        for title, items in (("IDS", ids), ("HANDLERS", handlers), ("DATA ATTRS", datas),
                             ("HREFS", hrefs), ("JS-REFERENCED CLASSES", sorted(present & jsc))):
            print(f"-- {title} ({len(items)}) --")
            for i in items:
                print("  " + i)
        print()


if __name__ == "__main__":
    main()
