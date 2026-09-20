"""
Trend CSV Logger — persists AcomBridge trend samples to disk while the
Monitor view is open.

The console keeps trend data in memory only (a 10-minute ring buffer) —
fine for the live strip charts, but it's gone on restart or once a sample
ages out. This writes every sample to CSV for as long as at least one
browser has /monitor open, so it can be reviewed later in Excel.

Liveness is heartbeat-based (see server.py's "monitor_heartbeat" ws
command) rather than tied to a specific WebSocket connection, since
/monitor shares the same /ws endpoint as the dashboard and a page
refresh/reconnect shouldn't be treated as "Monitor closed".
"""

import csv
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CSV_DIR = Path(__file__).resolve().parent.parent / "data" / "trend_logs"

CSV_FIELDS = ["ts_iso", "fwd_w", "refl_w", "swr", "temp_c", "drive_w", "current_a", "is_tx",
              "alc", "rf_power_pct"]


class TrendCsvLogger:
    def __init__(self):
        self._file = None
        self._writer = None
        self.path: Optional[Path] = None
        self.active = False

    def start(self):
        if self.active:
            return
        try:
            CSV_DIR.mkdir(parents=True, exist_ok=True)
            self.path = CSV_DIR / f"trend_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            self._file = open(self.path, "w", newline="")
            self._writer = csv.writer(self._file)
            self._writer.writerow(CSV_FIELDS)
            self._file.flush()
            self.active = True
            logger.info(f"Trend CSV logging started: {self.path}")
        except OSError as e:
            logger.error(f"Could not start trend CSV logging: {e}")
            self._file = None
            self._writer = None
            self.active = False

    def log(self, sample):
        """sample: AcomBridge.TrendSample — called synchronously from the
        bridge's telemetry handler, so any failure here must never raise."""
        if not self.active or self._writer is None:
            return
        try:
            self._writer.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(sample.ts)),
                round(sample.fwd_w, 1), round(sample.refl_w, 1), round(sample.swr, 2),
                round(sample.temp_c, 1), round(sample.drive_w, 1),
                round(sample.current_a, 2), 1 if sample.is_tx else 0,
                round(sample.alc, 3), sample.rf_power_pct,
            ])
            self._file.flush()
        except OSError as e:
            logger.error(f"Trend CSV write failed, stopping logger: {e}")
            self.stop()

    def stop(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            logger.info(f"Trend CSV logging stopped: {self.path}")
        self._file = None
        self._writer = None
        self.active = False
