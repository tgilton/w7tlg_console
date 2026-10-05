"""Tests for the DEBUG_SAMPLE_PAIRING diagnostics (sdr/pairing_debug.py) and
tools/analyze_pairing.py. No hardware: the SDR and combiner are stubs."""
import csv
import ctypes as C
import importlib.util
import os
from types import SimpleNamespace

import numpy as np

from sdr.pairing_debug import PairingDebug

_TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "analyze_pairing.py")


def _load_analyzer():
    spec = importlib.util.spec_from_file_location("analyze_pairing", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Params:
    """Stands in for a POINTER(StreamCbParamsT) — only .contents is read."""
    def __init__(self, first):
        self.contents = SimpleNamespace(firstSampleNum=first)

    def __bool__(self):
        return True


def _stub_sdr():
    comb = SimpleNamespace(enabled=False, gain=1.0, phase_deg=0.0, sample_delay=0,
                           level_dbfs=None, dropped_count_a=0, dropped_count_b=0,
                           _q_a=SimpleNamespace(qsize=lambda: 0),
                           _q_b=SimpleNamespace(qsize=lambda: 0),
                           debug_levels=False, dbg_pow_a=0.0, dbg_pow_b=0.0,
                           dbg_pow_out=0.0, dbg_n=0)
    return SimpleNamespace(combiner=comb, dropped_count=0, dropped_count_b=0,
                           _q=SimpleNamespace(qsize=lambda: 0),
                           _q_b=SimpleNamespace(qsize=lambda: 0),
                           rf_gain_pct=80.0, rf_gain_pct_b=80.0)


def test_wrapper_records_pairing_rows_and_writes_csv(tmp_path):
    sdr = _stub_sdr()
    dbg = PairingDebug(sdr, logs_dir=str(tmp_path))
    seen = []

    def body(xi, xq, params, n, reset, ctx):
        seen.append((params.contents.firstSampleNum, n))

    cb_a = dbg.wrap(body, 0)
    cb_b = dbg.wrap(body, 1)
    dbg.start()
    try:
        cb_a(None, None, _Params(1000), 4096, 0, None)
        cb_b(None, None, _Params(1000), 4096, 0, None)
        cb_a(None, None, _Params(5096), 4096, 0, None)
    finally:
        dbg.stop()

    assert seen == [(1000, 4096), (1000, 4096), (5096, 4096)]
    with open(tmp_path / "pairing_debug.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["stream"], int(r["first_sample"]), int(r["num_samples"])) for r in rows] == [
        ("A", 1000, 4096), ("B", 1000, 4096), ("A", 5096, 4096)]
    assert all(int(r["body_ns"]) >= 0 and int(r["cpu_ns"]) >= 0 for r in rows)
    with open(tmp_path / "rx0_cpu_debug.csv", newline="") as fh:
        per_sec = list(csv.DictReader(fh))
    assert per_sec, "final flush should write one per-second row"
    assert per_sec[-1]["a_cb_count"] == "2" and per_sec[-1]["b_cb_count"] == "1"


def test_wrapper_survives_bad_params_without_raising(tmp_path):
    dbg = PairingDebug(_stub_sdr(), logs_dir=str(tmp_path))
    cb = dbg.wrap(lambda *a: None, 0)
    cb(None, None, None, 16, 0, None)   # NULL params pointer
    assert dbg._w[0] == 1
    assert int(dbg._ring[0][0][0]) == -1


def test_analyzer_finds_gap_repeat_and_equal_pairing(tmp_path):
    analyze = _load_analyzer()
    path = tmp_path / "pairing.csv"
    rows = [
        # A: contiguous 0..4096, then a 100-sample gap, then a 50-sample repeat
        ("A", 0, 4096, 0, 0, 0),
        ("A", 4096, 4096, 2_048_000_000, 0, 0),
        ("A", 8292, 4096, 4_096_000_000, 0, 0),
        ("A", 12388 - 50, 4096, 6_144_000_000, 0, 0),
        # B: same counter values as A at matching arrival times
        ("B", 0, 4096, 10_000_000, 0, 0),
        ("B", 4096, 4096, 2_058_000_000, 0, 0),
        ("B", 8292, 4096, 4_106_000_000, 0, 0),
        ("B", 12388 - 50, 4096, 6_154_000_000, 0, 0),
    ]
    with open(path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["stream", "first_sample", "num_samples",
                     "arrival_ns", "body_ns", "cpu_ns"])
        wr.writerows(rows)

    data = analyze.load(str(path))
    cont = analyze.continuity(data["A"])
    assert cont["gap_events"] == 1 and cont["gap_samples"] == 100
    assert cont["repeat_events"] == 1 and cont["repeat_samples"] == 50
    p = analyze.pair(data["A"], data["B"])
    assert p["matched"] == 4
    assert p["verdict"].startswith("EQUAL")
    assert analyze.main([str(path)]) == 0


def test_analyzer_reports_constant_offset(tmp_path):
    analyze = _load_analyzer()
    a = np.array([[i * 4096, 4096, i * 2_048_000_000, 0, 0] for i in range(20)], dtype=np.int64)
    b = a.copy()
    b[:, 0] += 123456   # B's counter sits a fixed distance ahead of A's
    p = analyze.pair(a, b)
    assert p["verdict"].startswith("CONSTANT OFFSET 123456")
