"""
combined_report
---------------
Landing page of a combined "Page Load + DB Scan" run.

Links the page-load reports and the DB Scan report of one run folder and —
when per-page query tracking was available — shows which database work ran
while each page was being tested ("Pages ↔ Database").
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from typing import Optional

SLOW_QUERY_MS = 1000          # avg execution time that flags a query as slow
SLOW_PAGE_MS = 3000           # cold load time that flags a page as slow

_e = html.escape


def _fmt_s(ms) -> str:
    return "—" if ms is None else f"{ms / 1000:.2f} s"


def _fmt_ms(ms) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _fmt_int(n) -> str:
    return "—" if n is None else f"{int(n):,}"


def _link(href: Optional[str], text: str) -> str:
    if not href:
        return '<span class="muted">not generated</span>'
    return f'<a href="{_e(href)}" target="_blank" rel="noopener">{_e(text)}</a>'


def _summary_rows(run_dir: str, files: list[str]) -> list[dict]:
    data_file = next((f for f in files if f.startswith("summary_") and f.endswith("_data.json")), None)
    if not data_file:
        return []
    try:
        with open(os.path.join(run_dir, data_file), encoding="utf-8") as fh:
            return json.load(fh).get("rows", []) or []
    except Exception:                                  # noqa: BLE001
        return []


def _page_cold_ms(page: dict) -> Optional[float]:
    vals = [b.get("no_cache_ms") for b in page.get("browsers") or [] if b.get("no_cache_ms") is not None]
    return max(vals) if vals else None


def _page_warm_ms(page: dict) -> Optional[float]:
    vals = [b.get("with_cache_ms") for b in page.get("browsers") or [] if b.get("with_cache_ms") is not None]
    return max(vals) if vals else None


def _pages_section(pages: Optional[list], db_label: str, tracking_error: Optional[str]) -> str:
    if pages is None:
        reason = _e(tracking_error) if tracking_error else \
            "per-page query tracking was not available for this run"
        return (f'<section class="card"><h2>Pages ↔ Database</h2>'
                f'<p class="muted">Not available — {reason}.</p></section>')
    if not pages:
        return ('<section class="card"><h2>Pages ↔ Database</h2>'
                '<p class="muted">No pages were tested.</p></section>')

    max_db = max(((p.get("db") or {}).get("total_ms") or 0) for p in pages) or 1
    rows = []
    for p in pages:
        db = p.get("db")
        cold, warm = _page_cold_ms(p), _page_warm_ms(p)
        detail_file = next((b.get("detail_file") for b in p.get("browsers") or [] if b.get("detail_file")), None)
        name = _link(detail_file, p.get("name") or p.get("url") or "page") if detail_file else _e(p.get("name") or "")
        flags = []
        if cold is not None and cold >= SLOW_PAGE_MS:
            flags.append('<span class="tag bad">slow page</span>')
        if db and (db.get("slowest_avg_ms") or 0) >= SLOW_QUERY_MS:
            flags.append('<span class="tag bad">slow query</span>')
        if p.get("error"):
            flags.append('<span class="tag warn" title="' + _e(p["error"]) + '">test error</span>')

        if db is None:
            db_cells = '<td class="num muted" colspan="4">snapshot failed</td>'
            detail = ""
        else:
            pct = 100.0 * (db.get("total_ms") or 0) / max_db
            db_cells = (
                f'<td class="num">{_fmt_int(db.get("executions"))}</td>'
                f'<td class="num">{_fmt_ms(db.get("total_ms"))}'
                f'<div class="bar"><span style="width:{pct:.1f}%"></span></div></td>'
                f'<td class="num">{_fmt_ms(db.get("cpu_ms"))}</td>'
                f'<td class="num">{_fmt_ms(db.get("slowest_avg_ms")) if db.get("executions") else "—"}</td>')
            q_rows = []
            for q in db.get("top") or []:
                slow = ' class="slow"' if (q.get("avg_ms") or 0) >= SLOW_QUERY_MS else ""
                q_rows.append(
                    f'<tr{slow}><td class="num">{_fmt_ms(q.get("total_ms"))}</td>'
                    f'<td class="num">{_fmt_int(q.get("executions"))}</td>'
                    f'<td class="num">{_fmt_ms(q.get("avg_ms"))}</td>'
                    f'<td class="num">{_fmt_ms(q.get("cpu_ms"))}</td>'
                    f'<td class="num">{_fmt_int(q.get("logical_reads"))}</td>'
                    f'<td><code>{_e((q.get("query_text") or "")[:1500])}</code></td></tr>')
            detail = ""
            if q_rows:
                detail = (
                    '<tr class="detail"><td></td><td colspan="8"><details><summary>Top '
                    f'{len(q_rows)} statements during this page</summary>'
                    '<div class="scroll"><table class="inner"><thead><tr>'
                    '<th class="num">Total</th><th class="num">Runs</th><th class="num">Avg</th>'
                    '<th class="num">CPU</th><th class="num">Reads</th><th>Statement</th>'
                    '</tr></thead><tbody>' + "".join(q_rows) + '</tbody></table></div>'
                    '</details></td></tr>')
        rows.append(
            f'<tr><td class="num muted">{_e(str(p.get("index") or ""))}</td>'
            f'<td><div class="page">{name}</div><div class="url">{_e(p.get("url") or "")}</div>'
            f'{"".join(flags)}</td>'
            f'<td class="num">{_fmt_s(cold)}</td><td class="num">{_fmt_s(warm)}</td>'
            f'<td class="num">{_e(str(p.get("duration_s") or "—"))} s</td>{db_cells}</tr>{detail}')

    return f"""<section class="card">
 <div class="card-head"><h2>Pages ↔ Database</h2><span class="pill">{_e(db_label)}</span></div>
 <p class="muted small">Database work captured between the start and end of each page's test
 (login, cold load and warm load in every selected browser). Other activity on the same database in that
 time is included too, so test on a quiet environment for the cleanest picture.</p>
 <div class="scroll"><table class="grid">
 <thead><tr><th class="num">#</th><th>Page</th><th class="num">Cold load</th><th class="num">Warm load</th>
 <th class="num">Test time</th><th class="num">DB runs</th><th class="num">DB time</th><th class="num">DB CPU</th>
 <th class="num">Slowest query (avg)</th></tr></thead>
 <tbody>{''.join(rows)}</tbody></table></div>
</section>"""


def build_combined_report(run_dir: str, meta: dict, db_state: dict, page_rc: int,
                          pages: Optional[list] = None, engine_colours: Optional[dict] = None,
                          tracking_error: Optional[str] = None) -> str:
    """Write RapidRadar_Combined_<run>.html into *run_dir* and return its path."""
    os.makedirs(run_dir, exist_ok=True)
    files = os.listdir(run_dir)
    summary = next((f for f in files if f.startswith("summary_") and f.endswith(".html")), None)
    vpat = next((f for f in files if f.startswith("vpat_") and f.endswith(".html")), None)
    db_report = db_state.get("report_path")
    db_rel = os.path.relpath(db_report, run_dir).replace("\\", "/") if db_report else None

    detail = []
    for sub in sorted(files):
        sp = os.path.join(run_dir, sub)
        if os.path.isdir(sp):
            detail += [f"{sub}/{f}" for f in sorted(os.listdir(sp)) if f.endswith(".html")]

    rows = _summary_rows(run_dir, files)
    n_pages = len({r.get("page_url") for r in rows}) if rows else (len(pages) if pages else len(detail))
    loads = [r.get("no_cache_ms") for r in rows if r.get("no_cache_ms") is not None]
    if not loads and pages:
        loads = [v for v in (_page_cold_ms(p) for p in pages) if v is not None]
    avg_ms = sum(loads) / len(loads) if loads else None
    slowest = None
    if rows:
        s = max(rows, key=lambda r: r.get("no_cache_ms") or 0)
        slowest = (s.get("page_name"), s.get("no_cache_ms"))
    elif pages:
        s = max(pages, key=lambda p: _page_cold_ms(p) or 0)
        slowest = (s.get("name"), _page_cold_ms(s))
    db_total = sum(((p.get("db") or {}).get("total_ms") or 0) for p in pages) if pages else None

    score = db_state.get("health_score")
    score_cls = "ok" if (score or 0) >= 80 else "warn" if (score or 0) >= 60 else "bad"
    db_label = db_state.get("db_type_label") or "—"
    colour = (engine_colours or {}).get(db_state.get("db_type"), "#64748b")
    run = meta.get("run_suffix", "")

    detail_html = "".join(f"<li>{_link(d, d)}</li>" for d in detail) or \
        '<li class="muted">No page reports found.</li>'

    out = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RapidRadar Combined Report — {_e(run)}</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#0f172a;--muted:#64748b;--line:#e5e7eb;--accent:#4f46e5;
 --ok:#15803d;--warn:#b45309;--bad:#b91c1c;--soft:#eef2ff;--code:#f8fafc}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0b0f19;--card:#111827;--fg:#e5e7eb;--muted:#94a3b8;
 --line:#1f2937;--accent:#818cf8;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--soft:#1e1b4b;--code:#0f172a}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 "Segoe UI Variable","Segoe UI",system-ui,sans-serif}}
header{{background:var(--card);border-bottom:1px solid var(--line);padding:18px 24px}}
header .wrap,main{{max-width:1200px;margin:0 auto}}
h1{{font-size:20px;margin:0}} h2{{font-size:15px;margin:0 0 10px}}
.sub{{color:var(--muted);font-size:13px}}
main{{padding:20px 16px 40px;display:grid;gap:16px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.kpi .k{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.kpi .nm{{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.kpi .v{{font-size:20px;font-weight:650;margin-top:2px;overflow-wrap:anywhere}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;min-width:0}}
.card-head{{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.pill{{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;color:#fff;background:{_e(colour)}}}
.kv{{width:100%;border-collapse:collapse}} .kv td{{padding:6px 0;border-bottom:1px solid var(--line);vertical-align:top}}
.kv td:first-child{{color:var(--muted);width:42%}}
a{{color:var(--accent);text-decoration:none;font-weight:600}} a:hover{{text-decoration:underline}}
.muted{{color:var(--muted)}} .small{{font-size:12.5px;margin:0 0 10px}}
.ok{{color:var(--ok)}} .warn{{color:var(--warn)}} .bad{{color:var(--bad)}}
.scroll{{overflow-x:auto}}
table.grid{{width:100%;border-collapse:collapse;font-size:13px}}
table.grid th{{text-align:left;color:var(--muted);font-weight:600;font-size:12px;border-bottom:1px solid var(--line);padding:8px 8px;white-space:nowrap}}
table.grid td{{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}}
.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}} th.num{{text-align:right}}
.page{{font-weight:600}} .url{{color:var(--muted);font-size:12px;overflow-wrap:anywhere}}
.tag{{display:inline-block;margin:4px 4px 0 0;font-size:11px;font-weight:600;border-radius:4px;padding:1px 6px;border:1px solid currentColor}}
.bar{{height:4px;background:var(--line);border-radius:2px;margin-top:5px;min-width:70px}}
.bar span{{display:block;height:100%;background:var(--accent);border-radius:2px}}
tr.detail td{{border-bottom:1px solid var(--line);padding-top:0}}
details summary{{cursor:pointer;color:var(--accent);font-size:12.5px;font-weight:600;padding:2px 0 6px}}
table.inner{{width:100%;border-collapse:collapse;font-size:12px;background:var(--code);border-radius:6px}}
table.inner th,table.inner td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}}
table.inner td.num,table.inner th.num{{text-align:right}}
table.inner tr.slow td{{color:var(--bad)}}
code{{font:12px/1.4 Consolas,"Cascadia Mono",monospace;white-space:pre-wrap;word-break:break-word;display:block;min-width:280px;max-height:9em;overflow:auto}}
ul.files{{margin:0;padding-left:18px;max-height:260px;overflow:auto}} ul.files li{{margin:2px 0}}
@media(max-width:820px){{.two{{grid-template-columns:1fr}}}}
</style></head><body>
<header><div class="wrap"><h1>RapidRadar · Combined Report</h1>
<div class="sub">Page Load + DB Scan · {_e(run)} · generated {datetime.now():%Y-%m-%d %H:%M:%S}</div></div></header>
<main>
 <div class="kpis">
  <div class="kpi"><div class="k">Pages tested</div><div class="v">{n_pages}</div></div>
  <div class="kpi"><div class="k">Avg cold load</div><div class="v">{_fmt_s(avg_ms)}</div></div>
  <div class="kpi"><div class="k">Slowest page</div><div class="v">{_fmt_s(slowest[1]) if slowest else '—'}</div><div class="nm" title="{_e(str(slowest[0])) if slowest else ''}">{_e(str(slowest[0])) if slowest else ''}</div></div>
  <div class="kpi"><div class="k">DB time during test</div><div class="v">{_fmt_ms(db_total) if db_total is not None else '—'}</div></div>
  <div class="kpi"><div class="k">DB health</div><div class="v {score_cls}">{score if score is not None else '—'}<span class="muted" style="font-size:13px"> / 100</span></div></div>
 </div>
 {_pages_section(pages, db_label, tracking_error)}
 <div class="two">
  <section class="card"><h2>Page Load Analysis</h2><table class="kv">
   <tr><td>Application</td><td>{_e(meta.get('login_url') or '')}</td></tr>
   <tr><td>EDM version</td><td>{_e(meta.get('edm_version') or '—')}</td></tr>
   <tr><td>Browsers</td><td>{_e(', '.join(meta.get('browsers') or []))}</td></tr>
   <tr><td>Result</td><td>{'<span class="ok">✓ completed</span>' if page_rc == 0 else f'<span class="warn">⚠ exit code {page_rc}</span>'}</td></tr>
   <tr><td>Summary report</td><td>{_link(summary, summary or '')}</td></tr>
   <tr><td>Accessibility (VPAT)</td><td>{_link(vpat, vpat or '')}</td></tr>
  </table></section>
  <section class="card"><div class="card-head"><h2>DB Scan</h2><span class="pill">{_e(db_label)}</span></div><table class="kv">
   <tr><td>Database type tested</td><td>{_e(db_label)}</td></tr>
   <tr><td>Server / database</td><td>{_e(db_state.get('server') or '')} / {_e(db_state.get('database') or '')}</td></tr>
   <tr><td>Analysis window from</td><td>{_e(str(db_state.get('window_start') or '—')[:19])} (server time)</td></tr>
   <tr><td>Scans taken</td><td>{_e(str(db_state.get('scan_count')))}</td></tr>
   <tr><td>DB report</td><td>{_link(db_rel, os.path.basename(db_report) if db_report else '')}</td></tr>
   <tr><td>Errors</td><td>{_e(db_state.get('scan_error') or 'none')}</td></tr>
  </table></section>
 </div>
 <section class="card"><h2>Page detail reports</h2><ul class="files">{detail_html}</ul></section>
</main></body></html>"""
    path = os.path.join(run_dir, f"RapidRadar_Combined_{run or 'run'}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    return path
