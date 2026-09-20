#!/usr/bin/env python3
"""Inventory the receiver panels' TOP ROW (stage 4a scope).

Region: from each centre panel's opening tag to `.vol-alc-group`, which is
the gains card — the first thing stage 4a does not touch. Lists ids,
on* handlers, data-* attributes and the classes any JS in the file names.

    ./venv/bin/python ui-redesign/rx_toprow_inventory.py [path]
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

    for label, panel in (("RX1", 'class="panel panel-center"'),
                         ("RX2", 'class="panel panel-center2"')):
        a = src.index(panel)
        b = src.index('class="vol-alc-group"', a)
        reg = src[a:b]
        ids = sorted(set(re.findall(r'\bid="([^"]+)"', reg)))
        handlers = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(on\w+)="([^"]*)"', reg)))
        datas = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(data-[\w-]+)="([^"]*)"', reg)))
        present = set()
        for m in re.findall(r'class="([^"]*)"', reg):
            present.update(m.split())
        print(f"== {label} TOP ROW ==")
        print(f"-- IDS ({len(ids)}) --")
        for i in ids:
            print("  " + i)
        print(f"-- HANDLERS ({len(handlers)}) --")
        for h in handlers:
            print("  " + h)
        print(f"-- DATA ATTRS ({len(datas)}) --")
        for d in datas:
            print("  " + d)
        ref = sorted(present & jsc)
        print(f"-- JS-REFERENCED CLASSES ({len(ref)}) --")
        for c in ref:
            print("  " + c)
        print()


if __name__ == "__main__":
    main()
