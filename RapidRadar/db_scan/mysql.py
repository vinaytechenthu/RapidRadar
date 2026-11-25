"""
db_scan.mysql
-------------
MySQL 5.7+/8.x and MariaDB 10.x scanner (performance_schema + information_schema).

Driver : PyMySQL (pure Python, no client libraries needed)
Rights : SELECT on performance_schema, PROCESS
Needs  : performance_schema = ON (default in MySQL; OFF by default in MariaDB)
"""

from __future__ import annotations

from .base import (BaseDBScanner, ConnectionConfig, col, kv_result, ok_result,
                   to_float, unsupported_result)

_PS = 1_000_000_000.0          # performance_schema timers are picoseconds -> ms


def _bt(ident: str) -> str:
    return "`" + str(ident).replace("`", "``") + "`"


class MySQLScanner(BaseDBScanner):

    db_type = "mysql"

    def __init__(self, cfg: ConnectionConfig) -> None:
        super().__init__(cfg)
        self.is_mariadb = False
        self._ps_on = True

    # ------------------------------------------------------------------ #
    #  Connection                                                          #
    # ------------------------------------------------------------------ #

    def _connect(self):
        import pymysql
        conn = pymysql.connect(host=self.cfg.server, port=int(self.cfg.effective_port),
                               user=self.cfg.username, password=self.cfg.password,
                               database=self.cfg.database, connect_timeout=30,
                               read_timeout=300, autocommit=True, charset="utf8mb4",
                               program_name="RapidRadar DBScan")
        self.driver_name = f"PyMySQL {pymysql.__version__}"
        return conn

    def _version_and_time(self, cursor):
        cursor.execute("SELECT VERSION(), NOW()")
        ver, now = cursor.fetchone()
        self.is_mariadb = "mariadb" in str(ver).lower()
        return (f"MariaDB {ver}" if self.is_mariadb else f"MySQL {ver}"), now

    @property
    def label(self) -> str:
        return "MariaDB" if self.is_mariadb else "MySQL"

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _status(self, cur) -> dict:
        res = self._safe_query(cur, "SHOW GLOBAL STATUS")
        return {r[0]: r[1] for r in res["rows"]} if res["success"] else {}

    def _vars(self, cur, names: list[str]) -> dict:
        res = self._safe_query(cur, "SHOW GLOBAL VARIABLES")
        if not res["success"]:
            return {}
        want = set(names)
        return {r[0]: r[1] for r in res["rows"] if r[0] in want}

    def _window(self, column: str) -> tuple[str, list]:
        if self.window_start is None:
            return "", []
        return f" AND {column} >= %s", [self.window_start]

    def _ps_unavailable(self) -> dict:
        return unsupported_result("performance_schema is OFF. Enable it (performance_schema=ON in "
                                  "my.cnf / my.ini, restart required) to collect this metric.")

    # ------------------------------------------------------------------ #
    #  Collection                                                          #
    # ------------------------------------------------------------------ #

    def _collect(self, cur) -> dict:
        findings: list[dict] = []
        notes: dict[str, str] = {}
        variables = self._vars(cur, ["performance_schema", "innodb_buffer_pool_size", "max_connections",
                                     "slow_query_log", "long_query_time", "innodb_file_per_table",
                                     "character_set_server", "collation_server", "version_comment"])
        status = self._status(cur)
        self._ps_on = str(variables.get("performance_schema", "ON")).upper() in ("ON", "1")
        if not self._ps_on:
            findings.append({"severity": "issue",
                             "message": "performance_schema is OFF — slow query, index usage and I/O "
                                        "statistics cannot be collected."})
        notes["slow_queries"] = ("Statement digests from performance_schema"
                                 + (" seen during the scan window (LAST_SEEN)." if self.window_start
                                    else ", cumulative since server start / last truncate."))

        out = {
            "db_info":             self._db_info(cur, variables, status, findings),
            "slow_queries":        self._slow_queries(cur),
            "object_performance":  self._programs(cur),
            "throughput":          self._throughput(status),
            "missing_indexes":     self._missing_indexes(cur, notes),
            "index_fragmentation": self._heavy("index_fragmentation", notes,
                                               self._fragmentation, cur, notes),
            "unused_indexes":      self._unused_indexes(cur, status, notes),
            "iops":                self._iops(cur),
            "cpu_utilization":     unsupported_result(
                "MySQL / MariaDB do not expose CPU utilisation through SQL. "
                "Use OS monitoring (e.g. windows_exporter / node_exporter) for CPU."),
            "connections":         self._connections(cur, status, variables),
            "extra_sections": [{
                "id": "sec-nopk", "title": "Tables Without a Primary Key", "icon": "bi-key",
                "result": self._no_pk(cur, findings),
                "note": "InnoDB tables without a primary key use a hidden clustered index, which hurts "
                        "lookups and replication. Add an explicit primary key.",
            }],
        }
        out["findings"] = findings
        out["notes"] = notes
        return out

    # -- DB information --------------------------------------------------- #

    def _db_info(self, cur, variables: dict, status: dict, findings: list) -> dict:
        size = self._safe_query(cur, """
            SELECT ROUND(SUM(data_length + index_length) / 1048576, 2), COUNT(*)
            FROM information_schema.tables WHERE table_schema = %s""", [self.cfg.database])
        size_mb, ntables = (size["rows"][0] if size["success"] and size["rows"] else (None, None))
        reads = to_float(status.get("Innodb_buffer_pool_reads"))
        reqs = to_float(status.get("Innodb_buffer_pool_read_requests"))
        hit = round(100 * (1 - reads / reqs), 2) if reqs else None
        if hit is not None and hit < 95:
            findings.append({"severity": "warning",
                             "message": f"InnoDB buffer pool hit ratio is {hit:.1f} % — consider a "
                                        "larger innodb_buffer_pool_size."})
        maxc = to_float(variables.get("max_connections"))
        used = to_float(status.get("Max_used_connections"))
        if maxc and used / maxc >= 0.85:
            findings.append({"severity": "warning",
                             "message": f"Peak connections reached {used:.0f} of max_connections "
                                        f"{maxc:.0f} — risk of 'Too many connections' errors."})
        uptime = int(to_float(status.get("Uptime")))
        bp = variables.get("innodb_buffer_pool_size")
        return kv_result([
            ("Schema",                     self.cfg.database),
            ("Size (MB)",                  size_mb),
            ("Tables",                     ntables),
            ("Server uptime",              f"{uptime // 86400}d {uptime % 86400 // 3600}h "
                                           f"{uptime % 3600 // 60}m" if uptime else None),
            ("Distribution",               variables.get("version_comment")),
            ("innodb_buffer_pool_size",    f"{int(to_float(bp)) // 1048576} MB" if bp else None),
            ("Buffer pool hit ratio %",    hit),
            ("max_connections",            variables.get("max_connections")),
            ("Max used connections",       status.get("Max_used_connections")),
            ("performance_schema",         variables.get("performance_schema")),
            ("slow_query_log",             variables.get("slow_query_log")),
            ("long_query_time (s)",        variables.get("long_query_time")),
            ("innodb_file_per_table",      variables.get("innodb_file_per_table")),
            ("Character set / collation",  f"{variables.get('character_set_server')} / "
                                           f"{variables.get('collation_server')}"),
        ])

    # -- Slow queries ----------------------------------------------------- #

    def _query_counters(self, cur) -> dict:
        wc, wp = self._window("LAST_SEEN")
        res = self._safe_query(cur, f"""
            SELECT CONCAT(IFNULL(SCHEMA_NAME, ''), ':', IFNULL(DIGEST, 'other')) AS k,
                   COUNT_STAR                     AS executions,
                   SUM_TIMER_WAIT / {_PS}         AS elapsed_ms,
                   NULL                           AS cpu_ms,
                   SUM_ROWS_EXAMINED              AS logical_reads,
                   DIGEST_TEXT                    AS query_text
            FROM   performance_schema.events_statements_summary_by_digest
            WHERE  SCHEMA_NAME = %s AND COUNT_STAR > 0{wc}""", [self.cfg.database] + wp)
        return self._drop_own(res, limit=100000)

    def _slow_queries(self, cur) -> dict:
        if not self._ps_on:
            return self._ps_unavailable()
        wc, wp = self._window("LAST_SEEN")
        return self._drop_own(self._safe_query(cur, f"""
            SELECT COUNT_STAR                                          AS execution_count,
                   ROUND(AVG_TIMER_WAIT / {_PS}, 2)                    AS avg_elapsed_ms,
                   ROUND(SUM_LOCK_TIME / NULLIF(COUNT_STAR,0) / {_PS}, 2) AS avg_lock_ms,
                   ROUND(SUM_ROWS_EXAMINED / NULLIF(COUNT_STAR,0))     AS avg_rows_examined,
                   ROUND(SUM_ROWS_SENT / NULLIF(COUNT_STAR,0))         AS avg_rows_sent,
                   SUM_NO_INDEX_USED                                   AS no_index_used,
                   SUM_CREATED_TMP_DISK_TABLES                         AS tmp_disk_tables,
                   SUM_SORT_MERGE_PASSES                               AS sort_merge_passes,
                   DIGEST_TEXT                                         AS query_text,
                   DATE_FORMAT(LAST_SEEN, '%%Y-%%m-%%d %%H:%%i:%%s')   AS last_execution_time
            FROM   performance_schema.events_statements_summary_by_digest
            WHERE  SCHEMA_NAME = %s AND COUNT_STAR > 0{wc}
            ORDER  BY AVG_TIMER_WAIT DESC
            LIMIT  80""", [self.cfg.database] + wp), limit=30)

    @staticmethod
    def _drop_own(res: dict, limit: int) -> dict:
        """Remove RapidRadar's own diagnostic statements.

        DIGEST_TEXT strips comments, so the MARKER tag is not visible here;
        instead drop digests that only read the server's catalog schemas.
        """
        if not res["success"] or "query_text" not in res["columns"]:
            return res
        i = res["columns"].index("query_text")
        own = ("performance_schema", "information_schema", "SHOW GLOBAL", "SELECT VERSION")
        res["rows"] = [r for r in res["rows"]
                       if not any(o.lower() in str(r[i] or "").lower() for o in own)][:limit]
        return res

    def _programs(self, cur) -> dict:
        if not self._ps_on:
            return self._ps_unavailable()
        res = self._safe_query(cur, f"""
            SELECT OBJECT_NAME                                   AS object_name,
                   OBJECT_TYPE                                   AS object_type,
                   COUNT_STAR                                    AS execution_count,
                   ROUND(AVG_TIMER_WAIT / {_PS}, 2)              AS avg_elapsed_ms,
                   SUM_STATEMENTS_COUNT                          AS statements_executed,
                   ROUND(SUM_STATEMENTS_WAIT / {_PS}, 2)         AS total_statement_ms,
                   SUM_ROWS_EXAMINED                             AS rows_examined
            FROM   performance_schema.events_statements_summary_by_program
            WHERE  OBJECT_SCHEMA = %s AND COUNT_STAR > 0
            ORDER  BY AVG_TIMER_WAIT DESC
            LIMIT  20""", [self.cfg.database])
        if not res["success"] and self.is_mariadb:
            return unsupported_result("MariaDB does not provide "
                                      "events_statements_summary_by_program.")
        return res

    # -- Throughput ------------------------------------------------------- #

    def _throughput(self, status: dict) -> dict:
        if not status:
            return unsupported_result("SHOW GLOBAL STATUS is not accessible.")
        uptime = max(to_float(status.get("Uptime"), 1), 1)
        names = ["Questions", "Com_select", "Com_insert", "Com_update", "Com_delete", "Com_commit",
                 "Com_rollback", "Slow_queries", "Select_scan", "Select_full_join",
                 "Created_tmp_disk_tables", "Sort_merge_passes", "Innodb_rows_read",
                 "Innodb_rows_inserted", "Innodb_rows_updated", "Innodb_row_lock_waits",
                 "Threads_running", "Aborted_connects"]
        rows = [[n, status[n], round(to_float(status[n]) / uptime, 2)]
                for n in names if n in status]
        return ok_result(["counter_name", "cumulative_value", "avg_per_sec"], rows)

    # -- Missing indexes (heuristic) ---------------------------------------- #

    def _missing_indexes(self, cur, notes: dict) -> dict:
        if not self._ps_on:
            return self._ps_unavailable()
        wc, wp = self._window("LAST_SEEN")
        res = self._safe_query(cur, f"""
            SELECT DIGEST_TEXT                                   AS query_text,
                   COUNT_STAR                                    AS execution_count,
                   SUM_NO_INDEX_USED                             AS executions_without_index,
                   SUM_NO_GOOD_INDEX_USED                        AS executions_without_good_index,
                   ROUND(SUM_ROWS_EXAMINED / NULLIF(COUNT_STAR,0)) AS avg_rows_examined,
                   ROUND(SUM_ROWS_SENT / NULLIF(COUNT_STAR,0))     AS avg_rows_sent,
                   ROUND(AVG_TIMER_WAIT / {_PS}, 2)              AS avg_elapsed_ms
            FROM   performance_schema.events_statements_summary_by_digest
            WHERE  SCHEMA_NAME = %s
              AND  (SUM_NO_INDEX_USED > 0 OR SUM_NO_GOOD_INDEX_USED > 0)
              AND  SUM_ROWS_EXAMINED / NULLIF(COUNT_STAR,0) > 1000{wc}
            ORDER  BY SUM_ROWS_EXAMINED DESC
            LIMIT  80""", [self.cfg.database] + wp)
        res = self._drop_own(res, limit=30)
        notes["missing_indexes"] = ("MySQL has no missing-index advisor. These statements ran without "
                                    "a usable index and examined many rows per execution — EXPLAIN them "
                                    "and index the WHERE / JOIN columns.")
        if not res["success"]:
            return res
        rows = []
        for r in res["rows"]:
            ex = to_float(col(res, r, "avg_rows_examined"))
            pr = "HIGH" if ex >= 100_000 else "MEDIUM" if ex >= 10_000 else "LOW"
            rows.append([pr] + r)
        return ok_result(["priority"] + res["columns"], rows)

    # -- Fragmentation ---------------------------------------------------- #

    def _fragmentation(self, cur, notes: dict) -> dict:
        res = self._safe_query(cur, """
            SELECT table_name, engine,
                   ROUND(100 * data_free / NULLIF(data_length + index_length + data_free, 0), 2) AS fragmentation_pct,
                   ROUND(data_length  / 1048576, 2) AS data_mb,
                   ROUND(index_length / 1048576, 2) AS index_mb,
                   ROUND(data_free    / 1048576, 2) AS free_mb,
                   table_rows
            FROM   information_schema.tables
            WHERE  table_schema = %s AND table_type = 'BASE TABLE'
              AND  data_length + index_length > 10485760
              AND  data_free > 0
            ORDER  BY fragmentation_pct DESC
            LIMIT  60""", [self.cfg.database])
        notes["index_fragmentation"] = ("Table + index fragmentation = reclaimable free space (DATA_FREE). "
                                        "With innodb_file_per_table = OFF, DATA_FREE reflects the shared "
                                        "tablespace and is not table-specific.")
        if not res["success"]:
            return res
        cols = res["columns"] + ["recommended_action", "fix_script"]
        rows = []
        for r in res["rows"]:
            pct = to_float(col(res, r, "fragmentation_pct"))
            if pct <= 5:
                continue
            action = "OPTIMIZE" if pct >= 30 else "MONITOR" if pct >= 10 else "OK"
            fix = (f"OPTIMIZE TABLE {_bt(self.cfg.database)}.{_bt(col(res, r, 'table_name'))};"
                   if pct >= 10 else None)
            rows.append(r + [action, fix])
        return ok_result(cols, rows[:40])

    # -- Unused indexes --------------------------------------------------- #

    def _unused_indexes(self, cur, status: dict, notes: dict) -> dict:
        if not self._ps_on:
            return self._ps_unavailable()
        res = self._safe_query(cur, """
            SELECT t.OBJECT_NAME                            AS table_name,
                   t.INDEX_NAME                             AS index_name,
                   t.COUNT_READ                             AS index_reads,
                   t.COUNT_WRITE                            AS index_writes
            FROM   performance_schema.table_io_waits_summary_by_index_usage t
            WHERE  t.OBJECT_SCHEMA = %s
              AND  t.INDEX_NAME IS NOT NULL
              AND  t.INDEX_NAME <> 'PRIMARY'
              AND  t.COUNT_READ = 0
              AND  EXISTS (SELECT 1 FROM information_schema.statistics s
                           WHERE s.table_schema = t.OBJECT_SCHEMA AND s.table_name = t.OBJECT_NAME
                             AND s.index_name = t.INDEX_NAME AND s.non_unique = 1)
            ORDER  BY t.COUNT_WRITE DESC
            LIMIT  50""", [self.cfg.database])
        if res["success"]:
            res["columns"].append("drop_script")
            for r in res["rows"]:
                r.append(f"ALTER TABLE {_bt(self.cfg.database)}.{_bt(r[0])} DROP INDEX {_bt(r[1])};")
            up = int(to_float(status.get("Uptime")))
            notes["unused_indexes"] = (f"No reads recorded since server start ({up // 86400} days ago). "
                                       "Indexes backing foreign keys cannot be dropped while the FK exists.")
        return res

    # -- IOPS ------------------------------------------------------------- #

    def _iops(self, cur) -> dict:
        if not self._ps_on:
            return self._ps_unavailable()
        return self._safe_query(cur, f"""
            SELECT FILE_NAME                                             AS physical_name,
                   EVENT_NAME                                            AS file_type,
                   COUNT_READ                                            AS num_of_reads,
                   COUNT_WRITE                                           AS num_of_writes,
                   ROUND(AVG_TIMER_READ  / {_PS}, 3)                     AS avg_read_latency_ms,
                   ROUND(AVG_TIMER_WRITE / {_PS}, 3)                     AS avg_write_latency_ms,
                   ROUND(SUM_NUMBER_OF_BYTES_READ  / 1048576, 2)         AS mb_read,
                   ROUND(SUM_NUMBER_OF_BYTES_WRITE / 1048576, 2)         AS mb_written
            FROM   performance_schema.file_summary_by_instance
            WHERE  COUNT_READ + COUNT_WRITE > 0
            ORDER  BY SUM_TIMER_WAIT DESC
            LIMIT  25""")

    # -- Connections ------------------------------------------------------ #

    def _connections(self, cur, status: dict, variables: dict) -> dict:
        summary = self._safe_query(cur, """
            SELECT COUNT(*)                                                         AS total_sessions,
                   SUM(CASE WHEN command NOT IN ('Sleep','Daemon','Binlog Dump') THEN 1 ELSE 0 END) AS running_sessions,
                   SUM(CASE WHEN command = 'Sleep' THEN 1 ELSE 0 END)               AS sleeping_sessions,
                   SUM(CASE WHEN command IN ('Daemon','Binlog Dump')
                             OR user IN ('system user','event_scheduler') THEN 1 ELSE 0 END) AS background_sessions,
                   (SELECT COUNT(*) FROM information_schema.innodb_trx)             AS sessions_with_open_txn,
                   NULL                                                             AS total_memory_kb
            FROM   information_schema.processlist""")
        details = self._safe_query(cur, """
            SELECT id AS session_id, user AS login_name, host AS host_name, db AS database_name,
                   command, time AS seconds_in_state, state, LEFT(info, 200) AS current_query
            FROM   information_schema.processlist
            WHERE  (db = %s OR db IS NULL) AND id <> CONNECTION_ID()
            ORDER  BY (command <> 'Sleep') DESC, time DESC
            LIMIT  40""", [self.cfg.database])
        return {"summary": summary, "details": details}

    # -- Extras ----------------------------------------------------------- #

    def _no_pk(self, cur, findings: list) -> dict:
        res = self._safe_query(cur, """
            SELECT t.table_name, t.engine, t.table_rows,
                   ROUND((t.data_length + t.index_length) / 1048576, 2) AS size_mb
            FROM   information_schema.tables t
            LEFT   JOIN information_schema.table_constraints c
                   ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                  AND c.constraint_type = 'PRIMARY KEY'
            WHERE  t.table_schema = %s AND t.table_type = 'BASE TABLE' AND c.constraint_name IS NULL
            ORDER  BY t.table_rows DESC
            LIMIT  30""", [self.cfg.database])
        if res["success"] and res["rows"]:
            n = len(res["rows"])
            findings.append({"severity": "warning",
                             "message": f"{n} table{'s' if n > 1 else ''} without a primary key."})
        return res
