"""
db_scan.base
------------
Engine-neutral building blocks shared by every database scanner.

Every concrete scanner (SQL Server, PostgreSQL, Oracle, MySQL) returns the
*same* result structure from ``run_full_scan()`` so that one report generator
can render any engine.  Each section is a "result table":

    {"success": bool, "columns": [...], "rows": [[...], ...], "error": str|None}

and may additionally carry ``"unsupported": True`` when the engine simply does
not expose that metric (rendered as an info note rather than an error).

Normalised column names that the report / health score rely on
----------------------------------------------------------------
slow_queries         avg_elapsed_ms, avg_cpu_ms (optional), query_text
missing_indexes      priority  (HIGH / MEDIUM / LOW)
index_fragmentation  fragmentation_pct, fix_script (optional)
unused_indexes       drop_script (optional)
iops                 avg_read_latency_ms / avg_write_latency_ms (optional)
cpu_utilization      sample_time, sql_cpu_pct, other_cpu_pct, system_idle_pct
connections.summary  total_sessions, running_sessions, sleeping_sessions,
                     background_sessions, sessions_with_open_txn, total_memory_kb
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Supported engines
# ---------------------------------------------------------------------------

DB_TYPES: dict[str, dict[str, Any]] = {
    "sqlserver":  {"label": "Microsoft SQL Server", "short": "SQL Server", "port": 1433,
                   "driver_pkg": "pyodbc",          "icon": "bi-microsoft"},
    "postgresql": {"label": "PostgreSQL",           "short": "PostgreSQL", "port": 5432,
                   "driver_pkg": "psycopg2-binary", "icon": "bi-database"},
    "oracle":     {"label": "Oracle Database",      "short": "Oracle",     "port": 1521,
                   "driver_pkg": "oracledb",        "icon": "bi-database-fill"},
    "mysql":      {"label": "MySQL / MariaDB",      "short": "MySQL",      "port": 3306,
                   "driver_pkg": "pymysql",         "icon": "bi-database-gear"},
}


MARKER = "RapidRadar DBScan"      # tag added to every diagnostic statement


def is_own_query(text: Any) -> bool:
    return MARKER in str(text or "")


def db_type_label(db_type: str) -> str:
    return DB_TYPES.get(db_type, {}).get("label", db_type or "Unknown")


# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_SAFE_SERVER_RE = re.compile(r"^[\w\.\\\-,:]+$")      # host, host\instance, host,port
_SAFE_NAME_RE   = re.compile(r"^[\w\-\. $#]+$")       # db / service / schema names
_CTRL_RE        = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class ConnectionConfig:
    db_type:  str
    server:   str
    database: str
    port:     Optional[int] = None
    schema:   str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    auth:     str = "sql"            # SQL Server only: "windows" | "sql"

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionConfig":
        port_raw = str(data.get("port", "") or "").strip()
        port: Optional[int] = None
        if port_raw:
            try:
                port = int(port_raw)
            except ValueError:
                port = -1                        # flagged by validate()
        return cls(
            db_type =str(data.get("db_type", "sqlserver") or "sqlserver").strip().lower(),
            server  =str(data.get("server", "") or "").strip(),
            database=str(data.get("database", "") or "").strip(),
            port    =port,
            schema  =str(data.get("schema", "") or "").strip(),
            username=str(data.get("username", "") or "").strip(),
            password=str(data.get("password", "") or ""),
            auth    =str(data.get("auth", "sql") or "sql").strip().lower(),
        )

    @property
    def label(self) -> str:
        return db_type_label(self.db_type)

    @property
    def effective_port(self) -> Optional[int]:
        return self.port or DB_TYPES.get(self.db_type, {}).get("port")

    def validate(self) -> tuple[bool, str]:
        if self.db_type not in DB_TYPES:
            return False, f"Unsupported database type '{self.db_type}'."
        if not self.server or not self.database:
            return False, "Server/host and database name are required."
        if len(self.server) > 128 or len(self.database) > 128 or len(self.schema) > 128:
            return False, "Server, database or schema name is too long."
        if not _SAFE_SERVER_RE.match(self.server):
            return False, "Server name contains invalid characters."
        if not _SAFE_NAME_RE.match(self.database):
            return False, "Database / service name contains invalid characters."
        if self.schema and not _SAFE_NAME_RE.match(self.schema):
            return False, "Schema name contains invalid characters."
        if self.port is not None and not (1 <= self.port <= 65535):
            return False, "Port must be a number between 1 and 65535."
        if _CTRL_RE.search(self.username) or len(self.username) > 128:
            return False, "Username is invalid."
        if len(self.password) > 256:
            return False, "Password is too long."
        needs_login = not (self.db_type == "sqlserver" and self.auth == "windows")
        if needs_login and not self.username:
            return False, f"Username is required for {self.label}."
        return True, ""

    def describe(self) -> str:
        """Human-readable target description (never includes the password)."""
        port = f":{self.effective_port}" if self.db_type != "sqlserver" or self.port else ""
        return f"{self.label} — {self.server}{port} / {self.database}"


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def ok_result(columns: list[str], rows: list[list]) -> dict:
    return {"success": True, "columns": list(columns), "rows": rows, "error": None}


def err_result(message: str) -> dict:
    return {"success": False, "columns": [], "rows": [], "error": message}


def unsupported_result(message: str) -> dict:
    return {"success": False, "unsupported": True, "columns": [], "rows": [], "error": message}


def kv_result(pairs: list[tuple[str, Any]]) -> dict:
    """Key/value result used for the 'Database Information' section."""
    return ok_result(["property", "value"], [[k, v] for k, v in pairs if v is not None])


def col(result: dict, row: list, name: str, default: Any = None) -> Any:
    try:
        return row[result["columns"].index(name)]
    except (ValueError, IndexError, KeyError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Base scanner
# ---------------------------------------------------------------------------

class BaseDBScanner:
    """Common workflow; subclasses implement the engine-specific queries."""

    db_type: str = ""

    def __init__(self, cfg: ConnectionConfig) -> None:
        self.cfg = cfg
        self.window_start: Optional[datetime] = None    # server-clock time
        self.driver_name: str = ""
        self.server_version: str = ""
        self._heavy_cache: dict[str, tuple[dict, Any]] = {}

    def _heavy(self, key: str, notes: dict, fn, *args) -> dict:
        """Run an expensive check (e.g. index fragmentation) only once per session.

        Physical index structure barely changes during a test run, and these
        checks can read a lot of data, so repeated scans reuse the first result.
        """
        if key in self._heavy_cache:
            res, note = self._heavy_cache[key]
            if note:
                notes[key] = note
            return res
        try:
            res = fn(*args)
        except Exception as exc:                     # noqa: BLE001  never fail the whole scan
            return err_result(f"Check failed: {_short_error(exc)}")
        if res.get("success"):
            self._heavy_cache[key] = (res, notes.get(key))
        return res

    # -- to be implemented by subclasses -------------------------------- #

    def _connect(self):                              # pragma: no cover
        raise NotImplementedError

    def _version_and_time(self, cursor) -> tuple[str, Any]:   # pragma: no cover
        """Return (version string, current server timestamp)."""
        raise NotImplementedError

    def _collect(self, cursor) -> dict:              # pragma: no cover
        """Return the engine-specific section dict (see module docstring)."""
        raise NotImplementedError

    def _on_begin_window(self, cursor) -> None:
        """Optional hook: capture engine-specific baselines."""

    def _query_counters(self, cursor) -> dict:
        """Cumulative per-statement counters used for page ↔ query correlation.

        Must return a result dict with the columns
        ``k, executions, elapsed_ms, cpu_ms, logical_reads, query_text``
        (cpu_ms / logical_reads may be NULL).  Only statements that ran since
        ``self.window_start`` need to be included when it is set.
        """
        return unsupported_result(f"Per-page query tracking is not available for {self.label}.")

    def _prepare_snapshots(self, cursor) -> None:
        """Optional hook run once per snapshot connection (feature detection)."""

    # -- shared helpers ------------------------------------------------- #

    @property
    def label(self) -> str:
        return self.cfg.label

    def _safe_query(self, cursor, sql: str, params: Any = None) -> dict:
        """Execute *sql* and return a uniform result dict. Never raises.

        Every statement is tagged with MARKER so the scanners can exclude
        RapidRadar's own diagnostic queries from the slow-query results.
        """
        sql = f"/* {MARKER} */ {sql.strip()}"
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
            cols = [d[0].lower() if isinstance(d[0], str) else str(d[0])
                    for d in cursor.description]
            rows = [list(r) for r in cursor.fetchall()]
            return ok_result(cols, rows)
        except Exception as exc:                     # noqa: BLE001
            self._after_error(cursor)
            return err_result(_short_error(exc))

    def _after_error(self, cursor) -> None:
        """Hook to reset connection state after a failed statement."""

    def _scalar(self, cursor, sql: str, params: Any = None, default: Any = None) -> Any:
        res = self._safe_query(cursor, sql, params)
        if res["success"] and res["rows"]:
            return res["rows"][0][0]
        return default

    # -- public API ----------------------------------------------------- #

    def test_connection(self) -> dict:
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                version, now = self._version_and_time(cur)
            finally:
                conn.close()
            self.server_version = version
            return {
                "success":        True,
                "message":        f"Connected to {self.cfg.describe()}",
                "db_type":        self.cfg.db_type,
                "db_type_label":  self.label,
                "server_version": str(version)[:300],
                "server_time":    str(now),
                "driver":         self.driver_name,
            }
        except Exception as exc:                     # noqa: BLE001
            return {"success": False, "message": _short_error(exc),
                    "db_type": self.cfg.db_type, "db_type_label": self.label}

    def begin_window(self) -> None:
        """Mark 'now' (server clock) as the start of the analysis window.

        Subsequent scans only report query activity that happened after this
        point, which is what you want while a UI test drives the application.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            version, now = self._version_and_time(cur)
            self.server_version = version
            self.window_start = now
            self._on_begin_window(cur)
        finally:
            conn.close()

    # -- page ↔ query correlation -------------------------------------- #

    def query_snapshot(self) -> dict:
        """Return {statement_key: {text, executions, elapsed_ms, cpu_ms, logical_reads}}.

        Keeps one connection open between calls (snapshots are taken at every
        page boundary, so they must be quick) and reconnects once on failure.
        Raises RuntimeError when the engine / privileges do not allow it.
        """
        for attempt in (1, 2):
            try:
                if getattr(self, "_snap_conn", None) is None:
                    self._snap_conn = self._connect()
                    cur = self._snap_conn.cursor()
                    self._version_and_time(cur)
                    self._prepare_snapshots(cur)
                cur = self._snap_conn.cursor()
                res = self._query_counters(cur)
                if not res["success"]:
                    if res.get("unsupported") or attempt == 2:
                        raise RuntimeError(res.get("error") or "query statistics unavailable")
                    self.close_snapshots()          # maybe a dropped connection — retry once
                    continue
                snap: dict = {}
                for row in res["rows"]:
                    k, execs, elapsed, cpu, reads, text = row[:6]
                    if is_own_query(text):
                        continue
                    snap[str(k)] = {
                        "text": str(text or "")[:4000],
                        "executions": int(to_float(execs)),
                        "elapsed_ms": to_float(elapsed),
                        "cpu_ms": None if cpu is None else to_float(cpu),
                        "logical_reads": None if reads is None else to_float(reads),
                    }
                return snap
            except RuntimeError:
                raise
            except Exception:                        # noqa: BLE001  (connection dropped)
                self.close_snapshots()
                if attempt == 2:
                    raise
        return {}

    def close_snapshots(self) -> None:
        conn = getattr(self, "_snap_conn", None)
        self._snap_conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass

    def run_full_scan(self) -> dict:
        conn = self._connect()
        try:
            cur = conn.cursor()
            version, now = self._version_and_time(cur)
            self.server_version = version
            sections = self._collect(cur)
        finally:
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass

        results: dict[str, Any] = {
            "scan_time":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "server_time":    str(now),
            "db_type":        self.cfg.db_type,
            "db_type_label":  self.label,
            "server":         self.cfg.server,
            "port":           self.cfg.effective_port,
            "database":       self.cfg.database,
            "schema":         self.cfg.schema,
            "driver":         self.driver_name,
            "server_version": str(version),
            "scope":          "window" if self.window_start is not None else "cumulative",
            "window_start":   str(self.window_start) if self.window_start is not None else None,
            "findings":       [],
            "notes":          {},
            "extra_sections": [],
        }
        results.update(sections)
        _add_privilege_finding(results)
        return results


def _short_error(exc: Exception) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    return msg if len(msg) <= 600 else msg[:600] + "…"


_PRIV_HINTS = ("permission", "denied", "ora-00942", "ora-01031", "insufficient privilege",
               "view server state", "must be superuser", "command denied")


def _add_privilege_finding(results: dict) -> None:
    """Add one consolidated hint when several sections failed for lack of rights."""
    failed = 0
    for key in ("slow_queries", "missing_indexes", "index_fragmentation",
                "unused_indexes", "iops", "cpu_utilization", "throughput"):
        res = results.get(key) or {}
        err = str(res.get("error") or "").lower()
        if not res.get("success") and not res.get("unsupported") and \
                any(h in err for h in _PRIV_HINTS):
            failed += 1
    if failed >= 2:
        hint = {
            "sqlserver":  "Grant VIEW SERVER STATE (and VIEW DATABASE STATE) to the scan login.",
            "postgresql": "Grant the pg_monitor role to the scan user.",
            "oracle":     "Grant SELECT_CATALOG_ROLE (or SELECT ANY DICTIONARY) to the scan user.",
            "mysql":      "Grant SELECT on performance_schema and PROCESS to the scan user.",
        }.get(results.get("db_type", ""), "Grant monitoring privileges to the scan user.")
        results["findings"].append({
            "severity": "warning",
            "message":  f"{failed} checks failed due to insufficient privileges. {hint}",
        })
