#!/usr/bin/env python3
"""Inventory the A/B test and Measure Noise boxes wherever they live.

Stage 3a moves both out of the left column and into the tools tray. A move
is only safe if it changes nothing the JavaScript reaches for, so this
lists every id, every on* handler and every data-* attribute inside the
two boxes, located by content rather than position.

    ./venv/bin/python ui-redesign/moved_region_inventory.py [path/to/console.html]
"""
import pathlib, re, sys

TAG = re.compile(r"<(/?)(\w+)([^>]*?)(/?)>")
VOID = {"input", "br", "img", "hr", "meta", "link", "source", "col"}


def balanced(text, start):
    depth = 0
    for m in TAG.finditer(text, start):
        closing, name, _attrs, selfclose = m.groups()
        if name.lower() in VOID or selfclose:
            continue
        depth += -1 if closing else 1
        if depth == 0:
            return text[start:m.end()]
    sys.exit("unbalanced markup")


def box_containing(src, needle):
    i = src.index(needle)
    j = src.rindex('<div class="section-box', 0, i)
    return balanced(src, j)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "dashboard/console.html"
    src = pathlib.Path(path).read_text()
    for label, needle in (("ANTENNA A/B TEST", 'id="box-abtest"'),
                          ("MEASURE NOISE", "<h2>Measure Noise")):
        reg = box_containing(src, needle)
        ids = sorted(set(re.findall(r'\bid="([^"]+)"', reg)))
        handlers = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(on\w+)="([^"]*)"', reg)))
        datas = sorted(set(f'{n}="{v}"' for n, v in re.findall(r'\b(data-[\w-]+)="([^"]*)"', reg)))
        print(f"== {label} ==")
        print(f"  ids ({len(ids)}): " + ", ".join(ids))
        print(f"  handlers ({len(handlers)}):")
        for h in handlers:
            print("    " + h)
        print(f"  data attrs ({len(datas)}): " + (", ".join(datas) or "none"))
        print()


if __name__ == "__main__":
    main()
