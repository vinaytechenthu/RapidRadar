"""
performance_framework/process_monitor.py
=========================================
Lightweight background monitor that snapshots CPU and memory for three
key processes during a test run:

  • CADIS.exe   – App Server  (queried via windows_exporter HTTP on port 9182)
  • sqlservr.exe – DB Server   (queried via windows_exporter HTTP on port 9182)
  • Browser      – Web Server  (local psutil, matching the active Playwright browser)

Remote monitoring uses the windows_exporter Prometheus endpoint:
  http://<ip>:9182/metrics?collect[]=process

No extra Python packages are required — stdlib urllib is used for HTTP.
Requires windows_exporter to be installed and running on the App/DB servers
with the process collector enabled (the default).

Sampling runs in a daemon thread every ``interval_s`` seconds.
Call ``start()`` before running tests and ``stop()`` / ``get_stats()`` afterwards.
"""

import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

# ── Browser key → executable name fragments (case-insensitive match) ──────────
_BROWSER_EXE_MAP: Dict[str, List[str]] = {
    "chrome":   ["chrome"],
    "chromium": ["chromium"],
    "edge":     ["msedge"],
    "firefox":  ["firefox"],
    "webkit":   ["webkit"],
    "safari":   ["safari"],
}


# ── Process finders ────────────────────────────────────────────────────────────

def _find_by_pid(pid: int) -> "Optional[psutil.Process]":
    """Return the process with the exact *pid*, or None if it no longer exists."""
    if not _PSUTIL_AVAILABLE:
        return None
    try:
        proc = psutil.Process(pid)
        proc.status()  # raises NoSuchProcess if gone
        return proc
    except Exception:
        return None



def _find_by_exe(names: List[str]) -> "Optional[psutil.Process]":
    """Return the first process whose name/exe path contains any of *names*."""
    if not _PSUTIL_AVAILABLE:
        return None
    try:
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            pname = (proc.info.get("name") or "").lower()
            pexe  = (proc.info.get("exe")  or "").lower()
            for n in names:
                if n in pname or n in pexe:
                    return proc
    except Exception:
        pass
    return None


def _snapshot(proc: "Optional[psutil.Process]") -> "Optional[Dict[str, float]]":
    """Non-blocking CPU + RSS memory snapshot; returns None when unavailable."""
    if proc is None or not _PSUTIL_AVAILABLE:
        return None
    try:
        cpu = proc.cpu_percent(interval=0)          # non-blocking
        mem = proc.memory_info().rss / (1024 * 1024)
        return {"cpu_pct": cpu, "mem_mb": mem}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


# ── Prometheus text-format parser ──────────────────────────────────────────────────

def _parse_prom_sum(text: str, metric: str, process_filter: str) -> "Optional[float]":
    """
    Sum all values of *metric* whose ``process`` label equals *process_filter*
    or starts with ``process_filter + '#'`` (windows_exporter appends ``#N``
    for multiple instances of the same process name).
    """
    total = 0.0
    found = False
    prefix = metric + "{"
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        m = re.search(r'process="([^"]+)"', line)
        if not m:
            continue
        pname = m.group(1).lower()
        if pname != process_filter and not pname.startswith(process_filter + "#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) < 2:
            continue
        try:
            total += float(parts[1])
            found = True
        except ValueError:
            continue
    return total if found else None


# ── windows_exporter HTTP monitor ───────────────────────────────────────────

class _WindowsExporterMonitor:
    """
    Queries the windows_exporter Prometheus endpoint (port 9182 by default)
    on a remote Windows host to get CPU and memory stats for a named process.

    No special credentials are needed — just HTTP access on port 9182.
    Requires windows_exporter running on the target host with the process
    collector enabled (enabled by default).

    Install windows_exporter on each remote host:
      https://github.com/prometheus-community/windows_exporter/releases
      Default service port: 9182
    """

    _METRICS_PATH = "/metrics?collect[]=process"

    def __init__(self, host: str, port: int = 9182) -> None:
        self._host = host
        self._port = port
        self._url  = f"http://{host}:{port}{self._METRICS_PATH}"
        self._connected: bool = False
        self._error: Optional[str] = None
        # Track previous CPU counter values to compute delta %
        self._prev_cpu:  Dict[str, float] = {}
        self._prev_time: float = 0.0
        self._test_connection()

    def _test_connection(self) -> None:
        try:
            # Lightweight probe using the root endpoint
            probe = f"http://{self._host}:{self._port}/"
            with urllib.request.urlopen(probe, timeout=5) as resp:
                self._connected = resp.status == 200
                self._error = None
        except Exception as exc:
            self._connected = False
            self._error = str(exc)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def error(self) -> Optional[str]:
        return self._error

    def snapshot(self, process_name: str) -> "Optional[Dict[str, float]]":
        """
        Return {"cpu_pct": float, "mem_mb": float} for *process_name*.

        CPU% is computed from the counter delta between two successive calls
        (the first call after connect always returns cpu_pct=0.0).
        """
        proc_key = process_name.lower().replace(".exe", "")
        try:
            with urllib.request.urlopen(self._url, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            self._connected = True
            self._error = None
        except Exception as exc:
            self._connected = False
            self._error = str(exc)
            return None

        now = time.monotonic()

        # Working set (RSS equivalent) in bytes
        mem_bytes = _parse_prom_sum(raw, "windows_process_working_set_bytes", proc_key)
        if mem_bytes is None:
            return None

        # CPU time counter (user + privileged combined)
        cpu_total = _parse_prom_sum(raw, "windows_process_cpu_time_total", proc_key)

        cpu_pct = 0.0
        if (
            cpu_total is not None
            and proc_key in self._prev_cpu
            and self._prev_time > 0
        ):
            delta_cpu  = max(cpu_total - self._prev_cpu[proc_key], 0.0)
            delta_time = max(now - self._prev_time, 0.001)
            cpu_pct = (delta_cpu / delta_time) * 100.0

        if cpu_total is not None:
            self._prev_cpu[proc_key] = cpu_total
        self._prev_time = now

        return {
            "cpu_pct": cpu_pct,
            "mem_mb":  mem_bytes / (1024 * 1024),
        }


# ── Main class ─────────────────────────────────────────────────────────────────

class ProcessMonitor:
    """
    Background sampler for CADIS.exe (App Server), sqlservr.exe (DB Server),
    and Chrome.exe (Web Server / local).

    When *app_server_ip* / *db_server_ip* are provided the respective process
    is monitored on the remote host via WMI.  When no IP is supplied, local
    psutil is used as a fallback (single-machine deployments).

    Usage::

        mon = ProcessMonitor(
            browser_key="chrome",
            app_server_ip="10.0.1.50",
            db_server_ip="10.0.1.60",
        )
        mon.start()
        # … run tests …
        mon.stop()
        stats = mon.get_stats()   # dict keyed "edm" / "sql" / "browser"
    """

    def __init__(
        self,
        browser_key: str = "chrome",
        app_port: int = 8087,   # kept for backward-compat, no longer used
        interval_s: float = 2.0,
        app_server_ip: Optional[str] = None,
        db_server_ip: Optional[str] = None,
        remote_username: Optional[str] = None,   # kept for backward-compat, unused
        remote_password: Optional[str] = None,   # kept for backward-compat, unused
    ) -> None:
        self.browser_key = browser_key.lower()
        self.interval_s  = interval_s
        self._app_server_ip   = (app_server_ip or "").strip()
        self._db_server_ip    = (db_server_ip or "").strip()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Remote windows_exporter monitors (None = use local psutil instead)
        self._app_exporter: Optional[_WindowsExporterMonitor] = None
        self._db_exporter:  Optional[_WindowsExporterMonitor] = None

        # Internal accumulator per process key
        self._data: Dict[str, Dict[str, Any]] = {
            "edm":     {"cpu": [], "mem": [], "proc": None, "name": "—", "pid": None},
            "sql":     {"cpu": [], "mem": [], "proc": None, "name": "—", "pid": None},
            "browser": {"cpu": [], "mem": [], "proc": None, "name": "—", "pid": None},
        }

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Connect to / locate known processes and begin background sampling."""
        # ── CADIS.exe on App Server (windows_exporter HTTP) ───────────────
        if self._app_server_ip:
            self._app_exporter = _WindowsExporterMonitor(self._app_server_ip)
            if self._app_exporter.connected:
                self._data["edm"]["name"] = f"CADIS.exe @ {self._app_server_ip}"
                print(
                    f"  [MONITOR] Connected to App Server "
                    f"({self._app_server_ip}:9182) – monitoring CADIS.exe"
                )
            else:
                print(
                    f"  [MONITOR] WARNING: Could not reach windows_exporter on App Server "
                    f"({self._app_server_ip}:9182): {self._app_exporter.error}"
                )
        elif _PSUTIL_AVAILABLE:
            # Single-machine deployment: CADIS.exe is local
            self._init_proc("edm", _find_by_exe(["cadis"]))

        # ── sqlservr.exe on DB Server (windows_exporter HTTP) ─────────────
        if self._db_server_ip:
            self._db_exporter = _WindowsExporterMonitor(self._db_server_ip)
            if self._db_exporter.connected:
                self._data["sql"]["name"] = f"sqlservr.exe @ {self._db_server_ip}"
                print(
                    f"  [MONITOR] Connected to DB Server "
                    f"({self._db_server_ip}:9182) – monitoring sqlservr.exe"
                )
            else:
                print(
                    f"  [MONITOR] WARNING: Could not reach windows_exporter on DB Server "
                    f"({self._db_server_ip}:9182): {self._db_exporter.error}"
                )
        elif _PSUTIL_AVAILABLE:
            # Single-machine deployment: SQL Server is local
            self._init_proc("sql", _find_by_exe(["sqlservr"]))

        # ── Chrome.exe on Web Server (always local via psutil) ────────────
        if _PSUTIL_AVAILABLE:
            exe_names = _BROWSER_EXE_MAP.get(self.browser_key, [self.browser_key])
            self._init_proc("browser", _find_by_exe(exe_names))

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the sampling thread to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_stats(self) -> Dict[str, Any]:
        """
        Return a summary dict with one entry per process key
        (``"edm"``, ``"sql"``, ``"browser"``).

        Each entry contains::

            {
                "label":        str,            # human-readable process name
                "found":        bool,
                "pid":          int | None,
                "name":         str,            # OS process name
                "cpu_avg":      float | None,   # average % during test
                "cpu_max":      float | None,   # peak % during test
                "mem_mb":       float | None,   # RSS at last sample
                "mem_peak_mb":  float | None,   # peak RSS during test
                "samples":      int,
            }
        """
        browser_label = _BROWSER_EXE_MAP.get(self.browser_key, [self.browser_key])[0].replace(
            "msedge", "Edge"
        ).title() + " Browser (Web Server)"

        edm_label = (
            f"CADIS (App Server: {self._app_server_ip})"
            if self._app_server_ip else "CADIS (Local)"
        )
        sql_label = (
            f"SQL Server (DB Server: {self._db_server_ip})"
            if self._db_server_ip else "SQL Server (Local)"
        )

        labels = {
            "edm":     edm_label,
            "sql":     sql_label,
            "browser": browser_label,
        }

        result: Dict[str, Any] = {}
        for key, d in self._data.items():
            cpu_s = [c for c in d["cpu"] if c > 0]
            mem_s = d["mem"]
            if key == "edm":
                found = (d["proc"] is not None) or (
                    self._app_exporter is not None and len(d["mem"]) > 0
                )
            elif key == "sql":
                found = (d["proc"] is not None) or (
                    self._db_exporter is not None and len(d["mem"]) > 0
                )
            else:
                found = d["proc"] is not None
            result[key] = {
                "label":       labels[key],
                "found":       found,
                "pid":         d["pid"],
                "name":        d["name"] or "—",
                "cpu_avg":     round(sum(cpu_s) / len(cpu_s), 1) if cpu_s else None,
                "cpu_max":     round(max(cpu_s), 1)              if cpu_s else None,
                "mem_mb":      round(mem_s[-1], 1)               if mem_s else None,
                "mem_peak_mb": round(max(mem_s), 1)              if mem_s else None,
                "samples":     len(mem_s),
            }
        return result

    # ------------------------------------------------------------------
    def _init_proc(self, key: str, proc: "Optional[psutil.Process]") -> None:
        if proc is None:
            return
        try:
            self._data[key]["proc"] = proc
            self._data[key]["name"] = proc.name()
            self._data[key]["pid"]  = proc.pid
            proc.cpu_percent(interval=0)          # first call always returns 0 – discard
        except Exception:
            self._data[key]["proc"] = None

    def _loop(self) -> None:
        exe_names = _BROWSER_EXE_MAP.get(self.browser_key, [self.browser_key])
        time.sleep(self.interval_s)               # skip the warm-up period

        while not self._stop_event.is_set():
            # ── CADIS.exe (App Server) ────────────────────────────────────
            edm_d = self._data["edm"]
            if self._app_exporter is not None:
                snap = self._app_exporter.snapshot("CADIS.exe")
                if snap:
                    edm_d["cpu"].append(snap["cpu_pct"])
                    edm_d["mem"].append(snap["mem_mb"])
            elif edm_d["proc"] is not None:
                snap = _snapshot(edm_d["proc"])
                if snap:
                    edm_d["cpu"].append(snap["cpu_pct"])
                    edm_d["mem"].append(snap["mem_mb"])

            # ── sqlservr.exe (DB Server) ──────────────────────────────────
            sql_d = self._data["sql"]
            if self._db_exporter is not None:
                snap = self._db_exporter.snapshot("sqlservr.exe")
                if snap:
                    sql_d["cpu"].append(snap["cpu_pct"])
                    sql_d["mem"].append(snap["mem_mb"])
            elif sql_d["proc"] is not None:
                snap = _snapshot(sql_d["proc"])
                if snap:
                    sql_d["cpu"].append(snap["cpu_pct"])
                    sql_d["mem"].append(snap["mem_mb"])

            # ── Chrome.exe (Web Server — local) ──────────────────────────
            br_d = self._data["browser"]
            if br_d["proc"] is None and _PSUTIL_AVAILABLE:
                self._init_proc("browser", _find_by_exe(exe_names))
            snap = _snapshot(br_d["proc"])
            if snap:
                br_d["cpu"].append(snap["cpu_pct"])
                br_d["mem"].append(snap["mem_mb"])

            self._stop_event.wait(self.interval_s)
