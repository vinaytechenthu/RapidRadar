"""
db_scan.oracle
--------------
Oracle Database scanner (V$ dynamic performance views + ALL_* dictionary).

Driver : python-oracledb in *thin* mode (no Oracle Client install needed;
         Oracle 12.1+).  "Database" = service name, "Schema" = owner to analyse
         (defaults to the login user).
Rights : SELECT_CATALOG_ROLE (or SELECT ANY DICTIONARY).  DBA_INDEX_USAGE
         (12.2+) is used for unused-index detection when accessible.
"""

from __future__ import annotations

from .base import (MARKER, BaseDBScanner, ConnectionConfig, col, kv_result, ok_result,
                   to_float, unsupported_result)


def _q(ident: str) -> str:
    """Quote an Oracle identifier."""
    return '"' + str(ident).replace('"', '""') + '"'


class OracleScanner(BaseDBScanner):

    db_type = "oracle"

    def __init__(self, cfg: ConnectionConfig) -> None:
        super().__init__(cfg)
        self._conn_version = ""
        self.owner = (cfg.schema or cfg.username).upper()

    # ------------------------------------------------------------------ #
    #  Connection                                                          #
    # ------------------------------------------------------------------ #

    def _connect(self):
        import oracledb
        dsn = f"{self.cfg.server}:{self.cfg.effective_port}/{self.cfg.database}"
        conn = oracledb.connect(user=self.cfg.username, password=self.cfg.password,
                                dsn=dsn, tcp_connect_timeout=30)
        self.driver_name = f"python-oracledb {oracledb.__version__} (thin)"
        self._conn_version = conn.version
        return conn

    def _version_and_time(self, cursor):
        banner = self._scalar(cursor, "SELECT banner FROM v$version WHERE ROWNUM = 1")
        cursor.execute("SELECT SYSDATE FROM dual")
        now = cursor.fetchone()[0]
        return banner or f"Oracle Database {self._conn_version}", now

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _window(self, column: str) -> tuple[str, dict]:
        # Exclude RapidRadar's own diagnostic statements (tagged by _safe_query).
        alias = column.split(".")[0] + "." if "." in column else ""
        own = f" AND {alias}sql_text NOT LIKE '%{MARKER}%'"
        if self.window_start is None:
            return own, {}
        return own + f" AND {column} >= :ws - INTERVAL '1' SECOND", {"ws": self.window_start}

    # ------------------------------------------------------------------ #
    #  Collection                                                          #
    # ------------------------------------------------------------------ #

    def _collect(self, cur) -> dict:
        findings: list[dict] = []
        notes: dict[str, str] = {}
        if self.window_start is None:
            notes["slow_queries"] = ("Cumulative statistics for statements still in the shared pool "
                                     f"parsed by schema {self.owner}.")
        else:
            notes["slow_queries"] = (f"Statements parsed by schema {self.owner} that were active during "
                                     "the scan window (statistics are per cursor lifetime).")
        out = {
            "db_info":             self._db_info(cur, findings),
            "slow_queries":        self._slow_queries(cur),
            "object_performance":  self._object_performance(cur),
            "throughput":          self._throughput(cur),
            "missing_indexes":     self._missing_indexes(cur, notes),
            "index_fragmentation": self._heavy("index_fragmentation", notes,
                                               self._fragmentation, cur, notes),
            "unused_indexes":      self._unused_indexes(cur, notes),
            "iops":                self._iops(cur),
            "cpu_utilization":     self._cpu(cur),
            "connections":         self._connections(cur),
            "extra_sections": [{
                "id": "sec-waits", "title": "Top Wait Events (non-idle)", "icon": "bi-hourglass-split",
                "result": self._wait_events(cur),
                "note": "Cumulative since instance startup.",
            }, {
                "id": "sec-stats", "title": "Stale / Missing Optimizer Statistics",
                "icon": "bi-bar-chart-steps", "result": self._stale_stats(cur, findings),
                "note": "Stale statistics are a top cause of bad execution plans. "
                        "Refresh with DBMS_STATS.GATHER_TABLE_STATS.",
            }],
        }
        out["findings"] = findings
        out["notes"] = notes
        return out

    # -- DB information --------------------------------------------------- #

    def _db_info(self, cur, findings: list) -> dict:
        pairs: list[tuple] = []
        db = self._safe_query(cur, "SELECT name, log_mode, open_mode, database_role FROM v$database")
        if db["success"] and db["rows"]:
            n, log_mode, open_mode, role = db["rows"][0]
            pairs += [("Database", n), ("Log mode", log_mode), ("Open mode", open_mode),
                      ("Role", role)]
        inst = self._safe_query(cur, "SELECT instance_name, host_name, version, "
                                     "TO_CHAR(startup_time, 'YYYY-MM-DD HH24:MI:SS') FROM v$instance")
        if inst["success"] and inst["rows"]:
            iname, host, ver, started = inst["rows"][0]
            pairs += [("Instance", iname), ("Host", host), ("Version", ver), ("Started", started)]
        pairs.append(("Service name", self.cfg.database))
        pairs.append(("Schema analysed", self.owner))

        size = self._scalar(cur, "SELECT ROUND(SUM(bytes)/1048576, 2) FROM dba_segments "
                                 "WHERE owner = :o", {"o": self.owner})
        if size is None and self.owner == self.cfg.username.upper():
            size = self._scalar(cur, "SELECT ROUND(SUM(bytes)/1048576, 2) FROM user_segments")
        pairs.append(("Schema size (MB)", size))

        params = self._safe_query(cur, """
            SELECT name, value FROM v$parameter
            WHERE name IN ('sga_target','pga_aggregate_target','memory_target','cpu_count',
                           'optimizer_mode','db_block_size','cursor_sharing')
            ORDER BY name""")
        if params["success"]:
            pairs += [(r[0], r[1]) for r in params["rows"]]
            cs = {r[0]: r[1] for r in params["rows"]}.get("cursor_sharing")
            if cs and cs.upper() == "EXACT":
                lit = self._scalar(cur, """
                    SELECT COUNT(*) FROM (SELECT force_matching_signature FROM v$sqlarea
                    WHERE parsing_schema_name = :o AND force_matching_signature > 0
                    GROUP BY force_matching_signature HAVING COUNT(*) > 20)""", {"o": self.owner})
                if lit and int(lit) > 0:
                    findings.append({"severity": "warning",
                                     "message": f"{lit} SQL pattern(s) run with many different literal "
                                                "values — use bind variables to avoid hard parsing."})
        return kv_result(pairs)

    # -- Slow queries ----------------------------------------------------- #

    def _query_counters(self, cur) -> dict:
        wc, wp = self._window("last_active_time")
        return self._safe_query(cur, f"""
            SELECT sql_id                 AS k,
                   executions,
                   elapsed_time / 1000    AS elapsed_ms,
                   cpu_time / 1000        AS cpu_ms,
                   buffer_gets            AS logical_reads,
                   sql_text               AS query_text
            FROM   v$sqlarea
            WHERE  parsing_schema_name = :o
              AND  executions > 0{wc}""", {"o": self.owner, **wp})

    def _slow_queries(self, cur) -> dict:
        wc, wp = self._window("last_active_time")
        return self._safe_query(cur, f"""
            SELECT * FROM (
              SELECT sql_id,
                     executions                                        AS execution_count,
                     ROUND(elapsed_time / executions / 1000, 2)        AS avg_elapsed_ms,
                     ROUND(cpu_time     / executions / 1000, 2)        AS avg_cpu_ms,
                     ROUND(buffer_gets  / executions)                  AS avg_logical_reads,
                     ROUND(disk_reads   / executions)                  AS avg_physical_reads,
                     ROUND(rows_processed / executions, 1)             AS avg_rows,
                     buffer_gets                                       AS total_logical_reads,
                     disk_reads                                        AS total_physical_reads,
                     sql_text                                          AS query_text,
                     TO_CHAR(last_active_time, 'YYYY-MM-DD HH24:MI:SS') AS last_execution_time
              FROM   v$sqlarea
              WHERE  parsing_schema_name = :o
                AND  executions > 0
                AND  command_type IN (2, 3, 6, 7, 47, 189){wc}
              ORDER  BY elapsed_time / executions DESC)
            WHERE ROWNUM <= 30""", {"o": self.owner, **wp})

    def _object_performance(self, cur) -> dict:
        wc, wp = self._window("s.last_active_time")
        return self._safe_query(cur, f"""
            SELECT * FROM (
              SELECT o.object_name, o.object_type,
                     SUM(s.executions)                                               AS execution_count,
                     ROUND(SUM(s.elapsed_time) / NULLIF(SUM(s.executions), 0) / 1000, 2) AS avg_elapsed_ms,
                     ROUND(SUM(s.cpu_time) / NULLIF(SUM(s.executions), 0) / 1000, 2)     AS avg_cpu_ms,
                     ROUND(SUM(s.buffer_gets) / NULLIF(SUM(s.executions), 0))            AS avg_logical_reads,
                     SUM(s.disk_reads)                                               AS total_physical_reads,
                     COUNT(DISTINCT s.sql_id)                                        AS statements
              FROM   v$sqlarea s
              JOIN   all_objects o ON o.object_id = s.program_id
              WHERE  s.program_id > 0 AND s.executions > 0 AND o.owner = :o{wc}
              GROUP  BY o.object_name, o.object_type
              ORDER  BY avg_elapsed_ms DESC)
            WHERE ROWNUM <= 20""", {"o": self.owner, **wp})

    # -- Throughput ------------------------------------------------------- #

    def _throughput(self, cur) -> dict:
        return self._safe_query(cur, """
            SELECT metric_name AS counter_name, ROUND(value, 2) AS current_value, metric_unit
            FROM   v$sysmetric
            WHERE  group_id = 2
              AND  metric_name IN ('User Transaction Per Sec','Executions Per Sec','User Calls Per Sec',
                     'Logical Reads Per Sec','Physical Reads Per Sec','Physical Writes Per Sec',
                     'Hard Parse Count Per Sec','Total Parse Count Per Sec','Redo Generated Per Sec',
                     'SQL Service Response Time','Database Time Per Sec','Average Active Sessions',
                     'Buffer Cache Hit Ratio','Soft Parse Ratio')
            ORDER  BY metric_name""")

    # -- Missing indexes (heuristic) ---------------------------------------- #

    def _missing_indexes(self, cur, notes: dict) -> dict:
        wc, wp = self._window("s.last_active_time")
        res = self._safe_query(cur, f"""
            SELECT * FROM (
              SELECT p.object_owner || '.' || p.object_name           AS table_name,
                     COUNT(DISTINCT p.sql_id)                         AS sql_statements,
                     SUM(s.executions)                                AS executions,
                     MAX(t.num_rows)                                  AS num_rows,
                     ROUND(SUM(s.elapsed_time) / 1000)                AS total_elapsed_ms,
                     MAX(p.sql_id)                                    AS sample_sql_id
              FROM   v$sql_plan p
              JOIN   v$sql s ON s.sql_id = p.sql_id AND s.child_number = p.child_number
              LEFT   JOIN all_tables t ON t.owner = p.object_owner AND t.table_name = p.object_name
              WHERE  p.operation = 'TABLE ACCESS' AND p.options LIKE 'FULL%'
                AND  p.object_owner = :o{wc}
              GROUP  BY p.object_owner, p.object_name
              HAVING NVL(MAX(t.num_rows), 0) > 10000
              ORDER  BY total_elapsed_ms DESC)
            WHERE ROWNUM <= 30""", {"o": self.owner, **wp})
        notes["missing_indexes"] = ("Oracle has no missing-index view. These large tables are read by "
                                    "FULL TABLE SCANs in cached execution plans — candidates for a new "
                                    "index or the SQL Tuning Advisor.")
        if not res["success"]:
            return res
        rows = []
        for r in res["rows"]:
            n = to_float(col(res, r, "num_rows"))
            pr = "HIGH" if n >= 1_000_000 else "MEDIUM" if n >= 100_000 else "LOW"
            sid = col(res, r, "sample_sql_id")
            rows.append([pr] + r + [
                f"SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('{sid}')); -- review predicates on "
                f"{col(res, r, 'table_name')} and index the filtered columns"])
        return ok_result(["priority"] + res["columns"] + ["suggestion"], rows)

    # -- Fragmentation (estimated) ---------------------------------------- #

    def _fragmentation(self, cur, notes: dict) -> dict:
        bs = to_float(self._scalar(cur, "SELECT value FROM v$parameter WHERE name = 'db_block_size'"),
                      8192.0) or 8192.0
        res = self._safe_query(cur, """
            WITH k AS (
              SELECT ic.index_owner, ic.index_name, SUM(NVL(tc.avg_col_len, 10)) AS key_len
              FROM   all_ind_columns ic
              JOIN   all_tab_columns tc ON tc.owner = ic.table_owner AND tc.table_name = ic.table_name
                                       AND tc.column_name = ic.column_name
              WHERE  ic.index_owner = :o
              GROUP  BY ic.index_owner, ic.index_name)
            SELECT * FROM (
              SELECT i.table_name, i.index_name, i.index_type, i.blevel,
                     ROUND(GREATEST(0, 100 * (1 - CEIL(i.num_rows * (k.key_len + 12) / (:bs * 0.9))
                                               / i.leaf_blocks)), 2)                  AS fragmentation_pct,
                     i.leaf_blocks                                                    AS page_count,
                     CEIL(i.num_rows * (k.key_len + 12) / (:bs * 0.9))                AS estimated_ideal_blocks,
                     TO_CHAR(i.last_analyzed, 'YYYY-MM-DD')                           AS last_analyzed
              FROM   all_indexes i
              JOIN   k ON k.index_owner = i.owner AND k.index_name = i.index_name
              WHERE  i.owner = :o AND i.index_type IN ('NORMAL', 'NORMAL/REV')
                AND  i.partitioned = 'NO' AND i.leaf_blocks > 100 AND i.num_rows > 0
              ORDER  BY 5 DESC)
            WHERE ROWNUM <= 60""", {"o": self.owner, "bs": bs})
        notes["index_fragmentation"] = ("Estimated from optimizer statistics (actual leaf blocks vs. "
                                        "ideal size). BLEVEL ≥ 4 is also flagged. Gather fresh statistics "
                                        "for accurate numbers.")
        if not res["success"]:
            return res
        cols = res["columns"] + ["recommended_action", "fix_script"]
        rows = []
        for r in res["rows"]:
            pct = to_float(col(res, r, "fragmentation_pct"))
            blevel = to_float(col(res, r, "blevel"))
            if pct <= 5 and blevel < 4:
                continue
            name = f"{_q(self.owner)}.{_q(col(res, r, 'index_name'))}"
            if pct >= 30 or blevel >= 4:
                action, fix = "REBUILD", f"ALTER INDEX {name} REBUILD ONLINE;"
            elif pct >= 10:
                action, fix = "COALESCE", f"ALTER INDEX {name} COALESCE;"
            else:
                action, fix = "OK", None
            rows.append(r + [action, fix])
        return ok_result(cols, rows[:40])

    # -- Unused indexes --------------------------------------------------- #

    def _unused_indexes(self, cur, notes: dict) -> dict:
        res = self._safe_query(cur, """
            SELECT * FROM (
              SELECT i.table_name, i.index_name, i.index_type, i.leaf_blocks,
                     NVL(u.total_access_count, 0)                         AS total_access_count,
                     TO_CHAR(u.last_used, 'YYYY-MM-DD HH24:MI:SS')        AS last_used,
                     'DROP INDEX "' || i.owner || '"."' || i.index_name || '";' AS drop_script
              FROM   all_indexes i
              LEFT   JOIN dba_index_usage u ON u.owner = i.owner AND u.name = i.index_name
              WHERE  i.owner = :o
                AND  i.uniqueness = 'NONUNIQUE'
                AND  i.index_type IN ('NORMAL', 'NORMAL/REV', 'BITMAP', 'FUNCTION-BASED NORMAL')
                AND  NVL(u.total_access_count, 0) = 0
                AND  NOT EXISTS (SELECT 1 FROM all_constraints c
                                 WHERE c.owner = i.owner AND c.index_name = i.index_name)
              ORDER  BY i.leaf_blocks DESC NULLS LAST)
            WHERE ROWNUM <= 50""", {"o": self.owner})
        if res["success"]:
            notes["unused_indexes"] = ("From DBA_INDEX_USAGE (Oracle 12.2+; sampled tracking). Indexes on "
                                       "foreign-key columns prevent table locks on parent DELETE/UPDATE — "
                                       "keep those even if unused by queries.")
            return res
        return unsupported_result(
            "DBA_INDEX_USAGE is not accessible (needs Oracle 12.2+ and SELECT_CATALOG_ROLE). "
            "Alternative: ALTER INDEX <name> MONITORING USAGE, then check USER_OBJECT_USAGE. "
            f"Error: {res['error']}")

    # -- IOPS ------------------------------------------------------------- #

    def _iops(self, cur) -> dict:
        return self._safe_query(cur, """
            SELECT df.name                                                AS physical_name,
                   ts.name                                                AS tablespace,
                   fs.phyrds                                              AS num_of_reads,
                   fs.phywrts                                             AS num_of_writes,
                   ROUND(fs.readtim  * 10 / NULLIF(fs.phyrds,  0), 2)     AS avg_read_latency_ms,
                   ROUND(fs.writetim * 10 / NULLIF(fs.phywrts, 0), 2)     AS avg_write_latency_ms,
                   ROUND(fs.phyblkrd  * df.block_size / 1048576, 2)       AS mb_read,
                   ROUND(fs.phyblkwrt * df.block_size / 1048576, 2)       AS mb_written
            FROM   v$filestat fs
            JOIN   v$datafile df ON df.file# = fs.file#
            JOIN   v$tablespace ts ON ts.ts# = df.ts#
            ORDER  BY fs.readtim + fs.writetim DESC""")

    # -- CPU -------------------------------------------------------------- #

    def _cpu(self, cur) -> dict:
        ncpu = to_float(self._scalar(cur, "SELECT value FROM v$osstat WHERE stat_name = 'NUM_CPUS'"), 1) or 1
        res = self._safe_query(cur, """
            SELECT TO_CHAR(end_time, 'YYYY-MM-DD HH24:MI:SS')                            AS sample_time,
                   MAX(CASE WHEN metric_name = 'CPU Usage Per Sec' THEN value END)        AS db_cs_per_sec,
                   MAX(CASE WHEN metric_name = 'Host CPU Utilization (%)' THEN value END) AS host_pct
            FROM   v$sysmetric_history
            WHERE  group_id = 2
              AND  metric_name IN ('CPU Usage Per Sec', 'Host CPU Utilization (%)')
            GROUP  BY end_time
            ORDER  BY end_time DESC""")
        if not res["success"]:
            return res
        rows = []
        for ts, db_cs, host in res["rows"][:20]:
            db_pct = min(100.0, to_float(db_cs) / ncpu)        # centisec/sec per CPU -> %
            host_pct = to_float(host, db_pct)
            rows.append([ts, round(db_pct), round(max(0.0, host_pct - db_pct)),
                         round(max(0.0, 100 - host_pct))])
        return ok_result(["sample_time", "sql_cpu_pct", "other_cpu_pct", "system_idle_pct"], rows)

    # -- Connections ------------------------------------------------------ #

    def _connections(self, cur) -> dict:
        summary = self._safe_query(cur, """
            SELECT SUM(CASE WHEN type = 'USER' THEN 1 ELSE 0 END)                         AS total_sessions,
                   SUM(CASE WHEN type = 'USER' AND status = 'ACTIVE' THEN 1 ELSE 0 END)    AS running_sessions,
                   SUM(CASE WHEN type = 'USER' AND status = 'INACTIVE' THEN 1 ELSE 0 END)  AS sleeping_sessions,
                   SUM(CASE WHEN type = 'BACKGROUND' THEN 1 ELSE 0 END)                    AS background_sessions,
                   (SELECT COUNT(*) FROM v$transaction)                                   AS sessions_with_open_txn,
                   (SELECT ROUND(SUM(pga_used_mem) / 1024) FROM v$process)                AS total_memory_kb
            FROM   v$session""")
        details = self._safe_query(cur, """
            SELECT * FROM (
              SELECT s.sid AS session_id, s.serial# AS serial_no, s.username AS login_name,
                     s.machine AS host_name, SUBSTR(s.program, 1, 60) AS program_name, s.status,
                     TO_CHAR(s.logon_time, 'YYYY-MM-DD HH24:MI:SS') AS login_time,
                     s.last_call_et AS seconds_in_state, s.sql_id, s.event AS wait_event,
                     s.wait_class, s.blocking_session
              FROM   v$session s
              WHERE  s.type = 'USER' AND s.sid <> SYS_CONTEXT('USERENV', 'SID')
              ORDER  BY CASE WHEN s.username = :o THEN 0 ELSE 1 END, s.status, s.last_call_et DESC)
            WHERE ROWNUM <= 40""", {"o": self.owner})
        return {"summary": summary, "details": details}

    # -- Extras ----------------------------------------------------------- #

    def _wait_events(self, cur) -> dict:
        return self._safe_query(cur, """
            SELECT * FROM (
              SELECT event, wait_class, total_waits,
                     ROUND(time_waited_micro / 1e6, 1)                         AS wait_time_s,
                     ROUND(time_waited_micro / NULLIF(total_waits, 0) / 1000, 2) AS avg_wait_ms
              FROM   v$system_event
              WHERE  wait_class <> 'Idle'
              ORDER  BY time_waited_micro DESC)
            WHERE ROWNUM <= 15""")

    def _stale_stats(self, cur, findings: list) -> dict:
        res = self._safe_query(cur, """
            SELECT * FROM (
              SELECT table_name, num_rows, TO_CHAR(last_analyzed, 'YYYY-MM-DD HH24:MI:SS') AS last_analyzed,
                     NVL(stale_stats, 'NO STATS') AS stale_stats,
                     'EXEC DBMS_STATS.GATHER_TABLE_STATS(''' || owner || ''', ''' || table_name || ''');' AS fix_script
              FROM   all_tab_statistics
              WHERE  owner = :o AND object_type = 'TABLE'
                AND  (stale_stats = 'YES' OR last_analyzed IS NULL)
              ORDER  BY num_rows DESC NULLS LAST)
            WHERE ROWNUM <= 30""", {"o": self.owner})
        if res["success"] and res["rows"]:
            n = len(res["rows"])
            findings.append({"severity": "warning",
                             "message": f"{n} table{'s' if n > 1 else ''} with stale or missing optimizer statistics."})
        return res
