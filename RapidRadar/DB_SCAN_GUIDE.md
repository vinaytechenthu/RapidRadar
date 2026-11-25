# 🗄️ RapidRadar — DB Scan Guide

**Created by OPUS EDM PERF TEAM**

RapidRadar now combines two utilities in one web UI:

| Utility | What it finds |
|---|---|
| **Page Load Analysis** | Slow pages, Web Vitals, slow APIs, large / render-blocking resources, accessibility (VPAT) |
| **DB Scan** | Slow queries, missing indexes, fragmented indexes, unused indexes, I/O latency, CPU, connections |

---

## Run modes

Pick a mode with the switch at the top of the page (or open `http://127.0.0.1:5000/?mode=dbscan`, `?mode=pageload` or `?mode=both`).

| Mode | What happens | Output |
|---|---|---|
| 🌐 **Page Load Analysis** | Same as before — log in, discover pages, test each page. | `reports/<run>/summary_report_*.html`, page reports, VPAT |
| 🗄️ **DB Scan** | Scans the database every *N* seconds until you press **Stop DB Scan & Generate Report**. Then it takes one final scan and writes the report. | `reports/DBScan_<type>_<db>_<time>/DBScan_*.html` (+ `_data.json`) |
| ⚡ **Page Load + DB Scan** | Opens an *analysis window* on the database, runs the page-load test, then automatically takes a final DB scan and writes the DB report into the **same run folder**. Only DB activity that happened **while the UI was being tested** is reported, and every query is linked to the page it ran for (see below). | Everything from Page Load + `DBScan_*.html` + `RapidRadar_Combined_<run>.html` + `page_db_correlation.json` |

The CSV upload ("Is Any Page Missed?") also works in the combined mode.

### Query scope (DB Scan mode)

* **Only activity after Start** — reports just the queries executed after you pressed Start (best when you reproduce a problem manually).
* **All cumulative statistics** — reports everything the engine has collected since its statistics were last reset / the server restarted (the original DBScan behaviour).

The combined mode always uses *Only activity after Start*.

### Pages ↔ Database (combined mode)

At the start and end of every page's test RapidRadar takes a quick snapshot of the database's
per-statement counters (execution count, elapsed time, CPU, reads). The difference is the work the
database did *for that page*. The combined report shows, per page:

| Column | Meaning |
|---|---|
| Cold / Warm load | Page load time without / with browser cache (slowest browser) |
| Test time | How long that page's test took (login + cold load + warm load) |
| DB runs / DB time / DB CPU | Statement executions, total elapsed and CPU time in the database during that page |
| Slowest query (avg) | Highest average execution time of any statement during that page |
| *Top statements* (expand) | The 10 heaviest statements during that page, with their SQL |

Pages are flagged **slow page** (cold load ≥ 3 s) and **slow query** (a statement averaging ≥ 1 s).
The raw data is saved as `page_db_correlation.json` in the run folder.

Things to know:
* Pages are tested one after another, so each page's window is separate. With several browsers
  selected, they test the same page in parallel and their DB work is combined.
* Everything the database executed in that window is counted — including other users. Test on a quiet
  environment for a clean picture.
* Needs the same rights as the slow-query check. PostgreSQL needs `pg_stat_statements`; MySQL needs
  `performance_schema`. If it isn't available the run still works and the report says why.

### Run Self-Check (new database or login)

**Run Self-Check** (next to *Test Connection*) runs every check once and lists the result of each:
✓ works, – not available for this database type, ✕ failed (usually a missing permission — the message
says which). It also tests per-page query tracking. Use it the first time you point RapidRadar at a new
server, especially PostgreSQL, Oracle or MySQL.

---

## Supported databases

The **database type tested** is shown in the report title, the navbar badge, the *Database Type Tested* card, the *Database Information* section, the combined landing page and the report file name (`DBScan_<type>_...`).

| Database | Python driver | Connection fields | Recommended rights for the scan login |
|---|---|---|---|
| **Microsoft SQL Server** 2012+ | `pyodbc` + *ODBC Driver 17/18 for SQL Server* | Server (`host`, `host\instance` or `host,port`), Database, Windows or SQL login | `VIEW SERVER STATE`, `VIEW DATABASE STATE` |
| **PostgreSQL** 10+ | `psycopg2-binary` | Host, Port (5432), Database, optional Schema, user / password | `pg_monitor` role; `pg_stat_statements` extension (for slow queries); optional `pgstattuple` |
| **Oracle Database** 12.1+ | `oracledb` (thin mode — no Instant Client) | Host, Port (1521), **Service name**, optional Schema/owner (default = login user), user / password | `SELECT_CATALOG_ROLE` (or `SELECT ANY DICTIONARY`) |
| **MySQL** 5.7+ / 8.x, **MariaDB** 10.x | `pymysql` | Host, Port (3306), Database (schema), user / password | `PROCESS`, `SELECT` on `performance_schema`; `performance_schema=ON` |

Missing drivers are flagged in the UI with the `pip install` command to run. `install.bat` installs all four.

> Use a dedicated, read-only monitoring login. RapidRadar only runs `SELECT` / catalog queries — it never creates, drops or rebuilds anything. Suggested fix scripts are shown for you to review and run yourself.

---

## What is checked per engine

| Check | SQL Server | PostgreSQL | Oracle | MySQL / MariaDB |
|---|---|---|---|---|
| Slow queries | `sys.dm_exec_query_stats` | `pg_stat_statements` (window = counter delta) | `V$SQLAREA` (parsing schema) | `events_statements_summary_by_digest` |
| Procedures / functions | `dm_exec_query_stats` + `sys.objects` | `pg_stat_user_functions` (`track_functions`) | `V$SQLAREA.PROGRAM_ID` | `events_statements_summary_by_program` (MySQL) |
| Missing indexes | Missing-index DMVs (+ `CREATE INDEX` script) | Large tables read mainly by sequential scans *(heuristic)* | Large tables hit by `FULL` table scans in cached plans *(heuristic)* | Statements run with no / no good index *(heuristic)* |
| Fragmentation | `dm_db_index_physical_stats` (LIMITED) | `pgstatindex()` if `pgstattuple` installed, else statistics-based estimate | Estimate from optimizer stats + `BLEVEL ≥ 4` | Table `DATA_FREE` (reclaimable space) |
| Unused indexes | `dm_db_index_usage_stats` | `pg_stat_user_indexes.idx_scan = 0` | `DBA_INDEX_USAGE` (12.2+) | `table_io_waits_summary_by_index_usage` |
| I/O | `dm_io_virtual_file_stats` | `pg_statio_user_tables` (+ latency with `track_io_timing`) | `V$FILESTAT` | `file_summary_by_instance` |
| CPU | Scheduler ring buffer | *n/a via SQL* | `V$SYSMETRIC_HISTORY` | *n/a via SQL* |
| Connections | `dm_exec_sessions` | `pg_stat_activity` | `V$SESSION` | `processlist` + `innodb_trx` |
| Extra | Top wait statistics | Dead tuples / VACUUM health, blocked sessions | Top wait events, stale optimizer statistics | Tables without a primary key |

Checks that an engine cannot provide are shown as *"Not available for &lt;engine&gt;"* instead of an error.
Expensive structural checks (fragmentation) run **once per scan session** and are reused by later scans.
RapidRadar tags its own SQL (`/* RapidRadar DBScan */`) and excludes it from the slow-query results.

---

## Reading the report

* **Health score** (0–100) — deductions for slow queries (> 1 s / > 5 s), HIGH/MEDIUM missing-index candidates, heavy fragmentation, high I/O latency, high CPU and engine-specific findings (e.g. AUTO_SHRINK on, autovacuum off, stale statistics). If most checks fail (usually missing privileges) the score is marked **Incomplete data**.
* **Scan Timeline** — one row per scan: sessions, active sessions, open transactions, DB CPU and the number of queries > 1 s. In the combined mode this shows how the DB behaved while each page was being tested.
* **Prioritised Recommendations** — issues (red), warnings (amber) and info (blue).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| *Python driver for X is not installed* | `pip install pyodbc` / `psycopg2-binary` / `oracledb` / `pymysql` (or rerun `install.bat`) |
| SQL Server: *No SQL Server ODBC driver found* | Install *Microsoft ODBC Driver 18 for SQL Server* |
| *N checks failed due to insufficient privileges* | Grant the rights listed in the table above to the scan login |
| PostgreSQL: slow queries "not available" | `shared_preload_libraries = 'pg_stat_statements'`, restart, then `CREATE EXTENSION pg_stat_statements;` |
| MySQL: most sections "not available" | Set `performance_schema = ON` in `my.ini` / `my.cnf` and restart |
| Oracle: unused indexes "not available" | Needs 12.2+ and access to `DBA_INDEX_USAGE`; otherwise use `ALTER INDEX … MONITORING USAGE` |
| Combined run aborted before the browser started | The DB connection test failed — fix it with **Test Connection**, or run *Page Load Analysis* only |
| Combined report says *Pages ↔ Database: Not available* | The reason is shown next to it; run **Run Self-Check** and fix the failing *Per-page query tracking* line |

---

## API (for automation)

| Method | Endpoint | Body / notes |
|---|---|---|
| GET | `/api/db/drivers` | Supported engines and whether their driver is installed |
| POST | `/api/db/test-connection` | `{db_type, server, port, database, schema, auth, username, password}` |
| POST | `/api/db/self-check` | Same body; returns `{success, message, checks: [{name, ok, status, message}]}` |
| POST | `/api/db/start` | Same as above + `interval` (s), `scope` (`window`\|`cumulative`), `outputFolder` |
| POST | `/api/db/stop` | Final scan + report |
| GET | `/api/db/status` | Live state incl. `latest_report_url`, `health_score`, `db_type_label` |
| POST | `/run-test` | Page-load payload + `mode` (`pageload`\|`both`) + `db` object for the combined mode |
