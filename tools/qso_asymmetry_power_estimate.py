#!/usr/bin/env python3
"""
Estimate the TX power where mean QSO signal asymmetry crosses zero.

Reads data/qso_propagation_correlation.csv (see
tools/qso_propagation_correlation.py) and regresses asymmetry_db against
tx_pwr_logged_w to find the power at which, on average, you hear the other
station exactly as well as they hear you.

asymmetry_db = rst_sent - rst_rcvd (your report of them, minus their report
of you). Positive means you're hearing better than you're being heard
(under-driving relative to that crossover point); negative means the
reverse.

Two fits are reported:

  1. Pooled — single slope/intercept across every band. Simple, but bands
     differ enough in typical asymmetry (propagation, noise floor, band
     conditions) that pooling can bias the crossover.

  2. Per-band fixed-effects — one shared slope across bands (power is
     assumed to affect asymmetry the same way everywhere) but a separate
     intercept per band, so each band gets its own zero-crossing without
     losing the statistical power of the full dataset. Only bands with at
     least MIN_BAND_N rows get a reported crossing — thinner bands produce
     a coefficient but it's not trustworthy.

Caveat baked into the source data (see qso_propagation_correlation.py):
asymmetry is driven mostly by the OTHER station's setup — their noise
floor, power, antenna — which this dataset can't see. Power typically
explains only a small fraction of the variance (R^2 ~0.15-0.2 in initial
runs), so treat the crossing as "where the data centers," not a precise
setpoint. tx_pwr_logged_w is also the power WSJT-X was told to run, not a
measured output watt — there is no real fwd_w telemetry in this dataset
(fwd_w_avg_telemetry is empty for every row so far).

Usage:
    python3 tools/qso_asymmetry_power_estimate.py
        Reads data/qso_propagation_correlation.csv, prints the estimate.

    python3 tools/qso_asymmetry_power_estimate.py /path/to/correlation.csv
        Explicit input path.

    python3 tools/qso_asymmetry_power_estimate.py --predict-w 100
        Also prints the expected mean asymmetry at 100W (pooled and
        per-band), extrapolating the same fit rather than refitting.
"""

import argparse
import csv
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "data" / "qso_propagation_correlation.csv"

MIN_BAND_N = 20
POWER_BINS_W = [0, 50, 100, 150, 200, 250, 300, 400, 600]


def _to_float(x: Optional[str]) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_power_asymmetry(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (power, asymmetry_db, band) arrays for rows with both
    tx_pwr_logged_w and asymmetry_db present."""
    power, asym, band = [], [], []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            p = _to_float(row.get("tx_pwr_logged_w"))
            a = _to_float(row.get("asymmetry_db"))
            if p is None or a is None:
                continue
            power.append(p)
            asym.append(a)
            band.append(row.get("band", ""))
    return np.array(power), np.array(asym), np.array(band)


def pooled_fit(power: np.ndarray, asym: np.ndarray) -> dict:
    A = np.vstack([power, np.ones_like(power)]).T
    slope, intercept = np.linalg.lstsq(A, asym, rcond=None)[0]
    r = np.corrcoef(power, asym)[0, 1]
    zero_crossing = -intercept / slope if slope != 0 else None
    return {"slope": slope, "intercept": intercept, "r": r, "zero_crossing_w": zero_crossing}


def per_band_fixed_effects(power: np.ndarray, asym: np.ndarray, band: np.ndarray) -> dict:
    """Shared slope across all bands, one intercept per band. Returns the
    shared slope, overall R^2, and a per-band {n, intercept, zero_crossing_w}."""
    band_list = sorted(set(band))
    ref_band = band_list[0]
    cols = [power]
    for b in band_list[1:]:
        cols.append((band == b).astype(float))
    X = np.vstack(cols + [np.ones_like(power)]).T

    coef, *_ = np.linalg.lstsq(X, asym, rcond=None)
    pred = X @ coef
    ss_res = np.sum((asym - pred) ** 2)
    ss_tot = np.sum((asym - asym.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot

    power_coef = coef[0]
    ref_intercept = coef[-1]

    per_band = {}
    for i, b in enumerate(band_list):
        n = int((band == b).sum())
        if b == ref_band:
            b_intercept = ref_intercept
        else:
            b_intercept = ref_intercept + coef[i]  # coef[0]=power, coef[1..]=band offsets in band_list[1:] order
        zc = -b_intercept / power_coef if power_coef != 0 else None
        per_band[b] = {"n": n, "intercept": b_intercept, "zero_crossing_w": zc}

    return {"power_coef": power_coef, "r2": r2, "per_band": per_band}


def binned_means(power: np.ndarray, asym: np.ndarray, bins: list[int]) -> list[tuple[int, int, int, float, float]]:
    out = []
    for lo, hi in zip(bins, bins[1:]):
        mask = (power >= lo) & (power < hi)
        n = int(mask.sum())
        if n == 0:
            continue
        out.append((lo, hi, n, float(asym[mask].mean()), float(asym[mask].std())))
    return out


def predict_asymmetry(power_w: float, pooled: dict, fe: dict) -> tuple[float, dict[str, Optional[float]]]:
    """Extrapolates the already-fit models to a given power. Returns
    (pooled_prediction, {band: per_band_prediction}) — per-band predictions
    use the fixed-effects model's shared slope with that band's own
    intercept, and are None for bands below MIN_BAND_N."""
    pooled_pred = pooled["slope"] * power_w + pooled["intercept"]
    per_band_pred = {}
    for b, info in fe["per_band"].items():
        if info["n"] < MIN_BAND_N:
            per_band_pred[b] = None
            continue
        per_band_pred[b] = fe["power_coef"] * power_w + info["intercept"]
    return pooled_pred, per_band_pred


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT,
                    help="Path to qso_propagation_correlation.csv")
    p.add_argument("--predict-w", type=float, default=None,
                    help="Also predict expected mean asymmetry (dB) at this TX power")
    return p


def main():
    args = build_arg_parser().parse_args()
    input_path = args.input
    if not input_path.exists():
        print(f"No input file at {input_path}")
        return

    power, asym, band = load_power_asymmetry(input_path)
    if len(power) == 0:
        print("No rows with both tx_pwr_logged_w and asymmetry_db — nothing to fit.")
        return

    print(f"{len(power)} QSOs with power + asymmetry data "
          f"(power {power.min():.0f}-{power.max():.0f}W, mean asymmetry {asym.mean():+.2f} dB)\n")

    pooled = pooled_fit(power, asym)
    print("Pooled fit (all bands together):")
    print(f"  asymmetry = {pooled['slope']:.5f} * power + {pooled['intercept']:.3f}   "
          f"(r={pooled['r']:.3f})")
    if pooled["zero_crossing_w"] is not None:
        print(f"  Zero-crossing power: {pooled['zero_crossing_w']:.0f} W")
    print()

    fe = per_band_fixed_effects(power, asym, band)
    print(f"Per-band fixed-effects fit (shared slope={fe['power_coef']:.5f}, R^2={fe['r2']:.3f}):")
    for b, info in sorted(fe["per_band"].items(), key=lambda kv: -kv[1]["n"]):
        if info["n"] < MIN_BAND_N:
            print(f"  {b:>5} (n={info['n']:>3}): too few rows for a trustworthy crossing")
            continue
        zc = info["zero_crossing_w"]
        zc_str = f"{zc:.0f} W" if zc is not None else "n/a"
        print(f"  {b:>5} (n={info['n']:>3}): zero-crossing = {zc_str}")
    print()

    print("Binned means (pooled, sanity check against the fit):")
    for lo, hi, n, mean_a, std_a in binned_means(power, asym, POWER_BINS_W):
        print(f"  {lo:>4}-{hi:<4}W: n={n:>4}  mean asymmetry={mean_a:+6.2f} dB  std={std_a:5.2f}")

    if args.predict_w is not None:
        pooled_pred, per_band_pred = predict_asymmetry(args.predict_w, pooled, fe)
        print(f"\nPredicted mean asymmetry at {args.predict_w:.0f}W (extrapolated, not refit):")
        print(f"  pooled: {pooled_pred:+.2f} dB")
        for b, pred in sorted(per_band_pred.items(), key=lambda kv: -fe["per_band"][kv[0]]["n"]):
            n = fe["per_band"][b]["n"]
            pred_str = f"{pred:+.2f} dB" if pred is not None else "n/a (too few rows)"
            print(f"  {b:>5} (n={n:>3}): {pred_str}")


if __name__ == "__main__":
    main()
