"""
db_scan.postgres
----------------
PostgreSQL scanner (pg_stat_* views + optional extensions).

Driver     : psycopg2 (psycopg2-binary)  — falls back to psycopg 3 if present
Rights     : the pg_monitor role is recommended
Extensions : pg_stat_statements  -> slow queries   (strongly recommended)
             pgstattuple         -> exact index fragmentation (optional;
                                    otherwise a statistics-based estimate is used)
"""

from __future__ import annotations


from .base import (BaseDBScanner, ConnectionConfig, col, is_own_query, kv_result,
                   ok_result, to_float, unsupported_result)


class PostgresScanner(BaseDBScanner):

    db_type = "postgresql"

    def __init__(self, cfg: ConnectionConfig) -> None:
        super().__init__(cfg)
        self._pgss_schema: str | None = None
        self._version_num: int = 0
        # Baselines captured by begin_window() — pg_stat_* have no timestamps,
        # so window mode works by diffing cumulative counters.
        self._base_statements: dict[tuple, tuple] | None = None
        self._base_tables: dict[int, tuple] | None = None

    # ------------------------------------------------------------------ #
    #  Connection                                                          #
    # ------------------------------------------------------------------ #

    def _connect(self):
        kwargs = dict(host=self.cfg.server, port=self.cfg.effective_port,
                      dbname=self.cfg.database, user=self.cfg.username,
                      password=self.cfg.password, connect_timeout=30,
                      application_name="RapidRadar DBScan")
        try:
            import psycopg2
            conn = psycopg2.connect(**kwargs)
            self.driver_name = f"psycopg2 {psycopg2.__version__.split()[0]}"
        except ImportError:
            import psycopg                                   # psycopg 3
            conn = psycopg.connect(**kwargs)
            self.driver_name = f"psycopg {psycopg.__version__}"
        conn.autocommit = True        # a failed query must not abort the rest
        return conn

    def _version_and_time(self, cursor):
        cursor.execute("SELECT version(), now(), current_setting('server_version_num')::int")
        row = cursor.fetchone()
        self._version_num = int(row[2])
        return str(row[0]).split(",")[0], row[1]

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _schema_filter(self, column: str) -> tuple[str, list]:
        if self.cfg.schema:
            return f" AND {column} = %s", [self.cfg.schema]
        return f" AND {column} NOT IN ('pg_catalog','information_schema') AND {column} !~ '^pg_toast'", []

    def _detect_pgss(self, cur) -> str | None:
        ns = self._scalar(cur, """
            SELECT n.nspname FROM pg_extension e
            JOIN pg_namespace n ON n.oid = e.extnamespace
            WHERE e.extname = 'pg_stat_statements'""")
        self._pgss_schema = ns
        return ns

    def _pgss_table(self) -> str:
        return '"' + str(self._pgss_schema).replace('"', '""') + '".pg_stat_statements'

    def _time_cols(self) -> tuple[str, str]:
        # PG13 renamed total_time/mean_time -> total_exec_time/mean_exec_time
        return ("total_exec_time", "mean_exec_time") if self._version_num >= 130000 \
            else ("total_time", "mean_time")

    def _has_ext(self, cur, name: str) -> bool:
        return bool(self._scalar(cur, "SELECT 1 FROM pg_extension WHERE extname = %s", [name]))

    # ------------------------------------------------------------------ #
    #  Window baseline                                                     #
    # ------------------------------------------------------------------ #

    def _on_begin_window(self, cur) -> None:
        if self._detect_pgss(cur):
            self._base_statements = {tuple(r[:3]): tuple(r[3:])
                                     for r in self._fetch_statements(cur)["rows"]}
        res = self._safe_query(cur, "SELECT relid, seq_scan, seq_tup_read, COALESCE(idx_scan,0) "
                                    "FROM pg_stat_user_tables")
        if res["success"]:
            self._base_tables = {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in res["rows"]}

    def _fetch_statements(self, cur) -> dict:
        total_col, _ = self._time_cols()
        return self._safe_query(cur, f"""
            SELECT userid, dbid, queryid,
                   calls, {total_col}, rows, shared_blks_hit, shared_blks_read, query
            FROM   {self._pgss_table()}
            WHERE  dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND  queryid IS NOT NULL""")

    def _prepare_snapshots(self, cur) -> None:
        self._detect_pgss(cur)

    def _query_counters(self, cur) -> dict:
        if not self._pgss_schema:
            return unsupported_result("pg_stat_statements is not installed, so per-page query "
                                      "timings are not available.")
        res = self._fetch_statements(cur)
        if not res["success"]:
            return res
        rows = [[f"{r[0]}:{r[1]}:{r[2]}", r[3], r[4], None, (r[6] or 0) + (r[7] or 0), r[8]]
                for r in res["rows"]]
        return ok_result(["k", "executions", "elapsed_ms", "cpu_ms", "logical_reads", "query_text"],
                         rows)

    # ------------------------------------------------------------------ #
    #  Collection                                                          #
    # ------------------------------------------------------------------ #

    def _collect(self, cur) -> dict:
        findings: list[dict] = []
        notes: dict[str, str] = {}
        self._detect_pgss(cur)

        out = {
            "db_info":             self._db_info(cur, findings),
            "slow_queries":        self._slow_queries(cur, findings, notes),
            "object_performance":  self._functions(cur, notes),
            "throughput":          self._throughput(cur),
            "missing_indexes":     self._missing_indexes(cur, notes),
            "index_fragmentation": self._heavy("index_fragmentation", notes,
                                               self._fragmentation, cur, notes),
            "unused_indexes":      self._unused_indexes(cur, notes),
            "iops":                self._iops(cur, notes),
            "cpu_utilization":     unsupported_result(
                "PostgreSQL does not expose host/backend CPU utilisation through SQL. "
                "Use OS monitoring (e.g. windows_exporter / node_exporter) for CPU."),
            "connections":         self._connections(cur),
        }
        dead = self._dead_tuples(cur, findings)
        out["extra_sections"] = [{
            "id": "sec-vacuum", "title": "Table Bloat — Dead Tuples / VACUUM Health",
            "icon": "bi-recycle", "result": dead,
            "note": "Tables with many dead tuples waste I/O and slow sequential and index scans. "
                    "Tune autovacuum or run VACUUM (ANALYZE) on the flagged tables.",
        }, {
            "id": "sec-locks", "title": "Blocked Sessions (lock waits)",
            "icon": "bi-lock", "result": self._blocking(cur),
            "note": "Sessions currently waiting on a lock held by another session.",
        }]
        out["findings"] = findings
        out["notes"] = notes
        return out

    # -- DB information --------------------------------------------------- #

    def _db_info(self, cur, findings: list) -> dict:
        res = self._safe_query(cur, """
            SELECT current_database(),
                   ROUND(pg_database_size(current_database()) / 1048576.0, 2),
                   pg_encoding_to_char(d.encoding), d.datcollate,
                   date_trunc('second', now() - pg_postmaster_start_time())::text,
                   current_setting('max_connections'),
                   current_setting('shared_buffers'),
                   current_setting('work_mem'),
                   current_setting('autovacuum'),
                   current_setting('track_io_timing'),
                   ROUND(100.0 * s.blks_hit / NULLIF(s.blks_hit + s.blks_read, 0), 2),
                   s.stats_reset::text
            FROM pg_database d
            JOIN pg_stat_database s ON s.datid = d.oid
            WHERE d.datname = current_database()""")
        if not res["success"] or not res["rows"]:
            return res
        (name, size_mb, enc, coll, uptime, max_conn, shared_buf, work_mem,
         autovac, io_timing, hit_ratio, stats_reset) = res["rows"][0]
        extensions = self._safe_query(cur, "SELECT string_agg(extname || ' ' || extversion, ', ' "
                                           "ORDER BY extname) FROM pg_extension")
        ext_list = extensions["rows"][0][0] if extensions["success"] and extensions["rows"] else None

        if str(autovac).lower() != "on":
            findings.append({"severity": "issue",
                             "message": "autovacuum is OFF — tables will bloat and statistics go stale."})
        if hit_ratio is not None and to_float(hit_ratio) < 95:
            findings.append({"severity": "warning",
                             "message": f"Buffer cache hit ratio is {to_float(hit_ratio):.1f} % "
                                        "(target ≥ 99 %). Consider increasing shared_buffers."})
        if not self._pgss_schema:
            findings.append({"severity": "warning",
                             "message": "pg_stat_statements extension is not installed — slow-query "
                                        "analysis is unavailable. Add it to shared_preload_libraries "
                                        "and run CREATE EXTENSION pg_stat_statements."})

        return kv_result([
            ("Database",                name),
            ("Size (MB)",               size_mb),
            ("Encoding / collation",    f"{enc} / {coll}"),
            ("Server uptime",           uptime),
            ("max_connections",         max_conn),
            ("shared_buffers",          shared_buf),
            ("work_mem",                work_mem),
            ("autovacuum",              autovac),
            ("track_io_timing",         io_timing),
            ("Buffer cache hit ratio %", hit_ratio),
            ("Statistics reset at",     stats_reset or "never (since cluster init)"),
            ("Extensions",              ext_list),
        ])

    # -- Slow queries ----------------------------------------------------- #

    def _slow_queries(self, cur, findings: list, notes: dict) -> dict:
        if not self._pgss_schema:
            return unsupported_result("pg_stat_statements extension is not installed in this "
                                      "database, so per-query timings are not available.")
        cols = ["execution_count", "avg_elapsed_ms", "total_elapsed_ms", "avg_rows",
                "avg_shared_blks_read", "cache_hit_pct", "query_text"]
        res = self._fetch_statements(cur)
        if not res["success"]:
            return res

        rows = []
        for r in res["rows"]:
            key = tuple(r[:3])
            calls, total, nrows, hit, read, query = r[3:]
            if is_own_query(query):
                continue
            calls, total, nrows = calls or 0, to_float(total), nrows or 0
            hit, read = hit or 0, read or 0
            if self._base_statements is not None:
                b = self._base_statements.get(key)
                if b:
                    calls -= b[0] or 0
                    total -= to_float(b[1])
                    nrows -= b[2] or 0
                    hit   -= b[3] or 0
                    read  -= b[4] or 0
            if calls <= 0 or total <= 0:
                continue
            avg = total / calls
            rows.append([calls, round(avg, 2), round(total, 2), round(nrows / calls, 1),
                         round(read / calls, 1),
                         round(100.0 * hit / (hit + read), 2) if (hit + read) else None,
                         query])
        rows.sort(key=lambda x: x[1], reverse=True)
        if self._base_statements is None:
            notes["slow_queries"] = ("Cumulative pg_stat_statements data since its last reset "
                                     "(pg_stat_statements_reset()).")
        else:
            notes["slow_queries"] = "Only executions that happened during the scan window (counter delta)."
        return ok_result(cols, rows[:30])

    def _functions(self, cur, notes: dict) -> dict:
        sf, sp = self._schema_filter("schemaname")
        res = self._safe_query(cur, f"""
            SELECT schemaname || '.' || funcname          AS object_name,
                   'FUNCTION / PROCEDURE'                 AS object_type,
                   calls                                  AS execution_count,
                   ROUND((total_time / NULLIF(calls,0))::numeric, 2) AS avg_elapsed_ms,
                   ROUND((self_time  / NULLIF(calls,0))::numeric, 2) AS avg_self_ms,
                   ROUND(total_time::numeric, 2)          AS total_elapsed_ms
            FROM   pg_stat_user_functions
            WHERE  calls > 0{sf}
            ORDER  BY avg_elapsed_ms DESC
            LIMIT  20""", sp or None)
        if res["success"] and not res["rows"]:
            setting = self._scalar(cur, "SELECT current_setting('track_functions')")
            if str(setting).lower() == "none":
                notes["object_performance"] = ("track_functions = none — set it to 'pl' (or 'all') "
                                               "to collect function / procedure timings.")
        return res

    # -- Throughput ------------------------------------------------------- #

    def _throughput(self, cur) -> dict:
        res = self._safe_query(cur, """
            SELECT xact_commit, xact_rollback, tup_returned, tup_fetched, tup_inserted,
                   tup_updated, tup_deleted, deadlocks, temp_files, temp_bytes,
                   blks_read, blks_hit,
                   GREATEST(EXTRACT(EPOCH FROM now() - COALESCE(stats_reset, pg_postmaster_start_time())), 1)
            FROM   pg_stat_database WHERE datname = current_database()""")
        if not res["success"] or not res["rows"]:
            return res
        names = ["Transactions committed", "Transactions rolled back", "Rows returned",
                 "Rows fetched", "Rows inserted", "Rows updated", "Rows deleted", "Deadlocks",
                 "Temp files created", "Temp bytes written", "Blocks read (disk)", "Blocks hit (cache)"]
        vals = res["rows"][0]
        secs = to_float(vals[-1], 1.0)
        rows = [[n, v, round(to_float(v) / secs, 2)] for n, v in zip(names, vals[:-1])]
        return ok_result(["counter_name", "cumulative_value", "avg_per_sec"], rows)

    # -- Missing indexes (heuristic) ---------------------------------------- #

    def _missing_indexes(self, cur, notes: dict) -> dict:
        sf, sp = self._schema_filter("schemaname")
        res = self._safe_query(cur, f"""
            SELECT relid, schemaname || '.' || relname, seq_scan, seq_tup_read,
                   COALESCE(idx_scan, 0), n_live_tup,
                   pg_size_pretty(pg_relation_size(relid))
            FROM   pg_stat_user_tables
            WHERE  n_live_tup > 10000{sf}""", sp or None)
        if not res["success"]:
            return res
        rows = []
        for relid, name, seq, seq_rows, idx, live, size in res["rows"]:
            seq, seq_rows, idx = seq or 0, seq_rows or 0, idx or 0
            if self._base_tables is not None and relid in self._base_tables:
                b = self._base_tables[relid]
                seq, seq_rows, idx = seq - b[0], seq_rows - b[1], idx - b[2]
            if seq <= 0 or seq_rows / seq < 1000 or seq <= idx:
                continue
            live = live or 0
            priority = "HIGH" if live >= 1_000_000 else "MEDIUM" if live >= 100_000 else "LOW"
            rows.append([priority, name, seq, seq_rows, idx, live,
                         int(seq_rows / seq), size,
                         f"Find the WHERE / JOIN columns used against {name} in Slow Queries "
                         f"and add an index, e.g. CREATE INDEX CONCURRENTLY ON {name} (<column>);"])
        order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        rows.sort(key=lambda r: (order[r[0]], -r[3]))
        notes["missing_indexes"] = (
            "PostgreSQL has no missing-index advisor. These large tables are read mostly by "
            "sequential scans rather than index scans — a strong sign of a missing index."
            + (" Counts are deltas for the scan window." if self._base_tables is not None else ""))
        return ok_result(["priority", "table_name", "seq_scans", "rows_read_by_seq_scans",
                          "index_scans", "live_rows", "avg_rows_per_seq_scan", "table_size",
                          "suggestion"], rows[:30])

    # -- Fragmentation ---------------------------------------------------- #

    def _fragmentation(self, cur, notes: dict) -> dict:
        sf, sp = self._schema_filter("n.nspname")
        if self._has_ext(cur, "pgstattuple"):
            # pgstatindex() reads the whole index, so restrict it to the 50 largest
            # b-tree indexes below 2 GB (and the service runs this only once per session).
            res = self._safe_query(cur, f"""
                WITH big AS (
                  SELECT n.nspname, t.relname AS table_name, c.relname AS index_name,
                         c.oid, c.relpages
                  FROM   pg_index i
                  JOIN   pg_class c ON c.oid = i.indexrelid
                  JOIN   pg_class t ON t.oid = i.indrelid
                  JOIN   pg_namespace n ON n.oid = c.relnamespace
                  JOIN   pg_am am ON am.oid = c.relam
                  WHERE  am.amname = 'btree' AND c.relpages > 100
                    AND  pg_relation_size(c.oid) < 2147483648{sf}
                  ORDER  BY c.relpages DESC LIMIT 50)
                SELECT * FROM (
                  SELECT b.table_name, b.index_name, 'btree' AS index_type,
                         ROUND(s.leaf_fragmentation::numeric, 2)   AS fragmentation_pct,
                         ROUND(s.avg_leaf_density::numeric, 2)     AS avg_leaf_density_pct,
                         b.relpages                                AS page_count,
                         pg_size_pretty(pg_relation_size(b.oid))   AS index_size,
                         'REINDEX INDEX CONCURRENTLY ' || quote_ident(b.nspname) || '.' ||
                             quote_ident(b.index_name) || ';'      AS fix_script
                  FROM   big b CROSS JOIN LATERAL pgstatindex(b.oid::regclass) s) x
                WHERE fragmentation_pct > 5 OR avg_leaf_density_pct < 60
                ORDER BY fragmentation_pct DESC LIMIT 40""", sp or None)
            notes["index_fragmentation"] = ("Exact values from pgstattuple.pgstatindex() for the 50 "
                                            "largest b-tree indexes (< 2 GB): leaf_fragmentation and "
                                            "avg_leaf_density.")
            return self._add_action(res)

        res = self._safe_query(cur, f"""
            WITH idx AS (
              SELECT n.nspname, t.relname AS table_name, c.relname AS index_name, c.relpages,
                     c.reltuples,
                     (SELECT COALESCE(SUM(st.avg_width), 0)
                        FROM pg_attribute a
                        JOIN pg_stats st ON st.schemaname = n.nspname AND st.tablename = t.relname
                                        AND st.attname = a.attname
                       WHERE a.attrelid = t.oid AND a.attnum = ANY (i.indkey)) AS key_width
              FROM   pg_index i
              JOIN   pg_class c ON c.oid = i.indexrelid
              JOIN   pg_class t ON t.oid = i.indrelid
              JOIN   pg_namespace n ON n.oid = c.relnamespace
              JOIN   pg_am am ON am.oid = c.relam
              WHERE  am.amname = 'btree' AND c.relpages > 100 AND c.reltuples > 0{sf}
            )
            SELECT table_name, index_name, 'btree' AS index_type,
                   ROUND(GREATEST(0, 100 * (1 - CEIL(reltuples * (key_width + 16) /
                         (current_setting('block_size')::int * 0.9)) / relpages))::numeric, 2)
                                                         AS fragmentation_pct,
                   relpages                              AS page_count,
                   CEIL(reltuples * (key_width + 16) /
                        (current_setting('block_size')::int * 0.9))::bigint AS estimated_ideal_pages,
                   'REINDEX INDEX CONCURRENTLY ' || quote_ident(nspname) || '.' ||
                        quote_ident(index_name) || ';'  AS fix_script
            FROM   idx
            WHERE  key_width > 0
            ORDER  BY fragmentation_pct DESC
            LIMIT  40""", sp or None)
        if res["success"] and "fragmentation_pct" in res["columns"]:
            pct = res["columns"].index("fragmentation_pct")
            res["rows"] = [r for r in res["rows"] if to_float(r[pct]) > 5]
        notes["index_fragmentation"] = (
            "Estimated index bloat (actual pages vs. ideal size from column statistics). "
            "Install the pgstattuple extension for exact measurements. Run ANALYZE first for best accuracy.")
        return self._add_action(res)

    @staticmethod
    def _add_action(res: dict) -> dict:
        if not res["success"] or "fragmentation_pct" not in res["columns"]:
            return res
        idx = res["columns"].index("fragmentation_pct")
        res["columns"].insert(idx + 1, "recommended_action")
        for r in res["rows"]:
            p = to_float(r[idx])
            r.insert(idx + 1, "REINDEX" if p >= 30 else "MONITOR" if p >= 10 else "OK")
        return res

    # -- Unused indexes --------------------------------------------------- #

    def _unused_indexes(self, cur, notes: dict) -> dict:
        sf, sp = self._schema_filter("s.schemaname")
        res = self._safe_query(cur, f"""
            SELECT s.relname                                  AS table_name,
                   s.indexrelname                             AS index_name,
                   s.idx_scan                                 AS index_scans,
                   pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
                   pg_relation_size(s.indexrelid)             AS index_bytes,
                   'DROP INDEX CONCURRENTLY ' || quote_ident(s.schemaname) || '.' ||
                        quote_ident(s.indexrelname) || ';'    AS drop_script
            FROM   pg_stat_user_indexes s
            JOIN   pg_index i ON i.indexrelid = s.indexrelid
            WHERE  s.idx_scan = 0
              AND  NOT i.indisunique
              AND  NOT i.indisprimary
              AND  NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = s.indexrelid){sf}
            ORDER  BY pg_relation_size(s.indexrelid) DESC
            LIMIT  50""", sp or None)
        notes["unused_indexes"] = ("idx_scan = 0 since statistics were last reset. On replicas, "
                                   "check usage on every node before dropping.")
        return res

    # -- IOPS ------------------------------------------------------------- #

    def _iops(self, cur, notes: dict) -> dict:
        sf, sp = self._schema_filter("schemaname")
        db = self._safe_query(cur, """
            SELECT blks_read, blk_read_time, blk_write_time
            FROM pg_stat_database WHERE datname = current_database()""")
        read_lat = None
        if db["success"] and db["rows"]:
            blks, rtime, _ = db["rows"][0]
            if blks and to_float(rtime) > 0:
                read_lat = round(to_float(rtime) / blks, 3)
        notes["iops"] = (
            f"Database average read latency: {read_lat} ms per block." if read_lat is not None
            else "Per-block I/O latency requires track_io_timing = on.")
        return self._safe_query(cur, f"""
            SELECT schemaname || '.' || relname                     AS object_name,
                   heap_blks_read, heap_blks_hit,
                   COALESCE(idx_blks_read, 0)                       AS idx_blks_read,
                   COALESCE(idx_blks_hit, 0)                        AS idx_blks_hit,
                   ROUND(100.0 * (heap_blks_hit + COALESCE(idx_blks_hit,0)) /
                         NULLIF(heap_blks_hit + heap_blks_read + COALESCE(idx_blks_hit,0)
                                + COALESCE(idx_blks_read,0), 0), 2)  AS cache_hit_pct,
                   ROUND((heap_blks_read + COALESCE(idx_blks_read,0)) *
                         current_setting('block_size')::int / 1048576.0, 2) AS mb_read_from_disk
            FROM   pg_statio_user_tables
            WHERE  heap_blks_read + COALESCE(idx_blks_read,0) > 0{sf}
            ORDER  BY heap_blks_read + COALESCE(idx_blks_read,0) DESC
            LIMIT  25""", sp or None)

    # -- Connections ------------------------------------------------------ #

    def _connections(self, cur) -> dict:
        has_btype = self._version_num >= 100000
        client = "backend_type = 'client backend'" if has_btype else "true"
        bg = ("SUM(CASE WHEN backend_type <> 'client backend' THEN 1 ELSE 0 END)"
              if has_btype else "0")
        summary = self._safe_query(cur, f"""
            SELECT SUM(CASE WHEN {client} THEN 1 ELSE 0 END)                         AS total_sessions,
                   SUM(CASE WHEN {client} AND state = 'active' THEN 1 ELSE 0 END)     AS running_sessions,
                   SUM(CASE WHEN {client} AND state = 'idle' THEN 1 ELSE 0 END)       AS sleeping_sessions,
                   {bg}                                                               AS background_sessions,
                   SUM(CASE WHEN state IN ('idle in transaction', 'idle in transaction (aborted)')
                            THEN 1 ELSE 0 END)                                        AS sessions_with_open_txn,
                   NULL::bigint                                                       AS total_memory_kb
            FROM   pg_stat_activity""")
        details = self._safe_query(cur, """
            SELECT pid, usename AS login_name, client_addr::text AS client_addr,
                   LEFT(application_name, 60) AS program_name, state,
                   to_char(backend_start, 'YYYY-MM-DD HH24:MI:SS') AS login_time,
                   to_char(xact_start,    'YYYY-MM-DD HH24:MI:SS') AS transaction_start,
                   to_char(query_start,   'YYYY-MM-DD HH24:MI:SS') AS last_request_start,
                   wait_event_type, wait_event,
                   LEFT(query, 200) AS current_query
            FROM   pg_stat_activity
            WHERE  datname = current_database() AND pid <> pg_backend_pid()
            ORDER  BY (state = 'active') DESC, query_start
            LIMIT  40""")
        return {"summary": summary, "details": details}

    # -- Extras ----------------------------------------------------------- #

    def _dead_tuples(self, cur, findings: list) -> dict:
        sf, sp = self._schema_filter("schemaname")
        res = self._safe_query(cur, f"""
            SELECT schemaname || '.' || relname                         AS table_name,
                   n_live_tup, n_dead_tup,
                   ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS dead_pct,
                   to_char(last_autovacuum, 'YYYY-MM-DD HH24:MI:SS')    AS last_autovacuum,
                   to_char(last_autoanalyze,'YYYY-MM-DD HH24:MI:SS')    AS last_autoanalyze,
                   'VACUUM (ANALYZE) ' || quote_ident(schemaname) || '.' || quote_ident(relname) || ';' AS fix_script
            FROM   pg_stat_user_tables
            WHERE  n_dead_tup > 1000{sf}
            ORDER  BY n_dead_tup DESC
            LIMIT  25""", sp or None)
        if res["success"]:
            bad = sum(1 for r in res["rows"] if to_float(col(res, r, "dead_pct")) >= 20)
            if bad:
                findings.append({"severity": "warning",
                                 "message": f"{bad} table{'s' if bad > 1 else ''} with ≥ 20 % dead "
                                            "tuples — autovacuum is not keeping up."})
        return res

    def _blocking(self, cur) -> dict:
        if self._version_num < 90600:
            return unsupported_result("pg_blocking_pids() requires PostgreSQL 9.6+.")
        return self._safe_query(cur, """
            SELECT pid AS blocked_pid, usename AS login_name,
                   pg_blocking_pids(pid)::text AS blocked_by,
                   wait_event_type, wait_event,
                   date_trunc('second', now() - query_start)::text AS waiting_for,
                   LEFT(query, 200) AS blocked_query
            FROM   pg_stat_activity
            WHERE  cardinality(pg_blocking_pids(pid)) > 0
            ORDER  BY query_start""")
