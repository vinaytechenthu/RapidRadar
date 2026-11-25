"""
db_scan.service
---------------
Background scan session: scan → wait → scan … until stopped, then one final
scan and an HTML report.  Used by the web UI both for the standalone DB Scan
mode and for the combined "Page Load + DB Scan" mode.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Callable, Optional

from . import create_scanner
from .base import ConnectionConfig, col, to_float
from .report import ReportGenerator

LogFn = Callable[[str], None]


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("_") or "db"


class DBScanService:

    def __init__(self, reports_root: str, log: Optional[LogFn] = None) -> None:
        self.reports_root = os.path.abspath(reports_root)
        self._log_fn = log
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._state: dict = self._idle_state()

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _idle_state() -> dict:
        return {
            "running": False, "status": "idle", "mode": None,
            "db_type": None, "db_type_label": None, "server": None, "database": None,
            "interval": 60, "scope": "cumulative", "window_start": None,
            "scan_count": 0, "last_scan_time": None, "next_scan_in": None,
            "scan_error": None, "health_score": None,
            "latest_report_url": None, "report_path": None,
        }

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def is_busy(self) -> bool:
        with self._lock:
            return self._state["running"] or self._state["status"] in ("starting", "generating", "final-scan")

    def start(self, cfg: ConnectionConfig, interval: int = 60, scope: str = "cumulative",
              output_dir: Optional[str] = None, mode: str = "standalone",
              context: Optional[dict] = None) -> tuple[bool, str]:
        ok, msg = cfg.validate()
        if not ok:
            return False, msg
        if self.is_busy():
            return False, "A database scan is already running."
        interval = max(10, min(int(interval), 3600))
        if output_dir is None:
            stamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
            output_dir = os.path.join(self.reports_root,
                                      f"DBScan_{cfg.db_type}_{_safe_name(cfg.database)}_{stamp}")
        with self._lock:
            self._state = self._idle_state()
            self._state.update({
                "running": True, "status": "starting", "mode": mode,
                "db_type": cfg.db_type, "db_type_label": cfg.label,
                "server": cfg.server, "database": cfg.database,
                "interval": interval, "scope": scope,
            })
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="dbscan",
            args=(cfg, interval, scope, os.path.abspath(output_dir), context or {}), daemon=True)
        self._thread.start()
        return True, "Database scan started."

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self._state["running"]:
                return False, "Database scan is not running."
            self._state["running"] = False
        self._stop_evt.set()
        return True, "Stop signalled — a final scan and the report will follow."

    def wait(self, timeout: Optional[float] = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _log(self, msg: str) -> None:
        if self._log_fn:
            try:
                self._log_fn(f"[DB] {msg}\n")
            except Exception:                         # noqa: BLE001
                pass

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _run(self, cfg: ConnectionConfig, interval: int, scope: str, out_dir: str,
             context: dict) -> None:
        scanner = create_scanner(cfg)
        history: list[dict] = []
        latest: Optional[dict] = None
        self._log(f"Target  : {cfg.describe()}")

        if scope == "window":
            try:
                scanner.begin_window()
                self._set(window_start=str(scanner.window_start))
                self._log(f"Analysis window starts at {scanner.window_start} (server time). "
                          "Only activity after this point is reported.")
            except Exception as exc:                  # noqa: BLE001
                self._set(running=False, status="error", scan_error=f"Could not connect: {exc}")
                self._log(f"[ERROR] Could not connect: {exc}")
                return

        while True:
            if self._stop_evt.is_set():
                break
            self._set(status="scanning", scan_error=None)
            latest = self._scan_once(scanner, history) or latest
            for remaining in range(interval, 0, -1):
                if self._stop_evt.is_set():
                    break
                self._set(status="waiting", next_scan_in=remaining)
                time.sleep(1)
            self._set(next_scan_in=None)

        # Final scan so the report covers the whole window, then the report.
        self._set(status="final-scan")
        self._log("Taking final scan …")
        latest = self._scan_once(scanner, history) or latest
        if latest is None:
            self._set(status="stopped", running=False,
                      scan_error="No scan completed successfully — no report generated.")
            self._log("[ERROR] No scan completed successfully — no report generated.")
            return
        self._set(status="generating")
        try:
            path, score = self._write_report(latest, history, out_dir, context)
            self._set(report_path=path, latest_report_url=self._url_for(path), health_score=score)
            self._log(f"✓ {latest.get('db_type_label')} report saved (health {score}/100) → {path}")
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            self._set(scan_error=f"Report generation failed: {exc}")
            self._log(f"[ERROR] Report generation failed: {exc}")
        self._set(status="stopped", running=False)

    def _scan_once(self, scanner, history: list) -> Optional[dict]:
        n = len(history) + 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            res = scanner.run_full_scan()
        except Exception as exc:                      # noqa: BLE001
            history.append({"time": now, "error": str(exc)[:200]})
            self._set(scan_error=str(exc))
            self._log(f"[ERROR] Scan #{n} failed: {exc}")
            return None
        entry = _history_entry(res, now)
        history.append(entry)
        with self._lock:
            self._state["scan_count"] += 1
            self._state["last_scan_time"] = now
            self._state["db_type_label"] = res.get("db_type_label")
        self._log(f"Scan #{n} complete — sessions: {entry.get('total_sessions', '?')} "
                  f"(active {entry.get('running_sessions', '?')}), "
                  f"queries > 1 s: {entry.get('slow_over_1s', '?')}")
        return res

    def _write_report(self, results: dict, history: list, out_dir: str,
                      context: dict) -> tuple[str, Optional[int]]:
        os.makedirs(out_dir, exist_ok=True)
        gen = ReportGenerator(results, history=history, context=context)
        html_out = gen.generate()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"DBScan_{results.get('db_type')}_{_safe_name(results.get('database', ''))}_{stamp}"
        path = os.path.join(out_dir, base + ".html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html_out)
        with open(os.path.join(out_dir, base + "_data.json"), "w", encoding="utf-8") as fh:
            json.dump({"results": results, "history": history, "health_score": gen.health_score},
                      fh, default=str, indent=1)
        return path, gen.health_score

    def _url_for(self, path: str) -> Optional[str]:
        try:
            rel = os.path.relpath(path, self.reports_root)
        except ValueError:                            # different drive on Windows
            return None
        if rel.startswith(".."):
            return None
        return "/reports/" + rel.replace("\\", "/")


def _history_entry(res: dict, when: str) -> dict:
    entry: dict = {"time": when}
    summ = (res.get("connections") or {}).get("summary") or {}
    if summ.get("success") and summ.get("rows"):
        row = summ["rows"][0]
        entry["total_sessions"] = col(summ, row, "total_sessions")
        entry["running_sessions"] = col(summ, row, "running_sessions")
        entry["open_txn"] = col(summ, row, "sessions_with_open_txn")
    sq = res.get("slow_queries") or {}
    if sq.get("success"):
        avgs = [to_float(col(sq, r, "avg_elapsed_ms")) for r in sq.get("rows") or []]
        entry["slow_over_1s"] = sum(1 for a in avgs if a > 1000)
        entry["max_avg_ms"] = round(max(avgs), 1) if avgs else 0
    cpu = res.get("cpu_utilization") or {}
    if cpu.get("success") and cpu.get("rows"):
        entry["cpu_pct"] = col(cpu, cpu["rows"][0], "sql_cpu_pct")
    return entry
