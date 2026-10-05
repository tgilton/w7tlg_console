#!/usr/bin/env python3
"""Analyze logs/pairing_debug.csv written under DEBUG_SAMPLE_PAIRING=1.

Usage:
    ./venv/bin/python tools/analyze_pairing.py [path] [--rate 2000000]

Reports, per tuner stream (A = tuner 1, B = tuner 2):
  * sample-counter continuity: gaps (dropped samples) and repeats/overlaps
  * block sizes and how often A and B block sizes differ
  * callback inter-arrival jitter, relative to the block duration
  * A-to-B pairing: is firstSampleNum equal, or a constant offset?
  * A-to-B arrival skew
  * callback body and thread CPU time (from the wrapper's timers)

Pairing across streams is done by nearest arrival time (within 10 ms),
because the two callbacks are not guaranteed to arrive in lockstep.
"""

import argparse
import csv
import os
import sys
from collections import Counter

import numpy as np

_WRAP = 1 << 32
_HUGE = 1 << 20          # a jump bigger than this is a restart, not a drop
_PAIR_TOL_NS = 10_000_000


def load(path):
    streams = {"A": [], "B": []}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            streams[row["stream"]].append((
                int(row["first_sample"]), int(row["num_samples"]),
                int(row["arrival_ns"]), int(row["body_ns"]), int(row["cpu_ns"])))
    return {k: np.array(v, dtype=np.int64).reshape(-1, 5) for k, v in streams.items()}


def continuity(block):
    """Walk one stream in arrival order and classify each block boundary."""
    first, n = block[:, 0], block[:, 1]
    report = {"blocks": len(block), "gap_events": 0, "gap_samples": 0,
              "repeat_events": 0, "repeat_samples": 0,
              "restart_events": 0, "invalid_first": int((first < 0).sum()),
              "gap_sizes": Counter()}
    for i in range(1, len(block)):
        expected = (first[i - 1] + n[i - 1]) % _WRAP
        diff = (first[i] - expected) % _WRAP
        if diff >= _WRAP // 2:
            diff -= _WRAP
        if diff == 0:
            continue
        if abs(diff) > _HUGE:
            report["restart_events"] += 1
        elif diff > 0:
            report["gap_events"] += 1
            report["gap_samples"] += int(diff)
            report["gap_sizes"][int(diff)] += 1
        else:
            report["repeat_events"] += 1
            report["repeat_samples"] += int(-diff)
    return report


def jitter_ms(block, rate):
    """Arrival spacing minus the spacing the previous block's samples predict."""
    t = block[:, 2]
    dt_ms = np.diff(t) / 1e6
    expect_ms = block[:-1, 1] / rate * 1e3
    dev = dt_ms - expect_ms
    if len(dev) == 0:
        return None
    return {"mean": dev.mean(), "std": dev.std(), "min": dev.min(),
            "p50": np.percentile(dev, 50), "p99": np.percentile(dev, 99),
            "p99.9": np.percentile(dev, 99.9), "max": dev.max()}


def pair(a, b):
    """Match each A block to the B block that arrived nearest in time."""
    if len(a) == 0 or len(b) == 0:
        return None
    order = np.argsort(b[:, 2])
    tb = b[order, 2]
    idx = np.searchsorted(tb, a[:, 2])
    idx = np.clip(idx, 1, len(tb) - 1)
    left, right = tb[idx - 1], tb[idx]
    pick = np.where(np.abs(a[:, 2] - left) <= np.abs(right - a[:, 2]), idx - 1, idx)
    nearest = b[order[pick]]
    ok = np.abs(nearest[:, 2] - a[:, 2]) <= _PAIR_TOL_NS
    a_ok, b_ok = a[ok], nearest[ok]
    if len(a_ok) == 0:
        return {"matched": 0}
    d = b_ok[:, 0] - a_ok[:, 0]
    skew_ms = (b_ok[:, 2] - a_ok[:, 2]) / 1e6
    counts = Counter(d.tolist())
    mode, mode_n = counts.most_common(1)[0]
    frac = mode_n / len(d)
    if mode == 0 and frac > 0.99:
        verdict = "EQUAL (firstSampleNum matches on every matched pair)"
    elif frac > 0.99:
        verdict = f"CONSTANT OFFSET {mode} samples (B - A)"
    else:
        verdict = (f"NOT CONSTANT — mode {mode} holds {frac * 100:.1f}% of pairs; "
                   f"counter alone cannot pair the streams")
    return {"matched": len(d), "verdict": verdict, "top_offsets": counts.most_common(5),
            "size_differs": float((a_ok[:, 1] != b_ok[:, 1]).mean()),
            "skew_mean": skew_ms.mean(), "skew_std": skew_ms.std(),
            "skew_min": skew_ms.min(), "skew_max": skew_ms.max()}


def index_pair(a, b):
    """Compare the k-th block of A with the k-th block of B. This does not
    depend on arrival timing, but it is only valid while neither stream has
    dropped or repeated blocks — check the continuity lines first."""
    m = min(len(a), len(b))
    if m == 0:
        return None
    d = (b[:m, 0] - a[:m, 0]) % _WRAP
    d = np.where(d >= _WRAP // 2, d - _WRAP, d)
    counts = Counter(d.tolist())
    mode, mode_n = counts.most_common(1)[0]
    frac = mode_n / m
    if mode == 0 and frac > 0.99:
        verdict = "EQUAL"
    elif frac > 0.99:
        verdict = f"CONSTANT OFFSET {mode} samples (B - A)"
    else:
        verdict = (f"NOT CONSTANT — mode {mode} holds {frac * 100:.1f}% of blocks; "
                   f"streams are not counter-locked block-for-block")
    return {"blocks": m, "len_diff": len(a) - len(b), "verdict": verdict,
            "size_differs": float((a[:m, 1] != b[:m, 1]).mean())}


def cpu_stats(block):
    body_us = block[:, 3] / 1e3
    cpu_us = block[:, 4] / 1e3
    return {"body_mean_us": body_us.mean(), "body_max_us": body_us.max(),
            "cpu_mean_us": cpu_us.mean(), "cpu_max_us": cpu_us.max(),
            "cpu_over_body": cpu_us.sum() / max(body_us.sum(), 1e-9)}


def fmt_block_sizes(block):
    c = Counter(block[:, 1].tolist())
    return ", ".join(f"{n}×{k}" for n, k in c.most_common(6))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "pairing_debug.csv")
    ap.add_argument("path", nargs="?", default=default)
    ap.add_argument("--rate", type=float, default=2_000_000.0,
                    help="delivered IQ rate in samples/s (default 2e6)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.path):
        print(f"no such file: {args.path}", file=sys.stderr)
        return 1
    data = load(args.path)
    a, b = data["A"], data["B"]
    print(f"file: {args.path}")
    print(f"rows: A={len(a)}  B={len(b)}")
    if len(a) == 0 and len(b) == 0:
        print("no rows — was the console started with DEBUG_SAMPLE_PAIRING=1?")
        return 1

    for label, block in (("A", a), ("B", b)):
        if len(block) == 0:
            print(f"\n[{label}] no callbacks recorded")
            continue
        span_s = (block[-1, 2] - block[0, 2]) / 1e9
        rate_cb = (len(block) - 1) / span_s if span_s > 0 else float("nan")
        c = continuity(block)
        print(f"\n[{label}] {len(block)} callbacks over {span_s:.1f}s "
              f"({rate_cb:.0f}/s); samples/s ≈ {block[:, 1].sum() / max(span_s, 1e-9):.0f}")
        print(f"  counter: gaps={c['gap_events']} ({c['gap_samples']} samples dropped)  "
              f"repeats={c['repeat_events']} ({c['repeat_samples']} samples)  "
              f"restarts={c['restart_events']}  invalid_first={c['invalid_first']}")
        if c["gap_sizes"]:
            top = c["gap_sizes"].most_common(5)
            print("  largest gap sizes (samples×count): " +
                  ", ".join(f"{k}×{v}" for k, v in top))
        print(f"  block sizes (samples×count): {fmt_block_sizes(block)}")
        j = jitter_ms(block, args.rate)
        if j:
            print("  arrival jitter vs block duration (ms): " +
                  "  ".join(f"{k}={v:.3f}" for k, v in j.items()))
        s = cpu_stats(block)
        print(f"  callback body: mean {s['body_mean_us']:.1f}µs  max {s['body_max_us']:.1f}µs"
              f"   thread CPU: mean {s['cpu_mean_us']:.1f}µs  max {s['cpu_max_us']:.1f}µs"
              f"   cpu/body {s['cpu_over_body']:.2f}")

    if len(a) and len(b):
        print("\n[A vs B]")
        ip = index_pair(a, b)
        print("  index pairing (k-th block of A with k-th block of B):")
        print(f"    blocks compared: {ip['blocks']}  (count difference A-B: {ip['len_diff']})")
        print(f"    firstSampleNum: {ip['verdict']}")
        print(f"    block size differs on {ip['size_differs'] * 100:.1f}% of blocks")
        print("    valid only if both streams show zero gaps/repeats above")
        p = pair(a, b)
        print("  arrival pairing (nearest arrival within 10 ms):")
        if p is None or p.get("matched", 0) == 0:
            print("    no A/B callbacks within 10 ms of each other")
        else:
            print(f"    matched pairs: {p['matched']}")
            print(f"    firstSampleNum: {p['verdict']}")
            print(f"    top offsets (B-A, count): {p['top_offsets']}")
            print(f"    block size differs on {p['size_differs'] * 100:.1f}% of matched pairs")
            print(f"    arrival skew B-A (ms): mean {p['skew_mean']:.3f}  std {p['skew_std']:.3f}"
                  f"  min {p['skew_min']:.3f}  max {p['skew_max']:.3f}")
            print("    caveat: blocks are ~2 ms apart at 2 Msps, so this is only")
            print("    trustworthy when arrival jitter is well below that")
    return 0


if __name__ == "__main__":
    sys.exit(main())
