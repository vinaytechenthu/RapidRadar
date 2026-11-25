"""
db_scan.sqlserver
-----------------
Microsoft SQL Server scanner (DMV based).

Driver : pyodbc + "ODBC Driver 17/18 for SQL Server" (older drivers also work)
Auth   : Windows (Trusted_Connection) or SQL Server login
Rights : VIEW SERVER STATE + VIEW DATABASE STATE
"""

from __future__ import annotations


from .base import MARKER, BaseDBScanner, ConnectionConfig, col, kv_result


def _odbc_value(value: str) -> str:
    """Quote an ODBC connection-string value so ';' / '}' cannot inject keys."""
    if any(ch in value for ch in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


class SQLServerScanner(BaseDBScanner):

    db_type = "sqlserver"

    _DRIVER_PREFERENCE = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]

    def __init__(self, cfg: ConnectionConfig) -> None:
        super().__init__(cfg)
        self._odbc_driver: str | None = None

    # ------------------------------------------------------------------ #
    #  Connection                                                          #
    # ------------------------------------------------------------------ #

    def _detect_driver(self) -> str:
        if self._odbc_driver:
            return self._odbc_driver
        import pyodbc
        available = pyodbc.drivers()
        for d in self._DRIVER_PREFERENCE:
            if d in available:
                self._odbc_driver = d
                self.driver_name = f"pyodbc / {d}"
                return d
        raise RuntimeError(f"No SQL Server ODBC driver found. Installed drivers: {available}. "
                           "Install 'ODBC Driver 18 for SQL Server'.")

    def _conn_str(self) -> str:
        server = self.cfg.server
        if self.cfg.port and "," not in server:
            server = f"{server},{self.cfg.port}"
        parts = [
            f"DRIVER={{{self._detect_driver()}}}",
            f"SERVER={_odbc_value(server)}",
            f"DATABASE={_odbc_value(self.cfg.database)}",
            "Connection Timeout=30",
            "TrustServerCertificate=yes",
            "APP=RapidRadar DBScan",
        ]
        if self.cfg.auth == "windows":
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={_odbc_value(self.cfg.username)}")
            parts.append(f"PWD={_odbc_value(self.cfg.password)}")
        return ";".join(parts) + ";"

    def _connect(self):
        import pyodbc
        return pyodbc.connect(self._conn_str(), autocommit=True)

    def _version_and_time(self, cursor):
        cursor.execute("SELECT @@VERSION, GETDATE()")
        row = cursor.fetchone()
        return str(row[0]).splitlines()[0].strip(), row[1]

    # ------------------------------------------------------------------ #
    #  Collection                                                          #
    # ------------------------------------------------------------------ #

    def _collect(self, cur) -> dict:
        findings: list[dict] = []
        notes: dict[str, str] = {}
        if self.window_start is None:
            notes["slow_queries"] = ("Cumulative statistics from the plan cache "
                                     "(since the plan was cached / last SQL Server restart).")

        out = {
            "db_info":             self._db_info(cur, findings),
            "slow_queries":        self._slow_queries(cur),
            "object_performance":  self._object_performance(cur),
            "throughput":          self._throughput(cur),
            "missing_indexes":     self._missing_indexes(cur),
            "index_fragmentation": self._heavy("index_fragmentation", notes,
                                               self._index_fragmentation, cur),
            "unused_indexes":      self._unused_indexes(cur),
            "iops":                self._iops(cur),
            "cpu_utilization":     self._cpu(cur),
            "connections":         self._connections(cur),
            "extra_sections": [{
                "id": "sec-waits", "title": "Top Wait Statistics", "icon": "bi-hourglass-split",
                "result": self._wait_stats(cur),
                "note": "Cumulative since the last SQL Server restart; benign/idle waits are excluded.",
            }],
        }
        out["findings"] = findings
        out["notes"] = notes
        return out

    # -- DB information --------------------------------------------------- #

    def _db_info(self, cur, findings: list) -> dict:
        res = self._safe_query(cur, """
        SELECT d.name, d.compatibility_level, d.recovery_model_desc, d.state_desc,
               CAST(d.is_auto_shrink_on       AS INT) AS is_auto_shrink_on,
               CAST(d.is_auto_update_stats_on AS INT) AS is_auto_update_stats_on,
               CAST(d.is_auto_create_stats_on AS INT) AS is_auto_create_stats_on,
               CAST(d.is_read_only            AS INT) AS is_read_only,
               CAST(d.is_read_committed_snapshot_on AS INT) AS is_rcsi_on,
               d.collation_name,
               CONVERT(VARCHAR(19), d.create_date, 120) AS create_date,
               ROUND(SUM(CAST(mf.size AS BIGINT)) * 8.0 / 1024.0, 2) AS total_size_mb
        FROM   sys.databases d
        JOIN   sys.master_files mf ON d.database_id = mf.database_id
        WHERE  d.name = ?
        GROUP  BY d.name, d.compatibility_level, d.recovery_model_desc, d.state_desc,
                  d.is_auto_shrink_on, d.is_auto_update_stats_on, d.is_auto_create_stats_on,
                  d.is_read_only, d.is_read_committed_snapshot_on, d.collation_name, d.create_date
        """, [self.cfg.database])
        if not res["success"] or not res["rows"]:
            return res
        r = res["rows"][0]
        g = lambda c: col(res, r, c)                                   # noqa: E731
        yn = lambda v: "ON" if str(v) == "1" else "OFF"                # noqa: E731

        if str(g("is_auto_shrink_on")) == "1":
            findings.append({"severity": "warning",
                             "message": "AUTO_SHRINK is ON — causes index fragmentation and I/O spikes."})
        if str(g("is_auto_update_stats_on")) != "1":
            findings.append({"severity": "issue",
                             "message": "AUTO_UPDATE_STATISTICS is OFF — the optimizer may choose poor plans."})
        if str(g("is_auto_create_stats_on")) != "1":
            findings.append({"severity": "warning", "message": "AUTO_CREATE_STATISTICS is OFF."})

        return kv_result([
            ("Database",                 g("name")),
            ("Size (MB)",                g("total_size_mb")),
            ("Compatibility level",      g("compatibility_level")),
            ("Recovery model",           g("recovery_model_desc")),
            ("State",                    g("state_desc")),
            ("Collation",                g("collation_name")),
            ("Auto-shrink",              yn(g("is_auto_shrink_on"))),
            ("Auto-update statistics",   yn(g("is_auto_update_stats_on"))),
            ("Auto-create statistics",   yn(g("is_auto_create_stats_on"))),
            ("Read committed snapshot",  yn(g("is_rcsi_on"))),
            ("Read-only",                yn(g("is_read_only"))),
            ("Created",                  g("create_date")),
        ])

    # -- Slow queries ----------------------------------------------------- #

    def _window_clause(self, column: str) -> tuple[str, list]:
        # Exclude RapidRadar's own diagnostic statements (tagged by _safe_query).
        own = " AND st.text NOT LIKE N'%" + MARKER + "%'" if column.startswith("qs.") else ""
        if self.window_start is None:
            return own, []
        # DMV times are DATETIME (3.33 ms precision) while the parameter arrives as
        # DATETIME2, so allow 1 s of tolerance to avoid dropping edge executions.
        return own + f" AND {column} >= DATEADD(SECOND, -1, CAST(? AS DATETIME2))", [self.window_start]

    def _query_counters(self, cur) -> dict:
        wc, wp = self._window_clause("qs.last_execution_time")
        sql = f"""
        SELECT CONVERT(VARCHAR(130), qs.sql_handle, 1) + ':' +
               CAST(qs.statement_start_offset AS VARCHAR(12)) + ':' +
               CONVERT(VARCHAR(130), qs.plan_handle, 1)               AS k,
               qs.execution_count                                     AS executions,
               CAST(qs.total_elapsed_time AS FLOAT) / 1000.0          AS elapsed_ms,
               CAST(qs.total_worker_time  AS FLOAT) / 1000.0          AS cpu_ms,
               qs.total_logical_reads                                 AS logical_reads,
               SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
                   ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                     ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS query_text
        FROM   sys.dm_exec_query_stats qs
        CROSS  APPLY sys.dm_exec_sql_text(qs.sql_handle) st
        CROSS  APPLY (SELECT CONVERT(INT, pa.value) AS dbid
                      FROM   sys.dm_exec_plan_attributes(qs.plan_handle) pa
                      WHERE  pa.attribute = N'dbid') pdb
        WHERE  pdb.dbid = DB_ID(?){wc}
        """
        return self._safe_query(cur, sql, [self.cfg.database] + wp)

    def _slow_queries(self, cur) -> dict:
        wc, wp = self._window_clause("qs.last_execution_time")
        sql = f"""
        SELECT TOP 30
            qs.execution_count,
            ROUND(CAST(qs.total_elapsed_time  AS FLOAT) / NULLIF(qs.execution_count,0) / 1000.0, 2) AS avg_elapsed_ms,
            ROUND(CAST(qs.total_worker_time   AS FLOAT) / NULLIF(qs.execution_count,0) / 1000.0, 2) AS avg_cpu_ms,
            ROUND(CAST(qs.total_logical_reads AS FLOAT) / NULLIF(qs.execution_count,0), 0)           AS avg_logical_reads,
            ROUND(CAST(qs.total_physical_reads AS FLOAT)/ NULLIF(qs.execution_count,0), 0)           AS avg_physical_reads,
            qs.total_logical_reads,
            qs.total_physical_reads,
            qs.total_logical_writes,
            SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
                ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                  ELSE qs.statement_end_offset END - qs.statement_start_offset)/2)+1) AS query_text,
            OBJECT_NAME(st.objectid, st.dbid)                  AS object_name,
            CONVERT(VARCHAR(23), qs.last_execution_time, 121)  AS last_execution_time
        FROM   sys.dm_exec_query_stats qs
        CROSS  APPLY sys.dm_exec_sql_text(qs.sql_handle) st
        -- st.dbid is NULL for ad-hoc / prepared (sp_executesql) statements, which is
        -- what most web applications send, so use the plan's dbid attribute instead.
        CROSS  APPLY (SELECT CONVERT(INT, pa.value) AS dbid
                      FROM   sys.dm_exec_plan_attributes(qs.plan_handle) pa
                      WHERE  pa.attribute = N'dbid') pdb
        WHERE  pdb.dbid = DB_ID(?)
          AND  qs.total_elapsed_time > 0{wc}
        ORDER  BY avg_elapsed_ms DESC
        """
        return self._safe_query(cur, sql, [self.cfg.database] + wp)

    def _object_performance(self, cur) -> dict:
        wc, wp = self._window_clause("qs.last_execution_time")
        sql = f"""
        SELECT TOP 20
            OBJECT_NAME(st.objectid, st.dbid)                                     AS object_name,
            o.type_desc                                                            AS object_type,
            SUM(qs.execution_count)                                                AS execution_count,
            ROUND(CAST(SUM(qs.total_elapsed_time) AS FLOAT) /
                  NULLIF(SUM(qs.execution_count), 0) / 1000.0, 2)                  AS avg_elapsed_ms,
            ROUND(CAST(SUM(qs.total_worker_time) AS FLOAT) /
                  NULLIF(SUM(qs.execution_count), 0) / 1000.0, 2)                  AS avg_cpu_ms,
            ROUND(CAST(SUM(qs.total_logical_reads) AS FLOAT) /
                  NULLIF(SUM(qs.execution_count), 0), 0)                           AS avg_logical_reads,
            SUM(qs.total_physical_reads)                                           AS total_physical_reads,
            CONVERT(VARCHAR(23), MAX(qs.last_execution_time), 121)                 AS last_execution_time
        FROM   sys.dm_exec_query_stats qs
        CROSS  APPLY sys.dm_exec_sql_text(qs.sql_handle) st
        JOIN   sys.objects o ON st.objectid = o.object_id
        WHERE  st.dbid = DB_ID(?)
          AND  o.type IN ('P','FN','IF','TF','V'){wc}
        GROUP  BY st.objectid, st.dbid, o.type_desc
        ORDER  BY avg_elapsed_ms DESC
        """
        return self._safe_query(cur, sql, [self.cfg.database] + wp)

    # -- Throughput ------------------------------------------------------- #

    def _throughput(self, cur) -> dict:
        res = self._safe_query(cur, """
        SELECT RTRIM(pc.counter_name) AS counter_name,
               pc.cntr_value          AS cumulative_value,
               ROUND(CAST(pc.cntr_value AS FLOAT) /
                     NULLIF(DATEDIFF(SECOND, si.sqlserver_start_time, GETDATE()), 0), 2) AS avg_per_sec
        FROM   sys.dm_os_performance_counters pc
        CROSS  JOIN sys.dm_os_sys_info si
        WHERE  pc.object_name LIKE '%SQL Statistics%'
          AND  RTRIM(pc.counter_name) IN ('Batch Requests/sec','SQL Compilations/sec',
                 'SQL Re-Compilations/sec','Auto-Param Attempts/sec','Failed Auto-Params/sec',
                 'Safe Auto-Params/sec','Unsafe Auto-Params/sec')
        ORDER  BY pc.counter_name
        """)
        return res

    # -- Missing indexes -------------------------------------------------- #

    def _missing_indexes(self, cur) -> dict:
        wc = ""
        wp: list = []
        if self.window_start is not None:
            wc = (" AND (migs.last_user_seek >= DATEADD(SECOND, -1, CAST(? AS DATETIME2))"
                  " OR migs.last_user_scan >= DATEADD(SECOND, -1, CAST(? AS DATETIME2)))")
            wp = [self.window_start, self.window_start]
        sql = f"""
        SELECT
            CASE WHEN migs.avg_user_impact >= 70 THEN 'HIGH'
                 WHEN migs.avg_user_impact >= 40 THEN 'MEDIUM' ELSE 'LOW' END       AS priority,
            OBJECT_NAME(mid.object_id, mid.database_id)                            AS table_name,
            mid.equality_columns, mid.inequality_columns, mid.included_columns,
            migs.user_seeks, migs.user_scans,
            CONVERT(VARCHAR(23), migs.last_user_seek, 121)                         AS last_user_seek,
            ROUND(migs.avg_total_user_cost * migs.avg_user_impact *
                  (migs.user_seeks + migs.user_scans), 2)                          AS improvement_score,
            ROUND(migs.avg_user_impact, 2)                                         AS avg_user_impact_pct,
            'CREATE INDEX [IX_' + OBJECT_NAME(mid.object_id, mid.database_id)
              + '_' + CONVERT(VARCHAR(10), mig.index_group_handle) + '] ON '
              + mid.statement + ' ('
              + ISNULL(mid.equality_columns, '')
              + CASE WHEN mid.equality_columns IS NOT NULL
                      AND mid.inequality_columns IS NOT NULL THEN ', ' ELSE '' END
              + ISNULL(mid.inequality_columns, '') + ')'
              + CASE WHEN mid.included_columns IS NOT NULL
                     THEN ' INCLUDE (' + mid.included_columns + ')' ELSE '' END    AS suggested_index_sql
        FROM   sys.dm_db_missing_index_details     mid
        JOIN   sys.dm_db_missing_index_groups      mig  ON mid.index_handle = mig.index_handle
        JOIN   sys.dm_db_missing_index_group_stats migs ON mig.index_group_handle = migs.group_handle
        WHERE  mid.database_id = DB_ID(?){wc}
        ORDER  BY improvement_score DESC
        """
        return self._safe_query(cur, sql, [self.cfg.database] + wp)

    # -- Fragmentation ---------------------------------------------------- #

    def _index_fragmentation(self, cur) -> dict:
        return self._safe_query(cur, """
        SELECT TOP 40
            OBJECT_NAME(ips.object_id)                         AS table_name,
            i.name                                             AS index_name,
            i.type_desc                                        AS index_type,
            ROUND(ips.avg_fragmentation_in_percent, 2)         AS fragmentation_pct,
            ips.page_count,
            CASE WHEN ips.avg_fragmentation_in_percent >= 30 THEN 'REBUILD'
                 WHEN ips.avg_fragmentation_in_percent >= 10 THEN 'REORGANIZE'
                 ELSE 'OK' END                                 AS recommended_action,
            CASE WHEN ips.avg_fragmentation_in_percent >= 30
                   THEN 'ALTER INDEX [' + i.name + '] ON [' + SCHEMA_NAME(t.schema_id) + '].['
                        + OBJECT_NAME(ips.object_id) + '] REBUILD WITH (ONLINE = ON);'
                 WHEN ips.avg_fragmentation_in_percent >= 10
                   THEN 'ALTER INDEX [' + i.name + '] ON [' + SCHEMA_NAME(t.schema_id) + '].['
                        + OBJECT_NAME(ips.object_id) + '] REORGANIZE;'
                 ELSE NULL END                                 AS fix_script
        FROM   sys.dm_db_index_physical_stats(DB_ID(?), NULL, NULL, NULL, 'LIMITED') ips
        JOIN   sys.indexes i ON ips.object_id = i.object_id AND ips.index_id = i.index_id
        JOIN   sys.tables  t ON i.object_id = t.object_id
        WHERE  ips.avg_fragmentation_in_percent > 5
          AND  ips.page_count > 100
          AND  i.name IS NOT NULL
        ORDER  BY ips.avg_fragmentation_in_percent DESC
        """, [self.cfg.database])

    # -- Unused indexes --------------------------------------------------- #

    def _unused_indexes(self, cur) -> dict:
        return self._safe_query(cur, """
        SELECT
            OBJECT_NAME(i.object_id)                 AS table_name,
            i.name                                   AS index_name,
            i.type_desc                              AS index_type,
            ISNULL(ius.user_seeks,   0)              AS user_seeks,
            ISNULL(ius.user_scans,   0)              AS user_scans,
            ISNULL(ius.user_lookups, 0)              AS user_lookups,
            ISNULL(ius.user_updates, 0)              AS user_updates,
            'DROP INDEX [' + i.name + '] ON [' + SCHEMA_NAME(t.schema_id) + '].['
              + OBJECT_NAME(i.object_id) + '];'      AS drop_script
        FROM   sys.indexes i
        JOIN   sys.tables t ON i.object_id = t.object_id
        LEFT   JOIN sys.dm_db_index_usage_stats ius
               ON  i.object_id = ius.object_id AND i.index_id = ius.index_id
               AND ius.database_id = DB_ID(?)
        WHERE  i.type > 0
          AND  i.is_primary_key = 0
          AND  i.is_unique_constraint = 0
          AND  ISNULL(ius.user_seeks, 0) = 0
          AND  ISNULL(ius.user_scans, 0) = 0
          AND  ISNULL(ius.user_lookups, 0) = 0
          AND  ISNULL(ius.user_updates, 0) > 0
        ORDER  BY ius.user_updates DESC
        """, [self.cfg.database])

    # -- IOPS ------------------------------------------------------------- #

    def _iops(self, cur) -> dict:
        return self._safe_query(cur, """
        SELECT
            mf.name                                                AS logical_file_name,
            mf.physical_name,
            mf.type_desc                                           AS file_type,
            vfs.num_of_reads,
            vfs.num_of_writes,
            ROUND(CAST(vfs.io_stall_read_ms  AS FLOAT) / NULLIF(vfs.num_of_reads,  0), 2) AS avg_read_latency_ms,
            ROUND(CAST(vfs.io_stall_write_ms AS FLOAT) / NULLIF(vfs.num_of_writes, 0), 2) AS avg_write_latency_ms,
            ROUND(vfs.num_of_bytes_read    / 1048576.0, 2)         AS mb_read,
            ROUND(vfs.num_of_bytes_written / 1048576.0, 2)         AS mb_written,
            vfs.io_stall                                           AS total_io_stall_ms
        FROM   sys.dm_io_virtual_file_stats(DB_ID(?), NULL) vfs
        JOIN   sys.master_files mf
               ON vfs.database_id = mf.database_id AND vfs.file_id = mf.file_id
        ORDER  BY vfs.io_stall DESC
        """, [self.cfg.database])

    # -- CPU -------------------------------------------------------------- #

    def _cpu(self, cur) -> dict:
        return self._safe_query(cur, """
        DECLARE @ts_now BIGINT = (SELECT cpu_ticks / (cpu_ticks / ms_ticks) FROM sys.dm_os_sys_info);
        SELECT TOP 20
            CONVERT(VARCHAR(19), DATEADD(ms, -1 * (@ts_now - [timestamp]), GETDATE()), 120) AS sample_time,
            record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS sql_cpu_pct,
            100 - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int')
                - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS other_cpu_pct,
            record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS system_idle_pct
        FROM (
            SELECT [timestamp], CONVERT(XML, record) AS record
            FROM   sys.dm_os_ring_buffers
            WHERE  ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR'
              AND  record LIKE N'%<SystemHealth>%'
        ) AS rb
        ORDER BY sample_time DESC
        """)

    # -- Connections ------------------------------------------------------ #

    def _connections(self, cur) -> dict:
        summary = self._safe_query(cur, """
        SELECT COUNT(*)                                                  AS total_sessions,
               SUM(CASE WHEN status = 'running'    THEN 1 ELSE 0 END)   AS running_sessions,
               SUM(CASE WHEN status = 'sleeping'   THEN 1 ELSE 0 END)   AS sleeping_sessions,
               SUM(CASE WHEN status = 'background' THEN 1 ELSE 0 END)   AS background_sessions,
               SUM(CASE WHEN open_transaction_count > 0 THEN 1 ELSE 0 END) AS sessions_with_open_txn,
               SUM(memory_usage) * 8                                     AS total_memory_kb
        FROM   sys.dm_exec_sessions
        WHERE  is_user_process = 1
        """)
        details = self._safe_query(cur, """
        SELECT TOP 40
            s.session_id, s.login_name, s.host_name,
            LEFT(s.program_name, 60)                                AS program_name,
            s.status, s.open_transaction_count,
            CONVERT(VARCHAR(23), s.login_time, 121)                 AS login_time,
            CONVERT(VARCHAR(23), s.last_request_start_time, 121)    AS last_request_start,
            s.cpu_time, s.memory_usage * 8 AS memory_kb, s.reads, s.writes, s.logical_reads,
            r.wait_type, r.blocking_session_id
        FROM   sys.dm_exec_sessions s
        LEFT   JOIN sys.dm_exec_requests r ON s.session_id = r.session_id
        WHERE  s.is_user_process = 1
          AND  s.database_id = DB_ID(?)
        ORDER  BY s.cpu_time DESC
        """, [self.cfg.database])
        return {"summary": summary, "details": details}

    # -- Wait stats (extra) ----------------------------------------------- #

    def _wait_stats(self, cur) -> dict:
        return self._safe_query(cur, """
        SELECT TOP 15
            wait_type,
            waiting_tasks_count,
            ROUND(wait_time_ms / 1000.0, 1)                                       AS wait_time_s,
            ROUND(CAST(wait_time_ms AS FLOAT) / NULLIF(waiting_tasks_count, 0), 2) AS avg_wait_ms,
            ROUND(100.0 * wait_time_ms / NULLIF(SUM(wait_time_ms) OVER (), 0), 2)  AS pct_of_total
        FROM   sys.dm_os_wait_stats
        WHERE  waiting_tasks_count > 0
          AND  wait_type NOT IN (
               'BROKER_EVENTHANDLER','BROKER_RECEIVE_WAITFOR','BROKER_TASK_STOP','BROKER_TO_FLUSH',
               'BROKER_TRANSMITTER','CHECKPOINT_QUEUE','CLR_AUTO_EVENT','CLR_MANUAL_EVENT',
               'CLR_SEMAPHORE','DIRTY_PAGE_POLL','DISPATCHER_QUEUE_SEMAPHORE','FT_IFTS_SCHEDULER_IDLE_WAIT',
               'FT_IFTSHC_MUTEX','HADR_FILESTREAM_IOMGR_IOCOMPLETION','HADR_WORK_QUEUE','LAZYWRITER_SLEEP',
               'LOGMGR_QUEUE','ONDEMAND_TASK_QUEUE','QDS_ASYNC_QUEUE',
               'QDS_PERSIST_TASK_MAIN_LOOP_SLEEP','QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP',
               'REQUEST_FOR_DEADLOCK_SEARCH','RESOURCE_QUEUE','SERVER_IDLE_CHECK','SLEEP_BPOOL_FLUSH',
               'SLEEP_DBSTARTUP','SLEEP_DCOMSTARTUP','SLEEP_MASTERDBREADY','SLEEP_MASTERMDREADY',
               'SLEEP_MASTERUPGRADED','SLEEP_MSDBSTARTUP','SLEEP_SYSTEMTASK','SLEEP_TASK',
               'SLEEP_TEMPDBSTARTUP','SNI_HTTP_ACCEPT','SP_SERVER_DIAGNOSTICS_SLEEP',
               'SQLTRACE_BUFFER_FLUSH','SQLTRACE_INCREMENTAL_FLUSH_SLEEP','WAITFOR','WAITFOR_TASKSHUTDOWN',
               'XE_DISPATCHER_JOIN','XE_DISPATCHER_WAIT','XE_TIMER_EVENT','XE_LIVE_TARGET_TVF',
               'PREEMPTIVE_XE_DISPATCHER','PWAIT_ALL_COMPONENTS_INITIALIZED',
               'PWAIT_EXTENSIBILITY_CLEANUP_TASK','SOS_WORK_DISPATCHER','KSOURCE_WAKEUP',
               'PARALLEL_REDO_DRAIN_WORKER','PARALLEL_REDO_LOG_CACHE','PARALLEL_REDO_TRAN_LIST',
               'PARALLEL_REDO_WORKER_SYNC','PARALLEL_REDO_WORKER_WAIT_WORK','VDI_CLIENT_OTHER',
               'WAIT_XTP_HOST_WAIT','WAIT_XTP_OFFLINE_CKPT_NEW_LOG','WAIT_XTP_CKPT_CLOSE')
        ORDER  BY wait_time_ms DESC
        """)
