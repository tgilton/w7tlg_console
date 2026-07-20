#!/usr/bin/env python3
"""
Correlate QSO performance data with real terrain-derived takeoff angles.

Joins QSO records against a terrain-horizon-tool analysis
(https://github.com/tgilton/terrain-horizon-tool) — a separate project
that computes, for a given station location, the required takeoff angle
to clear real terrain at every bearing around it.

Two input sources are supported:

  1. data/qso_performance_log.jsonl (this console's own per-QSO ADIF +
     live station telemetry, see wsjtx/qso_logger.py) — smaller, but
     includes real fwd_w telemetry alongside the logged tx_pwr.
  2. A raw RUMLogNG ADIF export (e.g. "Full Log.adif") — much larger
     history, but has no per-QSO telemetry and (confirmed by inspection)
     no my_gridsquare field at all, so the home grid has to be assigned
     from the QSO date against the known Boise/La Quinta QTH switch
     (config/station_profile.py) rather than read from the record.

Only QSOs assignable to the Boise QTH get a takeoff angle: the terrain
analysis matches Boise's precise grid square (DN13WN) and is the only
location with a matching horizon_summary.csv today. La Quinta QSOs (or
any JSONL record logged under a different station_profile) still get
distance/bearing/asymmetry computed — just no takeoff_angle_deg, rather
than silently pairing them with the wrong location's terrain.

This does not explain asymmetry on its own — causes living entirely on the
other station's end (their noise floor, their power, their antenna) are
invisible to this data and can't be ruled in or out, only ruled out on
*our* end when nothing here correlates.

Usage:
    python3 tools/qso_propagation_correlation.py
        Reads data/qso_performance_log.jsonl (this console's own log) and
        /Users/tgilton/terrain_horizon_tool/output/horizon_summary.csv,
        writes data/qso_propagation_correlation.csv

    python3 tools/qso_propagation_correlation.py "/path/to/Full Log.adif"
        Same, but reads a raw RUMLogNG ADIF export instead (detected by
        .adif/.adi extension) — assigns home grid per QSO from date vs.
        the Boise/La Quinta cutover below.

    python3 tools/qso_propagation_correlation.py <input> <horizon_csv> <output_csv>
        Explicit paths for all three.
"""

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from wsjtx.adif import read_adif_file  # noqa: E402

DEFAULT_QSO_LOG = REPO_ROOT / "data" / "qso_performance_log.jsonl"
DEFAULT_HORIZON_CSV = Path("/Users/tgilton/terrain_horizon_tool/output/horizon_summary.csv")
DEFAULT_OUTPUT = REPO_ROOT / "data" / "qso_propagation_correlation.csv"

EARTH_RADIUS_KM = 6371.0
REQUIRED_STATION_PROFILE = "boise"

# Seasonal QTH switch (config/station_profile.py): La Quinta in winter,
# Boise the rest of the year. Terry confirmed returning to Boise "mid
# April" 2026 — using April 15 as the cutover date since that's as
# precise as "mid April" gets. Approximate by nature; a QSO logged within
# a few days of this boundary could be assigned the wrong QTH.
BOISE_GRID = "DN13WN"
LA_QUINTA_GRID = "DM13up"
QTH_CUTOVER_2026 = date(2026, 4, 15)


def grid_to_latlon(grid: str) -> tuple[float, float]:
    """Maidenhead grid square (4- or 6-character) -> (lat, lon) at the
    center of the cell. 4-char grids only narrow to a ~2deg x 1deg square
    (roughly 150km x 110km at mid-latitudes); 6-char narrows to a
    5min x 2.5min subsquare (roughly 7km x 4.6km)."""
    g = grid.strip()
    if len(g) < 4:
        raise ValueError(f"Grid square too short: {grid!r}")

    lon = (ord(g[0].upper()) - ord("A")) * 20 - 180
    lat = (ord(g[1].upper()) - ord("A")) * 10 - 90
    lon += int(g[2]) * 2
    lat += int(g[3]) * 1

    if len(g) >= 6:
        lon += (ord(g[4].lower()) - ord("a")) * (2 / 24)
        lat += (ord(g[5].lower()) - ord("a")) * (1 / 24)
        lon += (2 / 24) / 2   # center of subsquare cell
        lat += (1 / 24) / 2
    else:
        lon += 1.0   # center of the 2deg x 1deg square
        lat += 0.5

    return lat, lon


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def load_horizon_curve(path: Path) -> list[tuple[float, float]]:
    """Returns (bearing_deg, max_angle_deg) pairs, sorted by bearing."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((float(r["bearing_deg"]), float(r["max_angle_deg"])))
    rows.sort(key=lambda r: r[0])
    return rows


def interpolate_takeoff_angle(bearing_deg: float, curve: list[tuple[float, float]]) -> float:
    """Linear interpolation over the horizon curve's sampled bearings,
    wrapping around 360 -> 0 since bearing is circular."""
    n = len(curve)
    for i in range(n):
        b0, a0 = curve[i]
        b1, a1 = curve[(i + 1) % n]
        upper = b1 if b1 > b0 else b1 + 360
        target = bearing_deg if bearing_deg >= b0 else bearing_deg + 360
        if b0 <= target <= upper:
            span = upper - b0
            if span == 0:
                return a0
            frac = (target - b0) / span
            return a0 + frac * (a1 - a0)
    return curve[0][1]   # unreachable given a full 0-360 curve


# Digital modes that report SNR in dB (FT8, FT4/MFSK). Traditional-scale
# reports (CW/SSB 59-style, or a "599" CW-style typo landing in an FT8
# record) must never be mixed into dB-based stats — confirmed both by
# genuine mode mismatches (2 SSB + 2 FM contacts in the full log all use
# "59") and by at least one straight data-entry error (a real FT8 QSO
# with rst_rcvd logged as "599", a CW-style report, by mistake).
_DB_REPORT_MODES = {"FT8", "MFSK"}
_SANE_DB_RANGE = (-30.0, 30.0)   # generous bound around FT8/FT4's real -24..+20ish range


def parse_rst(value: Optional[str], mode: Optional[str]) -> Optional[float]:
    if not value or mode not in _DB_REPORT_MODES:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    lo, hi = _SANE_DB_RANGE
    if not (lo <= parsed <= hi):
        return None
    return parsed


def _make_row(
    qso: dict, my_grid: str, has_terrain: bool,
    horizon_curve: list[tuple[float, float]],
    fwd_w_avg: Optional[float] = None,
) -> Optional[dict]:
    their_grid = qso.get("gridsquare")
    if not their_grid:
        return None

    try:
        my_lat, my_lon = grid_to_latlon(my_grid)
        their_lat, their_lon = grid_to_latlon(their_grid)
    except (ValueError, IndexError):
        return None

    distance_km = haversine_distance_km(my_lat, my_lon, their_lat, their_lon)
    bearing_deg = initial_bearing_deg(my_lat, my_lon, their_lat, their_lon)
    takeoff_angle_deg = interpolate_takeoff_angle(bearing_deg, horizon_curve) if has_terrain else None

    mode = qso.get("mode")
    rst_sent = parse_rst(qso.get("rst_sent"), mode)
    rst_rcvd = parse_rst(qso.get("rst_rcvd"), mode)
    asymmetry_db = (rst_sent - rst_rcvd) if rst_sent is not None and rst_rcvd is not None else None

    return {
        "qso_date": qso.get("qso_date"),
        "time_on_utc": qso.get("time_on"),
        "call": qso.get("call"),
        "band": qso.get("band"),
        "mode": mode,
        "my_gridsquare": my_grid,
        "gridsquare": their_grid,
        "distance_km": round(distance_km, 1),
        "bearing_deg": round(bearing_deg, 1),
        "takeoff_angle_deg": round(takeoff_angle_deg, 2) if takeoff_angle_deg is not None else None,
        "tx_pwr_logged_w": qso.get("tx_pwr"),
        "fwd_w_avg_telemetry": fwd_w_avg,
        "rst_sent": rst_sent,
        "rst_rcvd": rst_rcvd,
        "asymmetry_db": round(asymmetry_db, 1) if asymmetry_db is not None else None,
        "comment": qso.get("comment"),
    }


def build_rows_from_jsonl(qso_log_path: Path, horizon_curve: list[tuple[float, float]]) -> list[dict]:
    rows = []
    skipped_grid = 0

    with open(qso_log_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"Skipping malformed line {line_num}: {e}", file=sys.stderr)
                continue

            qso = record.get("qso", {})
            my_grid = qso.get("my_gridsquare")
            if not my_grid:
                skipped_grid += 1
                continue

            has_terrain = record.get("station_profile") == REQUIRED_STATION_PROFILE
            telemetry = record.get("telemetry", {})
            fwd_w_avg = telemetry.get("fwd_w", {}).get("avg")

            row = _make_row(qso, my_grid, has_terrain, horizon_curve, fwd_w_avg)
            if row is None:
                skipped_grid += 1
                continue
            rows.append(row)

    if skipped_grid:
        print(f"Skipped {skipped_grid} QSO(s) missing a usable grid square on one or both ends")
    return rows


def _assign_home_grid(qso_date_str: str) -> tuple[str, bool]:
    """Returns (grid, has_terrain) for a raw ADIF record's qso_date
    (YYYYMMDD), based on the known Boise/La Quinta seasonal switch."""
    d = date(int(qso_date_str[0:4]), int(qso_date_str[4:6]), int(qso_date_str[6:8]))
    if d >= QTH_CUTOVER_2026:
        return BOISE_GRID, True
    return LA_QUINTA_GRID, False


def build_rows_from_adif(adif_path: Path, horizon_curve: list[tuple[float, float]]) -> list[dict]:
    records = read_adif_file(str(adif_path))
    rows = []
    skipped_grid = 0
    skipped_date = 0

    for qso in records:
        qso_date_str = qso.get("qso_date", "")
        if len(qso_date_str) != 8:
            skipped_date += 1
            continue
        my_grid, has_terrain = _assign_home_grid(qso_date_str)

        row = _make_row(qso, my_grid, has_terrain, horizon_curve)
        if row is None:
            skipped_grid += 1
            continue
        rows.append(row)

    if skipped_grid:
        print(f"Skipped {skipped_grid} QSO(s) missing a usable grid square for the other station")
    if skipped_date:
        print(f"Skipped {skipped_date} QSO(s) with no parseable qso_date (can't assign home QTH)")
    return rows


def main():
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_QSO_LOG
    horizon_csv_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_HORIZON_CSV
    output_path = Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_OUTPUT

    if not input_path.exists():
        print(f"No input file at {input_path}")
        return
    if not horizon_csv_path.exists():
        print(f"No terrain horizon CSV at {horizon_csv_path}")
        return

    horizon_curve = load_horizon_curve(horizon_csv_path)

    if input_path.suffix.lower() in (".adif", ".adi"):
        print(f"Reading raw ADIF export: {input_path}")
        rows = build_rows_from_adif(input_path, horizon_curve)
    else:
        print(f"Reading QSO performance JSONL: {input_path}")
        rows = build_rows_from_jsonl(input_path, horizon_curve)

    if not rows:
        print("No matching QSO records found.")
        return

    with_terrain = sum(1 for r in rows if r["takeoff_angle_deg"] is not None)
    print(f"{len(rows)} rows total, {with_terrain} with a takeoff angle (Boise-assigned QSOs)")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0].keys())
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows -> {output_path}")


if __name__ == "__main__":
    main()
