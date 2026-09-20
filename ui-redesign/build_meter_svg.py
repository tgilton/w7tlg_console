#!/usr/bin/env python3
"""Generate the analog S-meter face as inline SVG (spec §2.2).

Geometry is fixed: viewBox 460x240, arc centre (230, 330), radius 270,
sweeping -38 to +38 degrees from vertical. S1->S9 occupies the first 58%
of the arc linear in S units; S9->+60 the last 42% linear in dB.

Emitted once per receiver into dashboard/console.html. Re-run and paste if
the geometry ever changes:

    ./venv/bin/python ui-redesign/build_meter_svg.py rx1
"""
import math
import sys

CX, CY, R = 230.0, 330.0, 270.0
SWEEP = 38.0          # degrees either side of vertical
S9_FRAC = 0.58        # of the arc used by S1..S9

FACE, INK, RED = "#f2efe6", "#15181d", "#d0402b"


def ang(f):                       # fraction 0..1 -> degrees from vertical
    return -SWEEP + 2 * SWEEP * f


def pt(f, r):
    a = math.radians(ang(f))
    return (CX + r * math.sin(a), CY - r * math.cos(a))


def f_of_s(s):                    # S units 1..9
    return (s - 1) / 8.0 * S9_FRAC


def f_of_over(db):                # dB above S9
    return S9_FRAC + min(db, 60.0) / 60.0 * (1 - S9_FRAC)


def arc(f0, f1, r):
    x0, y0 = pt(f0, r)
    x1, y1 = pt(f1, r)
    return f"M {x0:.2f} {y0:.2f} A {r:.2f} {r:.2f} 0 0 1 {x1:.2f} {y1:.2f}"


def tick(f, r_out, r_in):
    x0, y0 = pt(f, r_out)
    x1, y1 = pt(f, r_in)
    return f"M {x0:.2f} {y0:.2f} L {x1:.2f} {y1:.2f}"


def label(f, text, r=238.0, size=30, weight=700, fill=INK):
    x, y = pt(f, r)
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="middle" dominant-baseline="middle">{text}</text>')


def main():
    rx = sys.argv[1] if len(sys.argv) > 1 else "rx1"
    sfx = "" if rx == "rx1" else "2"

    maj_s = [tick(f_of_s(s), R, R - 17) for s in (1, 3, 5, 7, 9)]
    min_s = [tick(f_of_s(s), R, R - 10) for s in (2, 4, 6, 8)]
    maj_o = [tick(f_of_over(d), R, R - 17) for d in (10, 30, 60)]
    min_o = [tick(f_of_over(d), R, R - 10) for d in (20, 40, 50)]

    labels = [label(f_of_s(1), "S"), label(f_of_s(3), "3"), label(f_of_s(5), "5"),
              label(f_of_s(7), "7"), label(f_of_s(9), "9"),
              label(f_of_over(10), "+10", r=300, fill=RED),
              label(f_of_over(30), "+30", r=300, fill=RED),
              label(f_of_over(60), "+60", r=300, fill=RED)]

    nx, ny = pt(0.0, R - 4)           # needle tip length (drawn straight up, rotated)
    needle_len = R - 4

    out = f'''<svg class="v2-meter-svg" id="smeter{sfx}-svg" viewBox="0 0 460 240"
     role="img" aria-labelledby="smeter{sfx}-svg-title" preserveAspectRatio="xMidYMid meet">
  <title id="smeter{sfx}-svg-title">Signal strength meter</title>
  <rect x="0" y="0" width="460" height="240" rx="10" fill="{FACE}"/>
  <path d="{arc(0, S9_FRAC, R)}" fill="none" stroke="{INK}" stroke-width="3"/>
  <path d="{arc(S9_FRAC, 1, R)}" fill="none" stroke="{RED}" stroke-width="5"/>
  <path d="{' '.join(maj_s)}" stroke="{INK}" stroke-width="3.5" fill="none"/>
  <path d="{' '.join(min_s)}" stroke="{INK}" stroke-width="2" fill="none"/>
  <path d="{' '.join(maj_o)}" stroke="{RED}" stroke-width="3.5" fill="none"/>
  <path d="{' '.join(min_o)}" stroke="{RED}" stroke-width="2" fill="none"/>
  {chr(10).join('  ' + l for l in labels)}
  <text x="230" y="205" font-size="34" font-weight="800" letter-spacing="3"
        fill="{INK}" text-anchor="middle">SIGNAL</text>
  <polygon id="smeter{sfx}-peak" class="v2-meter-peak" points="0,-250 -8,-236 8,-236"
           fill="#1f80c4" transform="translate({CX} {CY}) rotate({ang(0):.2f})"/>
  <line id="smeter{sfx}-needle" class="v2-meter-needle"
        x1="0" y1="0" x2="0" y2="-{needle_len:.0f}" stroke="{INK}" stroke-width="3"
        stroke-linecap="round" transform="translate({CX} {CY}) rotate({ang(0):.2f})"/>
</svg>'''
    print(out)


if __name__ == "__main__":
    main()
