"""
db_scan.pages
-------------
Page ↔ query correlation for the combined "Page Load + DB Scan" mode.

The page-load runner announces every page boundary (``@@RR_PAGE@@`` lines,
see run_test.py ``--page-events``) and waits for an acknowledgement.  At each
boundary the tracker takes a snapshot of the engine's cumulative per-statement
counters; the difference between the "start" and "end" snapshots of a page is
exactly the database work executed while that page was being tested.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from . import create_scanner
from .base import ConnectionConfig

LogFn = Callable[[str], None]

TOP_QUERIES_PER_PAGE = 10


def diff_snapshots(before: dict, after: dict) -> list[dict]:
    """Statements executed between two snapshots, heaviest (total time) first."""
    out = []
    for key, a in after.items():
        b = before.get(key)
        execs = a["executions"] - (b["executions"] if b else 0)
        elapsed = a["elapsed_ms"] - (b["elapsed_ms"] if b else 0.0)
        cpu = a.get("cpu_ms")
        reads = a.get("logical_reads")
        if b is not None and execs < 0:
            # Counters were reset / plan re-cached in between: count the new values only.
            execs, elapsed = a["executions"], a["elapsed_ms"]
            b = None
        if execs <= 0:
            continue
        if b is not None:
            cpu = None if cpu is None or b.get("cpu_ms") is None else cpu - b["cpu_ms"]
            reads = None if reads is None or b.get("logical_reads") is None \
                else reads - b["logical_reads"]
        elapsed = max(elapsed, 0.0)
        out.append({
            "query_text": a["text"],
            "executions": execs,
            "total_ms": round(elapsed, 2),
            "avg_ms": round(elapsed / execs, 2),
            "cpu_ms": None if cpu is None else round(max(cpu, 0.0), 2),
            "logical_reads": None if reads is None else int(max(reads, 0)),
        })
    out.sort(key=lambda r: r["total_ms"], reverse=True)
    return out


class PageTracker:
    """Collects per-page DB activity during one combined run."""

    def __init__(self, cfg: ConnectionConfig, log: Optional[LogFn] = None) -> None:
        self.cfg = cfg
        self._log_fn = log
        self._lock = threading.Lock()
        self._scanner = create_scanner(cfg)
        self._current: Optional[dict] = None
        self.pages: list[dict] = []
        self.enabled = True
        self.error: Optional[str] = None

    # ------------------------------------------------------------------ #

    def _log(self, msg: str) -> None:
        if self._log_fn:
            try:
                self._log_fn(f"[DB] {msg}\n")
            except Exception:                         # noqa: BLE001
                pass

    def begin(self) -> bool:
        """Open the snapshot connection and limit snapshots to this run's activity."""
        try:
            self._scanner.begin_window()
            self._scanner.query_snapshot()            # validates privileges / feature
            return True
        except Exception as exc:                      # noqa: BLE001
            self.enabled = False
            self.error = str(exc)[:300]
            self._log(f"Per-page query tracking unavailable: {self.error}")
            self._scanner.close_snapshots()
            return False

    def _snapshot(self) -> Optional[dict]:
        try:
            return self._scanner.query_snapshot()
        except Exception as exc:                      # noqa: BLE001
            self.error = str(exc)[:300]
            self._log(f"[WARN] Query snapshot failed: {self.error}")
            return None

    def handle_event(self, event: dict) -> None:
        """Process one page event from run_test.py (``start`` / ``end``)."""
        if not self.enabled:
            return
        with self._lock:
            kind = event.get("event")
            if kind == "start":
                self._current = {"event": event, "before": self._snapshot(),
                                 "t0": time.monotonic(),
                                 "started": datetime.now().strftime("%H:%M:%S")}
            elif kind == "end" and self._current is not None:
                after = self._snapshot()
                cur, self._current = self._current, None
                duration = time.monotonic() - cur["t0"]
                page = {
                    "index": event.get("index"),
                    "total": event.get("total"),
                    "url": event.get("url"),
                    "name": event.get("name"),
                    "started": cur["started"],
                    "duration_s": round(duration, 1),
                    "browsers": event.get("browsers") or [],
                    "error": event.get("error"),
                }
                if cur["before"] is None or after is None:
                    page["db"] = None
                else:
                    stmts = diff_snapshots(cur["before"], after)
                    cpu_vals = [s["cpu_ms"] for s in stmts if s["cpu_ms"] is not None]
                    page["db"] = {
                        "statements": len(stmts),
                        "executions": sum(s["executions"] for s in stmts),
                        "total_ms": round(sum(s["total_ms"] for s in stmts), 2),
                        "cpu_ms": round(sum(cpu_vals), 2) if cpu_vals else None,
                        "slowest_avg_ms": max((s["avg_ms"] for s in stmts), default=0),
                        "top": stmts[:TOP_QUERIES_PER_PAGE],
                    }
                    self._log(f"Page {page['index']}/{page['total']} '{page['name']}': "
                              f"{page['db']['executions']} DB executions, "
                              f"{page['db']['total_ms'] / 1000:.2f} s DB time")
                self.pages.append(page)

    def close(self) -> None:
        self._scanner.close_snapshots()

    def finish(self, run_dir: str) -> Optional[str]:
        self.close()
        if not self.pages and self.error is None:
            return None
        path = os.path.join(run_dir, "page_db_correlation.json")
        try:
            os.makedirs(run_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"db_type": self.cfg.db_type, "db_type_label": self.cfg.label,
                           "error": self.error, "pages": self.pages}, fh, indent=1, default=str)
            return path
        except Exception as exc:                      # noqa: BLE001
            self._log(f"[WARN] Could not save page correlation: {exc}")
            return None
