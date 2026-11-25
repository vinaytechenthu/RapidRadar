"""
db_scan.report
--------------
Builds a self-contained HTML diagnostic report from the normalised result
structure returned by any ``BaseDBScanner.run_full_scan()``.

The database engine under test (SQL Server / PostgreSQL / Oracle / MySQL) is
shown prominently: page title, navbar badge, the "Database Engine" card and
the report file name.
"""

from __future__ import annotations

import html
from decimal import Decimal
from typing import Any, Optional

from .base import col, to_float

ENGINE_COLOURS = {
    "sqlserver":  "#CC2927",
    "postgresql": "#336791",
    "oracle":     "#C74634",
    "mysql":      "#00758F",
}


def _h(value: Any) -> str:
    if value is None:
        return '<span class="text-muted">—</span>'
    return html.escape(str(value))


def _num(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"
    except (ValueError, TypeError):
        return html.escape(str(value))


class ReportGenerator:

    def __init__(self, results: dict, history: Optional[list[dict]] = None,
                 context: Optional[dict] = None) -> None:
        self.r = results
        self.history = history or []
        self.context = context or {}          # e.g. {"mode": "combined", "page_report": "..."}
        self.db_type = results.get("db_type", "")
        self.db_label = results.get("db_type_label", self.db_type)
        self.health_score: Optional[int] = None
        self.failed_checks = 0

    # ------------------------------------------------------------------ #
    #  Entry point                                                         #
    # ------------------------------------------------------------------ #

    def generate(self) -> str:
        score, issues, warnings, infos = self._health_score()
        self.health_score = score
        parts = [
            self._head(), "<body>", self._navbar(score),
            '<div class="container-fluid px-4 pb-5">',
            self._banner(score, issues, warnings),
            self._summary_cards(),
            self._section_db_info(),
            self._section_timeline(),
            self._section_slow_queries(),
            self._section_object_perf(),
            self._section_throughput(),
            self._section_missing_indexes(),
            self._section_index_frag(),
            self._section_unused_indexes(),
            self._section_iops(),
            self._section_cpu(),
            self._section_connections(),
        ]
        for extra in self.r.get("extra_sections") or []:
            parts.append(self._section(extra["id"], extra["title"], extra.get("icon", "bi-table"),
                                       self._note(extra.get("note")) +
                                       self._render_table(extra.get("result") or {},
                                                          col_format={"fix_script": "code",
                                                                      "blocked_query": "code"})))
        parts += [self._section_recommendations(issues, warnings, infos), "</div>",
                  self._scripts(), "</body></html>"]
        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------ #
    #  Health score                                                        #
    # ------------------------------------------------------------------ #

    def _health_score(self) -> tuple[int, list[str], list[str], list[str]]:
        score, issues, warnings, infos = 100, [], [], []

        sq = self.r.get("slow_queries") or {}
        if sq.get("success") and sq.get("rows"):
            avgs = [to_float(col(sq, r, "avg_elapsed_ms")) for r in sq["rows"]]
            over5 = sum(1 for a in avgs if a > 5000)
            over1 = sum(1 for a in avgs if 1000 < a <= 5000)
            if over5:
                score -= min(25, over5 * 5)
                issues.append(f"{over5} quer{'y' if over5 == 1 else 'ies'} with avg duration > 5 s")
            if over1:
                score -= min(15, over1 * 2)
                warnings.append(f"{over1} quer{'y' if over1 == 1 else 'ies'} with avg duration 1–5 s")

        mi = self.r.get("missing_indexes") or {}
        if mi.get("success") and mi.get("rows"):
            hi = sum(1 for r in mi["rows"] if col(mi, r, "priority") == "HIGH")
            med = sum(1 for r in mi["rows"] if col(mi, r, "priority") == "MEDIUM")
            if hi:
                score -= min(20, hi * 4)
                issues.append(f"{hi} high-priority missing index candidate{'s' if hi > 1 else ''}")
            if med:
                score -= min(10, med * 2)
                warnings.append(f"{med} medium-priority missing index candidate{'s' if med > 1 else ''}")

        cpu = self.r.get("cpu_utilization") or {}
        if cpu.get("success") and cpu.get("rows"):
            recent = [to_float(col(cpu, r, "sql_cpu_pct")) for r in cpu["rows"][:5]]
            avg = sum(recent) / len(recent)
            if avg > 85:
                score -= 20
                issues.append(f"Critical database CPU utilisation — avg {avg:.0f} %")
            elif avg > 70:
                score -= 10
                warnings.append(f"High database CPU utilisation — avg {avg:.0f} %")
            elif avg > 50:
                score -= 5
                warnings.append(f"Elevated database CPU utilisation — avg {avg:.0f} %")

        fr = self.r.get("index_fragmentation") or {}
        if fr.get("success") and fr.get("rows"):
            pcts = [to_float(col(fr, r, "fragmentation_pct")) for r in fr["rows"]]
            heavy = sum(1 for p in pcts if p >= 30)
            mid = sum(1 for p in pcts if 10 <= p < 30)
            if heavy > 5:
                score -= 15
                issues.append(f"{heavy} indexes/tables with ≥ 30 % fragmentation")
            elif heavy:
                score -= heavy * 2
                warnings.append(f"{heavy} index{'es' if heavy > 1 else ''} with ≥ 30 % fragmentation")
            if mid > 10:
                score -= 5
                warnings.append(f"{mid} indexes with 10–30 % fragmentation")

        io = self.r.get("iops") or {}
        if io.get("success") and io.get("rows"):
            hi_rd = sum(1 for r in io["rows"] if to_float(col(io, r, "avg_read_latency_ms")) > 50)
            hi_wr = sum(1 for r in io["rows"] if to_float(col(io, r, "avg_write_latency_ms")) > 30)
            if hi_rd:
                score -= 10
                issues.append(f"{hi_rd} file{'s' if hi_rd > 1 else ''} with read latency > 50 ms")
            if hi_wr:
                score -= 5
                warnings.append(f"{hi_wr} file{'s' if hi_wr > 1 else ''} with write latency > 30 ms")

        ui = self.r.get("unused_indexes") or {}
        if ui.get("success") and len(ui.get("rows") or []) >= 10:
            warnings.append(f"{len(ui['rows'])} unused indexes slowing down writes")

        f_issue = f_warn = 0
        for f in self.r.get("findings") or []:
            sev, msg = f.get("severity"), f.get("message", "")
            if sev == "issue":
                issues.append(msg)
                f_issue += 5
            elif sev == "warning":
                warnings.append(msg)
                f_warn += 2
            else:
                infos.append(msg)
        score -= min(15, f_issue) + min(10, f_warn)

        # A high score is meaningless if most checks could not run (e.g. no privileges).
        core = ("slow_queries", "missing_indexes", "index_fragmentation", "unused_indexes",
                "iops", "throughput", "cpu_utilization")
        self.failed_checks = sum(1 for k in core
                                 if not (self.r.get(k) or {}).get("success")
                                 and not (self.r.get(k) or {}).get("unsupported"))
        return max(0, score), issues, warnings, infos

    # ------------------------------------------------------------------ #
    #  Building blocks                                                     #
    # ------------------------------------------------------------------ #

    def _badge(self, text: str, cls: str) -> str:
        return f'<span class="badge {cls} me-1">{_h(text)}</span>'

    def _count_badge(self, res: dict, warn_at: int = 1) -> str:
        if not res.get("success"):
            return self._badge("N/A", "bg-secondary")
        n = len(res.get("rows") or [])
        if n == 0:
            return self._badge("OK", "bg-success")
        return self._badge(f"{n} found", "bg-warning text-dark" if n <= warn_at else "bg-danger")

    def _section(self, sec_id: str, title: str, icon: str, body: str, badge_html: str = "") -> str:
        return f"""
<div class="card shadow-sm mb-4" id="{sec_id}">
  <div class="card-header d-flex align-items-center justify-content-between py-2">
    <span class="fw-semibold fs-6"><i class="bi {icon} me-2"></i>{_h(title)}</span>
    <span>{badge_html}</span>
  </div>
  <div class="card-body p-0">{body}</div>
</div>"""

    def _note(self, text: Optional[str], kind: str = "info") -> str:
        if not text:
            return ""
        return (f'<div class="small px-3 pt-2 text-{"muted" if kind == "info" else kind}">'
                f'<i class="bi bi-info-circle me-1"></i>{_h(text)}</div>')

    def _section_note(self, key: str) -> str:
        return self._note((self.r.get("notes") or {}).get(key))

    def _render_table(self, result: dict, row_class_fn=None, col_format: Optional[dict] = None,
                      max_rows: int = 100) -> str:
        if not result.get("success"):
            msg = result.get("error") or "Unknown error"
            if result.get("unsupported"):
                return (f'<div class="alert alert-secondary m-3 mb-3 small">'
                        f'<i class="bi bi-slash-circle me-2"></i>Not available for {_h(self.db_label)}: '
                        f'{_h(msg)}</div>')
            return (f'<div class="alert alert-warning m-3"><i class="bi bi-exclamation-triangle me-2"></i>'
                    f'Could not collect data — {_h(msg)}</div>')
        rows = result["rows"][:max_rows]
        cols = result["columns"]
        if not rows:
            return ('<p class="text-muted p-3 mb-0"><i class="bi bi-check-circle text-success me-2">'
                    '</i>No records found.</p>')
        head = "".join(f'<th class="text-nowrap">{_h(c)}</th>' for c in cols)
        body_rows = []
        for row in rows:
            cls = row_class_fn(row) if row_class_fn else ""
            cells = []
            for i, cell in enumerate(row):
                fmt = (col_format or {}).get(cols[i] if i < len(cols) else "")
                if fmt == "code" and cell:
                    cells.append(f'<td><code class="sql-inline">{_h(cell)}</code>'
                                 f'<button class="btn btn-sm btn-outline-secondary ms-1 py-0 copy-btn"'
                                 f' data-code="{html.escape(str(cell), quote=True)}" title="Copy">⎘</button></td>')
                elif fmt == "ms" and cell is not None:
                    v = to_float(cell)
                    c = "text-danger fw-bold" if v > 5000 else "text-warning fw-semibold" if v > 1000 else ""
                    cells.append(f'<td class="{c} text-nowrap">{_num(v)} ms</td>')
                elif fmt == "pct" and cell is not None:
                    v = to_float(cell)
                    c = "text-danger fw-bold" if v >= 30 else "text-warning fw-semibold" if v >= 10 else ""
                    cells.append(f'<td class="{c}">{_num(v)} %</td>')
                elif fmt == "priority":
                    c = {"HIGH": "bg-danger", "MEDIUM": "bg-warning text-dark"}.get(str(cell), "bg-secondary")
                    cells.append(f'<td><span class="badge {c}">{_h(cell)}</span></td>')
                else:
                    cells.append(f"<td>{_h(cell)}</td>")
            body_rows.append(f'<tr class="{cls}">{"".join(cells)}</tr>')
        note = (f'<p class="text-muted small px-3 pt-2 mb-0">Showing first {max_rows} of '
                f'{len(result["rows"])} rows.</p>' if len(result["rows"]) > max_rows else "")
        return f"""{note}
<div class="table-responsive">
  <table class="table table-sm table-hover table-striped mb-0 sortable-table">
    <thead class="table-dark sticky-top"><tr>{head}</tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table>
</div>"""

    # ------------------------------------------------------------------ #
    #  Sections                                                            #
    # ------------------------------------------------------------------ #

    def _section_db_info(self) -> str:
        res = self.r.get("db_info") or {}
        engine_rows = [
            ("Database engine", f'<span class="engine-pill">{_h(self.db_label)}</span>'),
            ("Server version",  _h(self.r.get("server_version"))),
            ("Server / host",   _h(f'{self.r.get("server")}'
                                   + (f':{self.r.get("port")}' if self.r.get("port") else ""))),
            ("Database",        _h(self.r.get("database"))),
            ("Driver",          _h(self.r.get("driver"))),
        ]
        if self.r.get("schema"):
            engine_rows.insert(4, ("Schema", _h(self.r.get("schema"))))
        left = "".join(f'<tr><td class="text-muted w-50">{k}</td><td>{v}</td></tr>'
                       for k, v in engine_rows)
        if res.get("success") and res.get("rows"):
            def fmt(v):
                if isinstance(v, (float, Decimal)):
                    return _num(v, 2)
                return _h(v)
            right = "".join(f'<tr><td class="text-muted w-50">{_h(r[0])}</td><td>{fmt(r[1])}</td></tr>'
                            for r in res["rows"])
            right_html = f'<table class="table table-sm mb-0">{right}</table>'
        else:
            right_html = self._render_table(res)
        body = f"""
<div class="row g-0">
  <div class="col-lg-5 border-end"><table class="table table-sm mb-0">{left}</table></div>
  <div class="col-lg-7">{right_html}</div>
</div>"""
        return self._section("sec-dbinfo", f"Database Information — {self.db_label}",
                             "bi-database-fill-check", body)

    def _section_timeline(self) -> str:
        if len(self.history) < 1:
            return ""
        rows = "".join(
            f"<tr><td>{i}</td><td>{_h(h.get('time'))}</td><td>{_h(h.get('total_sessions'))}</td>"
            f"<td>{_h(h.get('running_sessions'))}</td><td>{_h(h.get('open_txn'))}</td>"
            f"<td>{_h(h.get('cpu_pct'))}</td><td>{_h(h.get('slow_over_1s'))}</td>"
            f"<td>{_h(h.get('max_avg_ms'))}</td><td>{_h(h.get('error'))}</td></tr>"
            for i, h in enumerate(self.history, 1))
        peak = max((to_float(h.get("running_sessions")) for h in self.history), default=0)
        body = f"""
{self._note(f"{len(self.history)} scan(s) taken during the session. Peak active sessions: {peak:.0f}.")}
<div class="table-responsive"><table class="table table-sm table-striped mb-0">
<thead class="table-dark"><tr><th>#</th><th>Scan time</th><th>Sessions</th><th>Active</th>
<th>Open txns</th><th>DB CPU %</th><th>Queries &gt; 1 s</th><th>Slowest avg (ms)</th><th>Error</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""
        return self._section("sec-timeline", "Scan Timeline", "bi-graph-up", body)

    def _section_slow_queries(self) -> str:
        res = self.r.get("slow_queries") or {}

        def row_cls(row):
            ms = to_float(col(res, row, "avg_elapsed_ms"))
            return "table-danger" if ms > 5000 else "table-warning" if ms > 1000 else ""

        body = self._section_note("slow_queries") + self._render_table(
            res, row_class_fn=row_cls,
            col_format={"avg_elapsed_ms": "ms", "avg_cpu_ms": "ms", "query_text": "code"})
        slow = sum(1 for r in res.get("rows") or [] if to_float(col(res, r, "avg_elapsed_ms")) > 1000)
        badge = (self._badge("N/A", "bg-secondary") if not res.get("success") else
                 self._badge(f"{slow} > 1 s", "bg-danger") if slow else self._badge("OK", "bg-success"))
        return self._section("sec-slow", "Slow Queries (top 30 by average duration)",
                             "bi-clock-history", body, badge)

    def _section_object_perf(self) -> str:
        res = self.r.get("object_performance") or {}
        body = self._section_note("object_performance") + self._render_table(
            res, col_format={"avg_elapsed_ms": "ms", "avg_cpu_ms": "ms", "avg_self_ms": "ms"})
        return self._section("sec-objperf", "Stored Procedures / Functions / Views — Performance",
                             "bi-gear-wide-connected", body)

    def _section_throughput(self) -> str:
        return self._section("sec-throughput", "Throughput", "bi-speedometer2",
                             self._section_note("throughput") +
                             self._render_table(self.r.get("throughput") or {}))

    def _section_missing_indexes(self) -> str:
        res = self.r.get("missing_indexes") or {}

        def row_cls(row):
            p = col(res, row, "priority")
            return "table-danger" if p == "HIGH" else "table-warning" if p == "MEDIUM" else ""

        note = """
<div class="alert alert-info m-3 mb-0 small"><i class="bi bi-info-circle me-2"></i>
<strong>How to use:</strong> start with HIGH priority rows. Always validate a new index against the
real workload in a non-production environment first.</div>"""
        body = note + self._section_note("missing_indexes") + self._render_table(
            res, row_class_fn=row_cls,
            col_format={"priority": "priority", "avg_user_impact_pct": "pct",
                        "suggested_index_sql": "code", "suggestion": "code", "query_text": "code",
                        "avg_elapsed_ms": "ms"})
        return self._section("sec-missing", "Missing Indexes", "bi-search", body,
                             self._count_badge(res, 2))

    def _section_index_frag(self) -> str:
        res = self.r.get("index_fragmentation") or {}

        def row_cls(row):
            p = to_float(col(res, row, "fragmentation_pct"))
            return "table-danger" if p >= 30 else "table-warning" if p >= 10 else ""

        body = self._section_note("index_fragmentation") + self._render_table(
            res, row_class_fn=row_cls, col_format={"fragmentation_pct": "pct", "fix_script": "code"})
        return self._section("sec-frag", "Index Fragmentation", "bi-puzzle", body,
                             self._count_badge(res, 3))

    def _section_unused_indexes(self) -> str:
        res = self.r.get("unused_indexes") or {}
        note = """
<div class="alert alert-warning m-3 mb-0 small"><i class="bi bi-exclamation-triangle me-2"></i>
<strong>Caution:</strong> usage statistics reset when the server restarts. Confirm an index is unused
over a full business cycle (month-end, batch jobs) before dropping it, and keep a rollback script.</div>"""
        body = note + self._section_note("unused_indexes") + self._render_table(
            res, col_format={"drop_script": "code"})
        return self._section("sec-unused", "Unused Indexes (candidates for removal)", "bi-trash3",
                             body, self._count_badge(res, 3))

    def _section_iops(self) -> str:
        res = self.r.get("iops") or {}

        def row_cls(row):
            rd = to_float(col(res, row, "avg_read_latency_ms"))
            wr = to_float(col(res, row, "avg_write_latency_ms"))
            if rd > 50 or wr > 30:
                return "table-danger"
            if rd > 20 or wr > 15:
                return "table-warning"
            return ""

        guide = """<div class="p-3 pb-0 small text-muted"><strong>Latency thresholds:</strong>
Read &gt; 50 ms = <span class="text-danger">Critical</span> | Read &gt; 20 ms =
<span class="text-warning">Warning</span> | Write &gt; 30 ms = <span class="text-danger">Critical</span></div>"""
        body = guide + self._section_note("iops") + self._render_table(
            res, row_class_fn=row_cls,
            col_format={"avg_read_latency_ms": "ms", "avg_write_latency_ms": "ms"})
        return self._section("sec-iops", "I/O Statistics (IOPS & latency)", "bi-hdd-stack", body)

    def _section_cpu(self) -> str:
        res = self.r.get("cpu_utilization") or {}
        if not res.get("success"):
            return self._section("sec-cpu", "CPU Utilisation", "bi-cpu", self._render_table(res))
        rows = res.get("rows") or []
        if not rows:
            return self._section("sec-cpu", "CPU Utilisation", "bi-cpu",
                                 '<p class="text-muted p-3 mb-0">No CPU history data available.</p>')
        chart = ""
        for row in rows[:20]:
            db_pct = to_float(col(res, row, "sql_cpu_pct"))
            oth = to_float(col(res, row, "other_cpu_pct"))
            idle = to_float(col(res, row, "system_idle_pct"))
            total = db_pct + oth
            cls = "bg-danger" if total > 85 else "bg-warning" if total > 70 else "bg-success"
            chart += f"""
<tr><td class="text-nowrap small" style="width:170px">{_h(str(col(res, row, "sample_time"))[:19])}</td>
<td style="width:100%"><div class="cpu-bar">
  <div class="cpu-seg bg-primary" style="width:{db_pct:.0f}%" title="{_h(self.db_label)} {db_pct:.0f}%"></div>
  <div class="cpu-seg bg-secondary" style="width:{oth:.0f}%" title="Other {oth:.0f}%"></div></div></td>
<td class="text-nowrap small"><span class="badge {cls}">{total:.0f} % used</span></td>
<td class="text-nowrap small text-muted">DB: {db_pct:.0f}%&nbsp; Idle: {idle:.0f}%</td></tr>"""
        legend = (f'<div class="p-3 pb-1 d-flex gap-3 small"><span><span class="badge bg-primary">&nbsp;'
                  f'</span> {_h(self.db_label)} CPU</span><span><span class="badge bg-secondary">&nbsp;'
                  f'</span> Other processes</span></div>')
        body = (legend + f'<div class="table-responsive p-3"><table class="table table-sm mb-0">'
                         f'<tbody>{chart}</tbody></table></div>')
        return self._section("sec-cpu", f"CPU Utilisation (last {min(20, len(rows))} samples)",
                             "bi-cpu", body)

    def _section_connections(self) -> str:
        conn = self.r.get("connections") or {}
        summ, det = conn.get("summary") or {}, conn.get("details") or {}
        summary_html = ""
        if summ.get("success") and summ.get("rows"):
            row = summ["rows"][0]

            def card(label, key, extra="", fmt=None):
                v = col(summ, row, key)
                val = _num(v, 0) if fmt == "num" else _h(v)
                return (f'<div class="col-md-2 col-6"><div class="stat-card {extra}">'
                        f'<div class="stat-label">{label}</div><div class="stat-value">{val}</div></div></div>')
            summary_html = (f'<div class="row g-3 p-3">'
                            f'{card("Total Sessions", "total_sessions")}'
                            f'{card("Active / Running", "running_sessions", "bg-success-subtle")}'
                            f'{card("Idle / Sleeping", "sleeping_sessions")}'
                            f'{card("Background", "background_sessions")}'
                            f'{card("Open Txns", "sessions_with_open_txn", "bg-warning-subtle")}'
                            f'{card("Memory (KB)", "total_memory_kb", "", "num")}</div>')
        elif summ:
            summary_html = self._render_table(summ)
        body = summary_html + self._render_table(det, col_format={"current_query": "code"})
        return self._section("sec-conn", "Active Connections & Sessions", "bi-people-fill", body)

    def _section_recommendations(self, issues, warnings, infos) -> str:
        items = "".join(f'<li class="list-group-item list-group-item-danger"><i class="bi bi-x-circle me-2">'
                        f'</i>{_h(i)}</li>' for i in issues)
        items += "".join(f'<li class="list-group-item list-group-item-warning"><i class="bi '
                         f'bi-exclamation-triangle me-2"></i>{_h(w)}</li>' for w in warnings)
        items += "".join(f'<li class="list-group-item list-group-item-info"><i class="bi bi-info-circle '
                         f'me-2"></i>{_h(n)}</li>' for n in infos)
        if not items:
            items = ('<li class="list-group-item list-group-item-success"><i class="bi bi-check-circle '
                     'me-2"></i>No significant issues detected.</li>')
        return self._section("sec-recs", "Prioritised Recommendations", "bi-clipboard2-check",
                             f'<ul class="list-group list-group-flush">{items}</ul>')

    # ------------------------------------------------------------------ #
    #  Page chrome                                                         #
    # ------------------------------------------------------------------ #

    def _engine_badge(self) -> str:
        colour = ENGINE_COLOURS.get(self.db_type, "#6c757d")
        return (f'<span class="badge fs-6 px-3" style="background:{colour}">'
                f'<i class="bi bi-database me-1"></i>{_h(self.db_label)}</span>')

    def _navbar(self, score: int) -> str:
        score_cls = "bg-success" if score >= 80 else "bg-warning text-dark" if score >= 60 else "bg-danger"
        links = [("sec-dbinfo", "DB Info"), ("sec-timeline", "Timeline"), ("sec-slow", "Slow Queries"),
                 ("sec-objperf", "Objects"), ("sec-throughput", "Throughput"),
                 ("sec-missing", "Missing Indexes"), ("sec-frag", "Fragmentation"),
                 ("sec-unused", "Unused Indexes"), ("sec-iops", "I/O"), ("sec-cpu", "CPU"),
                 ("sec-conn", "Connections")]
        if not self.history:
            links = [l for l in links if l[0] != "sec-timeline"]
        links += [(e["id"], e["title"].split(" — ")[0].split(" (")[0])
                  for e in self.r.get("extra_sections") or []]
        links.append(("sec-recs", "Recommendations"))
        nav = "".join(f'<a class="nav-link" href="#{i}">{_h(t)}</a>' for i, t in links)
        return f"""
<nav class="navbar navbar-dark bg-dark sticky-top px-4 py-2">
  <span class="navbar-brand fw-bold"><i class="bi bi-radar me-2"></i>RapidRadar DBScan</span>
  <div class="d-flex align-items-center gap-3 text-white small flex-wrap">
    {self._engine_badge()}
    <span><i class="bi bi-server me-1"></i>{_h(self.r.get("server"))}</span>
    <span><i class="bi bi-database me-1"></i>{_h(self.r.get("database"))}</span>
    <span><i class="bi bi-clock me-1"></i>{_h(self.r.get("scan_time"))}</span>
    <span class="badge {score_cls} fs-6 px-3">Health: {score}/100</span>
  </div>
</nav>
<nav class="navbar navbar-light bg-light px-4 py-1 small border-bottom flex-wrap gap-2">{nav}</nav>"""

    def _banner(self, score: int, issues: list, warnings: list) -> str:
        cls = "success" if score >= 80 else "warning" if score >= 60 else "danger"
        label = ("Healthy" if score >= 80 else "Needs Attention" if score >= 60 else
                 "Warning" if score >= 40 else "Critical")
        if getattr(self, "failed_checks", 0) >= 3:
            cls = "secondary"
            label = f"Incomplete data — {self.failed_checks} checks failed"
        issue_html = "".join(f'<span class="badge bg-danger me-1 mb-1 text-wrap text-start">{_h(i)}</span>'
                             for i in issues)
        warn_html = "".join(f'<span class="badge bg-warning text-dark me-1 mb-1 text-wrap text-start">'
                            f'{_h(w)}</span>' for w in warnings)
        if self.r.get("scope") == "window":
            scope = (f'<i class="bi bi-stopwatch me-1"></i><strong>Test window:</strong> DB activity since '
                     f'{_h(self.r.get("window_start"))} (server time)')
            if self.context.get("mode") == "combined":
                scope += " — captured while RapidRadar Page Load Analysis was driving the UI."
        else:
            scope = ('<i class="bi bi-collection me-1"></i><strong>Cumulative:</strong> statistics since '
                     'they were last reset / server restart')
        colour = ENGINE_COLOURS.get(self.db_type, "#6c757d")
        return f"""
<div class="row g-3 my-3">
  <div class="col-md-3">
    <div class="card h-100 border-{cls}"><div class="card-body text-center">
      <div class="display-4 fw-bold text-{cls}">{score}</div>
      <div class="text-muted">Health Score / 100</div>
      <div class="progress mt-2" style="height:12px"><div class="progress-bar bg-{cls}" style="width:{score}%"></div></div>
      <div class="mt-1 fw-semibold text-{cls}">{label}</div>
    </div></div>
  </div>
  <div class="col-md-3">
    <div class="card h-100" style="border-color:{colour}"><div class="card-body">
      <div class="stat-label mb-1">Database Type Tested</div>
      <div class="fs-4 fw-bold" style="color:{colour}"><i class="bi bi-database me-1"></i>{_h(self.db_label)}</div>
      <div class="small text-muted mt-1">{_h(self.r.get("server_version"))}</div>
      <div class="small mt-2">{_h(self.r.get("server"))} / <strong>{_h(self.r.get("database"))}</strong></div>
    </div></div>
  </div>
  <div class="col-md-6">
    <div class="card h-100"><div class="card-header fw-semibold">Key Findings</div>
      <div class="card-body">
        <div class="small mb-2">{scope}</div>
        <div class="mb-1">{issue_html or '<span class="text-success">No critical issues.</span>'}</div>
        <div>{warn_html or '<span class="text-muted small">No warnings.</span>'}</div>
      </div></div>
  </div>
</div>"""

    def _summary_cards(self) -> str:
        def count(key, pred=None):
            res = self.r.get(key) or {}
            if not res.get("success"):
                return "N/A"
            rows = res.get("rows") or []
            return sum(1 for r in rows if pred(res, r)) if pred else len(rows)

        n_slow = count("slow_queries", lambda res, r: to_float(col(res, r, "avg_elapsed_ms")) > 1000)
        n_miss = count("missing_indexes")
        n_frag = count("index_fragmentation", lambda res, r: to_float(col(res, r, "fragmentation_pct")) >= 10)
        n_unused = count("unused_indexes")
        cpu = self.r.get("cpu_utilization") or {}
        avg_cpu = "N/A"
        if cpu.get("success") and cpu.get("rows"):
            vals = [to_float(col(cpu, r, "sql_cpu_pct")) for r in cpu["rows"][:5]]
            avg_cpu = f"{sum(vals) / len(vals):.0f} %"

        def colour(v, warn, crit):
            if not isinstance(v, int):
                return "secondary"
            return "danger" if v > crit else "warning" if v > warn else "success"

        def card(title, value, icon, c):
            return f"""
<div class="col-md col-sm-4 col-6"><div class="card text-center border-{c}"><div class="card-body py-2">
  <div class="fs-4 text-{c}"><i class="bi {icon}"></i></div>
  <div class="display-6 fw-bold">{_h(value)}</div>
  <div class="small text-muted">{title}</div></div></div></div>"""

        return f"""
<div class="row g-3 mb-4">
  {card("Slow Queries (&gt; 1 s)", n_slow, "bi-clock-history", colour(n_slow, 0, 5))}
  {card("Missing Index Candidates", n_miss, "bi-search", colour(n_miss, 0, 5))}
  {card("Fragmented (≥ 10 %)", n_frag, "bi-puzzle", colour(n_frag, 0, 10))}
  {card("Unused Indexes", n_unused, "bi-trash3", "secondary")}
  {card("Avg DB CPU", avg_cpu, "bi-cpu", "primary")}
</div>"""

    def _head(self) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DBScan Report — {_h(self.db_label)} — {_h(self.r.get("database"))} on {_h(self.r.get("server"))}</title>
<meta name="rapidradar-db-type" content="{_h(self.db_type)}">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
<style>
  body {{ background:#f5f7fa; font-size:.875rem; }}
  .stat-card {{ background:#fff; border:1px solid #dee2e6; border-radius:.5rem; padding:.75rem 1rem;
               text-align:center; height:100%; }}
  .stat-label {{ font-size:.7rem; text-transform:uppercase; color:#6c757d; letter-spacing:.04em; }}
  .stat-value {{ font-size:1.4rem; font-weight:700; }}
  .engine-pill {{ background:{ENGINE_COLOURS.get(self.db_type, "#6c757d")}; color:#fff; border-radius:1rem;
                 padding:1px 10px; font-weight:600; }}
  code.sql-inline {{ font-size:.78rem; white-space:pre-wrap; word-break:break-all; background:#f8f9fa;
                     padding:2px 4px; border-radius:3px; display:inline-block; min-width:320px;
                     max-width:900px; }}
  .cpu-bar {{ display:flex; height:16px; border-radius:4px; overflow:hidden; background:#e9ecef; width:100%; }}
  .cpu-seg {{ height:100%; }}
  .sortable-table th {{ cursor:pointer; user-select:none; }}
  .sortable-table th:hover {{ background:#495057 !important; }}
  .copy-btn {{ font-size:.7rem; padding:0 4px; line-height:1.4; }}
  @media print {{ nav {{ display:none; }} .card {{ break-inside:avoid; }} }}
</style>
</head>"""

    def _scripts(self) -> str:
        return """
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
document.querySelectorAll('.sortable-table').forEach(tbl => {
  tbl.querySelectorAll('thead th').forEach((th, idx) => {
    th.dataset.dir = '1';
    th.addEventListener('click', () => {
      const dir = parseInt(th.dataset.dir), tbody = tbl.querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((a, b) => {
        const av = a.cells[idx]?.innerText.trim() ?? '', bv = b.cells[idx]?.innerText.trim() ?? '';
        const an = parseFloat(av.replace(/[^0-9.\\-]/g, '')), bn = parseFloat(bv.replace(/[^0-9.\\-]/g, ''));
        if (!isNaN(an) && !isNaN(bn)) return dir * (an - bn);
        return dir * av.localeCompare(bv);
      });
      rows.forEach(r => tbody.appendChild(r));
      th.dataset.dir = String(-dir);
    });
  });
});
document.querySelectorAll('.copy-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    navigator.clipboard?.writeText(btn.dataset.code).then(() => {
      btn.textContent = '✓'; setTimeout(() => btn.textContent = '⎘', 1500);
    });
  });
});
</script>"""
