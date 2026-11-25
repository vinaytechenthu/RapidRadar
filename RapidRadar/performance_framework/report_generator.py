"""
performance_framework/report_generator.py
==========================================
Generates a self-contained, single-file HTML performance report.

Architecture
------------
* All page data is serialised as a JSON object injected into a <script> tag.
* Client-side JavaScript reads the JSON and renders every section dynamically.
* Chart.js (CDN) renders the metric charts.
* No external CSS framework – all styles are inlined.
"""

import json
import os
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def generate_report(data: Dict[str, Any], output_file: str) -> None:
    """Write the full per-page HTML report to *output_file*."""
    report_json: str = json.dumps(data, default=str, ensure_ascii=False)

    html: str = (
        _HTML_TEMPLATE
        .replace("__REPORT_DATA_PLACEHOLDER__", report_json)
    )

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html)


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def analyze_issues(no_cache: Dict[str, Any], slow_api_threshold: int = 2000) -> List[Dict[str, str]]:
    """
    Mirror the JavaScript analyzeIssues() logic from the detail report template
    and return a list of dicts {sev, icon, cat, title} describing each issue.

    sev values: 'critical' | 'high' | 'medium' | 'low'
    """
    issues: List[Dict[str, str]] = []
    timing = no_cache.get("timing", {})

    def issue(sev: str, icon: str, cat: str, title: str) -> None:
        issues.append({"sev": sev, "icon": icon, "cat": cat, "title": title})

    # TTFB
    ttfb = timing.get("ttfb")
    if ttfb:
        if ttfb > 1000:
            issue("critical", "🔴", "Server", f"High TTFB: {ttfb:.0f} ms")
        elif ttfb > 600:
            issue("high", "🟠", "Server", f"Elevated TTFB: {ttfb:.0f} ms")

    # Slow APIs
    slow_apis = no_cache.get("slow_apis", [])
    if slow_apis:
        sev = "critical" if len(slow_apis) >= 3 else "high"
        issue(sev, "⏳", "API", f"{len(slow_apis)} API call(s) > {slow_api_threshold} ms")

    # FCP
    fcp = timing.get("fcp")
    if fcp and fcp > 3000:
        issue("high", "🎨", "Rendering", f"Slow FCP: {fcp:.0f} ms")

    # LCP
    lcp = timing.get("lcp")
    if lcp and lcp > 4000:
        issue("high", "🖼", "Rendering", f"Slow LCP: {lcp:.0f} ms")

    # CLS
    cls = timing.get("cls")
    if cls is not None and float(cls) > 0.25:
        issue("medium", "📐", "Stability", f"High CLS: {float(cls):.3f}")

    # Large resources (>500 KB)
    large_resources = no_cache.get("large_resources", [])
    if large_resources:
        largest = large_resources[0]
        issue("medium", "📦", "Resource Size",
              f"{len(large_resources)} resource(s) >500 KB (largest: {_fmt_bytes(largest.get('size', 0))})")

    # Slow resources (>1 s)
    slow_resources = no_cache.get("slow_resources", [])
    if slow_resources:
        issue("medium", "🐌", "Resource Loading", f"{len(slow_resources)} resource(s) >1 s to load")

    # Render-blocking
    render_blocking = no_cache.get("render_blocking", [])
    if render_blocking:
        issue("medium", "🚧", "Render Blocking",
              f"{len(render_blocking)} render-blocking script(s)/stylesheet(s)")

    # Too many requests
    total_requests = no_cache.get("total_requests", 0)
    if total_requests > 150:
        issue("medium", "🌐", "Network", f"High request count: {total_requests}")

    # Large page weight (>5 MB)
    total_size = no_cache.get("total_size", 0)
    if total_size > 5 * 1024 * 1024:
        issue("medium", "⚖️", "Page Weight", f"Page weight > 5 MB: {_fmt_bytes(total_size)}")

    return issues


def compute_performance_score(no_cache: Dict[str, Any]) -> int:
    """
    Compute a 0–100 Performance Score for a page based on its no-cache run.

    Weights
    -------
    Total Load Time  30 %
    LCP              20 %
    Slow API count   10 %
    TTFB             10 %
    FCP              10 %
    CLS              10 %
    Page Size        10 %

    Grade bands:  A 90-100 · B 75-89 · C 50-74 · D 25-49 · F 0-24
    """
    timing = no_cache.get("timing", {})

    def _safe(val: Any, default: float = 0.0) -> float:
        try:
            return float(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    lt    = _safe(timing.get("total_load_time"))
    s_lt  = (100 if lt <= 1000 else 90 if lt <= 2000 else 75 if lt <= 3000
             else 50 if lt <= 5000 else 25 if lt <= 8000 else 10)

    ttfb   = _safe(timing.get("ttfb"))
    s_ttfb = 100 if ttfb <= 200 else 80 if ttfb <= 600 else 50 if ttfb <= 1000 else 20

    fcp_raw = timing.get("fcp")
    fcp     = _safe(fcp_raw)
    s_fcp   = 80 if fcp_raw is None else (100 if fcp <= 1800 else 70 if fcp <= 3000 else 30)

    lcp_raw = timing.get("lcp")
    lcp     = _safe(lcp_raw)
    s_lcp   = 80 if lcp_raw is None else (100 if lcp <= 2500 else 70 if lcp <= 4000 else 20)

    cls_raw = timing.get("cls")
    cls_val = _safe(cls_raw)
    s_cls   = 100 if cls_val <= 0.1 else 70 if cls_val <= 0.25 else 30

    slow_count = len(no_cache.get("slow_apis", []))
    s_api      = 100 if slow_count == 0 else 70 if slow_count <= 2 else 40 if slow_count <= 5 else 10

    page_size  = no_cache.get("total_size", 0) or 0
    s_size     = (100 if page_size <= 1*1024*1024 else 80 if page_size <= 3*1024*1024
                  else 60 if page_size <= 5*1024*1024 else 30 if page_size <= 10*1024*1024 else 10)

    score = round(
        s_lt * 0.30 + s_lcp * 0.20 + s_api * 0.10
        + s_ttfb * 0.10 + s_fcp * 0.10 + s_cls * 0.10 + s_size * 0.10
    )
    return max(0, min(100, score))


def merge_summary_data(
    data_file: str,
    new_rows: List[Dict[str, Any]],
    login_time_ms: Optional[int],
    timestamp: str,
) -> Dict[str, Any]:
    """
    Load the JSON data file produced by a previous ``generate_summary_report``
    call and merge *new_rows* into it.

    Rows that share the same ``(page_url, browser)`` key are **replaced** by
    the new result; genuinely new pages are **appended**.  Returns the merged
    data dict ready to be passed straight to ``generate_summary_report``.
    """
    with open(data_file, "r", encoding="utf-8") as fh:
        existing: Dict[str, Any] = json.load(fh)

    # O(1) lookup: (page_url, browser) → index in existing["rows"]
    row_map: Dict[tuple, int] = {
        (r["page_url"], r.get("browser", "")): i
        for i, r in enumerate(existing["rows"])
    }
    for new_row in new_rows:
        key = (new_row["page_url"], new_row.get("browser", ""))
        if key in row_map:
            existing["rows"][row_map[key]] = new_row  # update in-place
        else:
            existing["rows"].append(new_row)          # add new page

    existing["timestamp"] = timestamp
    if login_time_ms is not None:
        existing["login_time_ms"] = login_time_ms

    return existing


def generate_summary_report(
    login_url: str,
    login_time_ms: Optional[int],
    timestamp: str,
    rows: List[Dict[str, Any]],
    output_file: str,
    process_stats: Optional[Dict[str, Any]] = None,
    edm_version: str = "",
) -> None:
    """
    Write a parent summary HTML report that lists every tested page with
    its key metrics and a link to the individual detailed report.

    Each *row* dict must have:
        page_url      str   – the tested page URL
        page_name     str   – filesystem-safe name (used for the detail link)
        browser       str   – display name of the browser
        no_cache_ms   float – cold-load total ms
        with_cache_ms float – warm-load total ms
        slow_api_count int  – number of slow API calls (no-cache run)
        issues        list  – list of issue dicts (optional)
        detail_file   str   – relative path to the detail HTML report

    A companion ``<output_file_base>_data.json`` is saved alongside the HTML
    so future CSV-merge runs can load and update the data without re-running
    all pages.
    """
    data: Dict[str, Any] = {
        "login_url": login_url,
        "login_time_ms": login_time_ms,
        "timestamp": timestamp,
        "edm_version": edm_version,
        "rows": rows,
        "process_stats": process_stats,
    }
    summary_json: str = json.dumps(data, default=str, ensure_ascii=False)

    html = _SUMMARY_TEMPLATE.replace("__SUMMARY_DATA_PLACEHOLDER__", summary_json)

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html)

    # Persist raw data so CSV-merge runs can update specific pages later
    data_file = output_file.replace(".html", "_data.json")
    with open(data_file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, default=str, indent=2)


# ---------------------------------------------------------------------------
# Summary report template
# ---------------------------------------------------------------------------

_SUMMARY_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>RapidRadar – Summary Report</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+CiAgPCEtLSBEYXJrIGJhY2tncm91bmQgLS0+CiAgPGNpcmNsZSBjeD0iMTYiIGN5PSIxNiIgcj0iMTYiIGZpbGw9IiMwZDExMTciLz4KICA8IS0tIFJhZGFyIHJpbmdzIC0tPgogIDxjaXJjbGUgY3g9IjE2IiBjeT0iMTYiIHI9IjEyIiBmaWxsPSJub25lIiBzdHJva2U9IiM1OGE2ZmYiIHN0cm9rZS13aWR0aD0iMS41IiBvcGFjaXR5PSIwLjM1Ii8+CiAgPGNpcmNsZSBjeD0iMTYiIGN5PSIxNiIgcj0iNyIgIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzU4YTZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIG9wYWNpdHk9IjAuNiIvPgogIDwhLS0gQ2VudHJlIGRvdCAtLT4KICA8Y2lyY2xlIGN4PSIxNiIgY3k9IjE2IiByPSIyLjIiIGZpbGw9IiM1OGE2ZmYiLz4KICA8IS0tIFN3ZWVwIGFybSAtLT4KICA8bGluZSB4MT0iMTYiIHkxPSIxNiIgeDI9IjI3IiB5Mj0iNyIgc3Ryb2tlPSIjM2ZiOTUwIiBzdHJva2Utd2lkdGg9IjEuOCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPCEtLSBCbGlwIG9uIHN3ZWVwIC0tPgogIDxjaXJjbGUgY3g9IjIzLjUiIGN5PSI5LjUiIHI9IjIuOCIgZmlsbD0iIzNmYjk1MCIgb3BhY2l0eT0iMC45Ii8+CiAgPCEtLSBGYWludCBlY2hvIGJsaXAgLS0+CiAgPGNpcmNsZSBjeD0iMTEiIGN5PSIyMiIgcj0iMS44IiBmaWxsPSIjNThhNmZmIiBvcGFjaXR5PSIwLjQ1Ii8+Cjwvc3ZnPg==">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#0d1117;--bg1:#161b22;--bg2:#1c2128;--bg3:#262c36;
  --border:#30363d;
  --txt1:#e6edf3;--txt2:#8b949e;--txt3:#6e7681;
  --blue:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;
  --orange:#e3b341;--purple:#bc8cff;
  --radius:8px;
}
[data-theme="light"]{
  --bg0:#f6f8fa;--bg1:#ffffff;--bg2:#f0f2f5;--bg3:#e8ecf0;
  --border:#d0d7de;--txt1:#1f2328;--txt2:#656d76;--txt3:#9198a1;
  --blue:#0969da;--green:#1a7f37;--yellow:#9a6700;--red:#cf222e;
  --orange:#bc4c00;
}
body{background:var(--bg0);color:var(--txt1);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;line-height:1.6}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:1400px;margin:0 auto;padding:24px 20px}

/* Header */
.report-header{background:linear-gradient(135deg,#0d1b2e 0%,#1a2742 100%);border-bottom:1px solid var(--border);padding:28px 0}
[data-theme="light"] .report-header{background:linear-gradient(135deg,#dce8f5 0%,#b8d0eb 100%)!important}
.report-header .container{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px}
.header-left h1{font-size:26px;font-weight:700;color:var(--txt1);display:flex;align-items:center;gap:10px}
.header-sub{margin-top:6px;font-size:12px;font-weight:700;color:#58a6ff;letter-spacing:.04em}
.header-meta{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--txt2);text-align:right}
.header-meta span{display:flex;align-items:center;gap:6px;justify-content:flex-end}
.header-meta .label{color:var(--txt3)}
.theme-btn{background:var(--bg2);border:1px solid var(--border);color:var(--txt1);border-radius:20px;padding:5px 14px;font-size:13px;cursor:pointer;transition:all .15s}
.theme-btn:hover{background:var(--bg3);border-color:var(--blue);color:var(--blue)}

/* Login card */
.login-card-row{display:flex;gap:16px;margin:28px 0 24px;flex-wrap:wrap}
.login-card{background:var(--bg1);border:1px solid var(--border);border-radius:var(--radius);padding:18px 24px;min-width:180px;flex:0 0 auto}
.login-card .lc-label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt2);margin-bottom:4px}
.login-card .lc-value{font-size:24px;font-weight:700}
.login-card .lc-sub{font-size:11px;color:var(--txt3);margin-top:2px}
.good{color:var(--green)}.medium{color:var(--yellow)}.poor{color:var(--red)}.neutral{color:var(--txt1)}

/* Table */
.section{background:var(--bg1);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:32px}
.section-title{padding:14px 20px;font-size:15px;font-weight:600;border-bottom:1px solid var(--border);background:var(--bg2)}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{background:var(--bg2);padding:10px 14px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt2);border-bottom:1px solid var(--border);white-space:nowrap}
tbody tr:hover td{background:var(--bg2)}
tbody td{padding:11px 14px;border-bottom:1px solid var(--border);vertical-align:middle;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
.sno{color:var(--txt3);font-size:12px;width:42px}
.page-col{min-width:200px;white-space:normal}
.page-name{font-weight:600;color:var(--txt1);white-space:nowrap}
.page-url-small{font-size:11px;color:var(--txt3);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:320px}
.ms{font-family:ui-monospace,'Cascadia Code',Consolas,monospace;font-size:13px}
.badge-cnt{display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600;background:var(--bg3);border:1px solid var(--border)}
.badge-cnt.zero{color:var(--green)}
.badge-cnt.low{color:var(--yellow)}
.badge-cnt.high{color:var(--red)}
.issues-col{white-space:normal;min-width:160px}
.issues-text{font-size:12px;color:var(--txt2);line-height:1.8}
.issues-text .issue-item{display:inline-block;border-radius:4px;padding:2px 8px;margin:2px 3px 2px 0;font-size:11px;border:1px solid;white-space:nowrap}
.issues-text .issue-item.critical{background:rgba(248,81,73,.12);color:#f85149;border-color:rgba(248,81,73,.25)}
.issues-text .issue-item.high{background:rgba(227,179,65,.12);color:var(--orange);border-color:rgba(227,179,65,.25)}
.issues-text .issue-item.medium{background:rgba(210,153,34,.12);color:var(--yellow);border-color:rgba(210,153,34,.25)}
.issues-text .issue-item.low{background:rgba(88,166,255,.10);color:var(--blue);border-color:rgba(88,166,255,.25)}
.issues-text .issue-item.more{background:var(--bg3);color:var(--txt2);border-color:var(--border)}
.detail-btn{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;text-decoration:none;transition:transform .15s,box-shadow .15s;white-space:nowrap}
.detail-btn:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(102,126,234,.4);color:#fff;text-decoration:none}
.na{color:var(--txt3);font-style:italic}
thead th.sortable{cursor:pointer;user-select:none;position:relative;padding-right:26px;transition:color .15s,background .15s}
thead th.sortable:hover{color:var(--txt1);background:var(--bg3)}
thead th.sortable::after{content:'⇅';position:absolute;right:8px;top:50%;transform:translateY(-50%);opacity:.35;font-size:11px}
thead th.sort-asc::after{content:'↑';opacity:1;color:var(--blue)}
thead th.sort-desc::after{content:'↓';opacity:1;color:var(--blue)}
thead th.sort-asc,thead th.sort-desc{color:var(--blue)}

/* Footer */
.report-footer{text-align:center;padding:20px;font-size:12px;color:var(--txt3);border-top:1px solid var(--border);margin-top:16px}

/* Process stats section */
.proc-section{margin:0 0 24px}
.proc-section-title{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--txt2);margin-bottom:10px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:8px;user-select:none}
.proc-section-title:hover{color:var(--txt1)}
.proc-chevron{font-size:10px;transition:transform .2s}
.proc-card-row{display:flex;gap:16px;flex-wrap:wrap}
.proc-card{background:var(--bg1);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;flex:1 1 220px;min-width:200px}
.proc-card-title{font-size:13px;font-weight:700;color:var(--txt1);margin-bottom:10px;border-bottom:1px solid var(--border);padding-bottom:8px}
.proc-card-title .proc-icon{margin-right:6px}
.proc-metric{display:flex;justify-content:space-between;align-items:baseline;font-size:12px;margin-bottom:5px}
.proc-metric .pm-label{color:var(--txt2)}
.proc-metric .pm-val{font-weight:600;color:var(--txt1)}
.proc-metric .pm-val.good{color:var(--good)}
.proc-metric .pm-val.medium{color:var(--medium)}
.proc-metric .pm-val.poor{color:var(--poor)}
.proc-metric .pm-val.na{color:var(--txt3)}
.proc-not-found{font-size:12px;color:var(--txt3);font-style:italic}
.proc-pid{font-size:11px;color:var(--txt3);margin-bottom:8px}
/* ── Performance Score badge ──────────────────────────────── */
.sum-score-badge{display:inline-flex;flex-direction:column;align-items:center;padding:6px 14px;border-radius:8px;border:1px solid;min-width:76px;cursor:default}
.sum-score-num{font-size:22px;font-weight:700;line-height:1.1}
.sum-score-grade{font-size:12px;font-weight:700;line-height:1.3}
.sum-score-label{font-size:10px;color:var(--txt3);margin-top:1px}
/* ── Overall Scorecard ──────────────────────────────────────────── */
.sc-wrap{display:flex;gap:32px;align-items:flex-start;flex-wrap:wrap;padding:20px}
.sc-gauge-col{display:flex;flex-direction:column;align-items:center;min-width:180px}
.sc-gauge-meta{text-align:center;margin-top:12px}
.sc-gauge-title{font-size:15px;font-weight:700;color:var(--txt1)}
.sc-gauge-sub{font-size:11px;color:var(--txt3);margin-top:3px}
.sc-right{flex:1;min-width:280px;display:flex;flex-direction:column;gap:24px}
.sc-dist-title,.sc-pages-title{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt2);font-weight:600;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.sc-grade-row{display:grid;grid-template-columns:22px 1fr 36px 28px;align-items:center;gap:8px;margin-bottom:7px;font-size:12px}
.sc-grade-lbl{font-weight:700;font-size:13px}
.sc-grade-bar-wrap{height:8px;background:var(--bg3);border-radius:4px;overflow:hidden}
.sc-grade-bar{height:100%;border-radius:4px}
.sc-grade-cnt{font-weight:600;text-align:right}
.sc-grade-pct{color:var(--txt3);font-size:11px}
.sc-pages-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sc-page-item{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;gap:8px}
.sc-page-name{font-size:12px;font-weight:500;color:var(--txt1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px}
.sc-page-score{font-size:15px;font-weight:700}
/* ── Accessibility badge (summary) ─────────────────────────────────── */
.a11y-badge-sm{display:inline-flex;flex-direction:column;align-items:center;padding:4px 10px;border-radius:6px;border:1px solid;min-width:60px;cursor:default}
.a11y-badge-num{font-size:18px;font-weight:700;line-height:1.1}
.a11y-badge-lbl{font-size:10px;font-weight:600;line-height:1.3}
.a11y-badge-sub{font-size:9px;color:var(--txt3);margin-top:1px}
.a11y-viol-cnt{font-size:10px;margin-top:2px;font-weight:600}
/* ── VPAT section ───────────────────────────────────────────────────── */
.vpat-section{background:var(--bg1);border:1px solid var(--border);border-left:4px solid #58a6ff;border-radius:var(--radius);padding:20px 24px;margin:0 0 24px;display:flex;align-items:center;gap:24px;flex-wrap:wrap}
.vpat-icon{font-size:32px;flex-shrink:0}
.vpat-info{flex:1;min-width:220px}
.vpat-title{font-size:15px;font-weight:700;color:var(--txt1);margin-bottom:4px}
.vpat-sub{font-size:12px;color:var(--txt2)}
.vpat-stats{display:flex;gap:16px;margin-top:10px;flex-wrap:wrap}
.vpat-stat{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:6px 14px;text-align:center}
.vpat-stat-val{font-size:18px;font-weight:700;color:var(--txt1)}
.vpat-stat-lbl{font-size:10px;color:var(--txt3);text-transform:uppercase;letter-spacing:.4px}
.vpat-actions{display:flex;flex-direction:column;gap:8px;min-width:180px;align-items:stretch}
.vpat-open-btn{display:flex;align-items:center;justify-content:center;gap:8px;padding:10px 20px;background:linear-gradient(135deg,#1f6feb,#388bfd);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;transition:opacity .2s}
.vpat-open-btn:hover{opacity:.85}
.vpat-regen-btn{display:flex;align-items:center;justify-content:center;gap:6px;padding:7px 14px;background:var(--bg2);color:var(--txt2);border:1px solid var(--border);border-radius:8px;font-size:12px;font-weight:500;cursor:pointer;transition:color .2s,background .2s}
.vpat-regen-btn:hover{background:var(--bg3);color:var(--txt1)}
.vpat-regen-btn:disabled{opacity:.5;cursor:not-allowed}
/* ── Rows without accessibility data ────────────────────────────────── */
tbody tr.no-a11y-row td{background:rgba(210,153,34,.06)}
tbody tr.no-a11y-row:hover td{background:rgba(210,153,34,.12)}
tbody tr.no-a11y-row td:first-child{border-left:3px solid var(--yellow)}
.a11y-missing-pill{display:inline-flex;align-items:center;gap:5px;font-size:10px;font-weight:600;color:var(--yellow);background:rgba(210,153,34,.14);border:1px solid rgba(210,153,34,.4);border-radius:5px;padding:3px 8px;white-space:nowrap;cursor:default}
</style>
</head>
<body>
<header class="report-header">
  <div class="container">
    <div class="header-left">
      <h1>🚀 RapidRadar</h1>
      <div class="header-sub">Created by OPUS EDM PERF TEAM</div>
    </div>
    <div style="display:flex;align-items:flex-start;gap:14px">
      <button class="theme-btn" onclick="toggleTheme()">☀️ Day</button>
      <button class="theme-btn" id="vpat-btn" onclick="openOrGenerateVpat()" title="Open or Generate VPAT 2.4 Accessibility Conformance Report">♿ VPAT</button>
      <div class="header-meta" id="hdr-meta"></div>
    </div>
  </div>
</header>

<div class="container">
  <!-- Login card row -->
  <div class="login-card-row" id="login-cards"></div>

  <!-- Process stats cards -->
  <div class="proc-section" id="proc-section" style="display:none">
    <div class="proc-section-title" onclick="toggleProcSection()">⚙️ System Process Stats (during test) <span class="proc-chevron" id="proc-chevron">▼</span></div>
    <div class="proc-card-row" id="proc-cards"></div>
  </div>

  <!-- Overall Performance Scorecard -->
  <div class="proc-section" id="scorecard-section" style="margin-bottom:24px">
    <div class="proc-section-title" onclick="toggleScorecard()" style="cursor:pointer">&#x1F4CA; Overall Application Performance Scorecard <span class="proc-chevron" id="scorecard-chevron" style="transform:rotate(-90deg)">&#x25BC;</span></div>
    <div id="overall-scorecard" style="display:none"></div>
  </div>

  <!-- Overall Accessibility Scorecard -->
  <div class="proc-section" id="a11y-scorecard-section" style="margin-bottom:24px;display:none">
    <div class="proc-section-title" onclick="toggleA11yScorecard()" style="cursor:pointer">&#x267F; Overall Application Accessibility Scorecard <span class="proc-chevron" id="a11y-scorecard-chevron" style="transform:rotate(-90deg)">&#x25BC;</span></div>
    <div id="overall-a11y-scorecard" style="display:none"></div>
  </div>

  <!-- VPAT Report Section -->
  <div class="vpat-section" id="vpat-section">
    <div class="vpat-icon">&#x267F;</div>
    <div class="vpat-info">
      <div class="vpat-title">VPAT 2.4 &mdash; Accessibility Conformance Report (WCAG 2.1 AA)</div>
      <div class="vpat-sub">Voluntary Product Accessibility Template &bull; Generated by RapidRadar using axe-core automated scan</div>
      <div class="vpat-stats" id="vpat-stats"></div>
    </div>
    <div class="vpat-actions">
      <a id="vpat-open-link" class="vpat-open-btn" href="#" target="_blank">&#x1F4C4; Open VPAT Report</a>
      <button id="vpat-regen-btn" class="vpat-regen-btn" onclick="regenVpat()" style="display:none">&#x1F504; Regenerate VPAT</button>
      <div id="vpat-status" style="font-size:11px;color:var(--txt3);text-align:center"></div>
    </div>
  </div>

  <!-- Pages table -->
  <div class="section">
    <div class="section-title">📋 Pages Performance Summary</div>
    <div class="table-wrap">
      <table id="summary-table">
        <thead>
          <tr>
            <th class="sno">#</th>
            <th class="sortable" data-col="page_name" data-type="text">Page</th>
            <th class="sortable" data-col="perf_score" data-type="num">Perf Score</th>
            <th class="sortable" data-col="a11y_score" data-type="num">Accessibility</th>
            <th class="sortable" data-col="no_cache_ms" data-type="num">No-Cache</th>
            <th class="sortable" data-col="with_cache_ms" data-type="num">With-Cache</th>
            <th class="sortable" data-col="issue_count" data-type="num">Performance Issues</th>
            <th class="sortable" data-col="slow_api_count" data-type="num">Slow APIs</th>
            <th>Detailed Report</th>
          </tr>
        </thead>
        <tbody id="pages-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<footer class="report-footer">
  <strong>RapidRadar</strong> &bull; OPUS EDM PERF TEAM &bull; <span id="footer-ts"></span>
</footer>

<script>
const D = __SUMMARY_DATA_PLACEHOLDER__;

function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme');
  var next = cur === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next === 'dark' ? '' : 'light');
  document.querySelector('.theme-btn').textContent = next === 'light' ? '🌙 Night' : '☀️ Day';
}

function fms(ms) {
  if (ms == null || isNaN(ms)) return '<span class="na">N/A</span>';
  var n = Math.round(ms);
  var cls = n <= 2000 ? 'good' : n <= 5000 ? 'medium' : 'poor';
  return '<span class="ms ' + cls + '">' + n + ' ms</span>';
}

function loginFms(ms) {
  if (!ms) return '<span class="na">—</span>';
  var cls = ms < 3000 ? 'good' : ms < 8000 ? 'medium' : 'poor';
  return '<span class="' + cls + '">' + ms + ' ms</span>';
}

function pct(nc, wc) {
  if (!nc || !wc || nc <= 0) return '<span class="na">—</span>';
  var g = ((nc - wc) / nc * 100);
  if (g <= 0) return '<span class="medium">0%</span>';
  var cls = g > 30 ? 'good' : g > 10 ? 'medium' : 'neutral';
  return '<span class="' + cls + '">' + g.toFixed(1) + '% faster</span>';
}

function slowBadge(n) {
  var cls = n === 0 ? 'zero' : n < 3 ? 'low' : 'high';
  return '<span class="badge-cnt ' + cls + '">' + n + '</span>';
}

function issuesSummary(row) {
  var issues = row.issues || [];
  if (issues.length === 0)
    return '<span style="color:var(--green);font-size:12px">✓ No issues</span>';
  var sevOrder = { critical:0, high:1, medium:2, low:3 };
  var sorted = issues.slice().sort(function(a,b){
    return (sevOrder[a.sev]||9) - (sevOrder[b.sev]||9);
  });
  var parts = sorted.slice(0, 4).map(function(iss) {
    var sev = (iss.sev || 'medium').toLowerCase();
    var label = (iss.icon ? iss.icon + ' ' : '') + (iss.title || iss.cat || '');
    if (label.length > 48) label = label.substring(0, 46) + '…';
    return '<span class="issue-item ' + sev + '">' + label + '</span>';
  });
  if (issues.length > 4)
    parts.push('<span class="issue-item more">+' + (issues.length - 4) + ' more</span>');
  return '<div class="issues-text">' + parts.join('') + '</div>';
}

function truncUrl(u, n) {
  if (!u) return '';
  if (u.length <= n) return u;
  return u.substring(0, n) + '…';
}

// ── Performance Score helpers ──────────────────────────────────────
function computePageScore(r) {
  if (r.perf_score != null) return r.perf_score;
  var lt   = r.no_cache_ms || 0;
  var slow = r.slow_api_count || 0;
  var sLT  = lt<=1000?100:lt<=2000?90:lt<=3000?75:lt<=5000?50:lt<=8000?25:10;
  var sAPI = slow===0?100:slow<=2?70:slow<=5?40:10;
  // TTFB/FCP/LCP/page-size not in summary data – assume fast (100) to match detail page score
  return Math.min(100, Math.max(0, Math.round(sLT*0.30 + 100*0.20 + sAPI*0.10 + 100*0.10 + 100*0.10 + 100*0.10 + 100*0.10)));
}
function scoreGrade(s) {
  if (s>=90) return {grade:'A',label:'Excellent',  color:'var(--green)',  bg:'rgba(63,185,80,.12)',   border:'rgba(63,185,80,.35)'};
  if (s>=75) return {grade:'B',label:'Good',       color:'#4ec9b0',       bg:'rgba(78,201,176,.12)',  border:'rgba(78,201,176,.35)'};
  if (s>=50) return {grade:'C',label:'Needs Work', color:'var(--yellow)', bg:'rgba(210,153,34,.12)',  border:'rgba(210,153,34,.35)'};
  if (s>=25) return {grade:'D',label:'Poor',       color:'var(--orange)', bg:'rgba(227,179,65,.12)',  border:'rgba(227,179,65,.35)'};
  return        {grade:'F',label:'Critical',  color:'var(--red)',    bg:'rgba(248,81,73,.12)',    border:'rgba(248,81,73,.35)'};
}
function scoreBadge(s) {
  if (s == null) return '<span style="color:var(--txt3)">—</span>';
  var G = scoreGrade(s);
  return '<span class="sum-score-badge" style="background:' + G.bg + ';border-color:' + G.border + '">' +
    '<span class="sum-score-num" style="color:' + G.color + '">' + s + '</span>' +
    '<span class="sum-score-grade" style="color:' + G.color + '">' + G.grade + '</span>' +
  '</span>';
}
function a11yBadge(s, violations) {
  if (s == null) return '<span style="color:var(--txt3);font-size:11px">—</span>';
  var G = scoreGrade(s);
  var vLabel = violations != null ? violations + ' violation' + (violations !== 1 ? 's' : '') : '';
  return '<span class="a11y-badge-sm" style="background:' + G.bg + ';border-color:' + G.border + '">' +
    '<span class="a11y-badge-num" style="color:' + G.color + '">' + s + '</span>' +
    '<span class="a11y-badge-lbl" style="color:' + G.color + '">' + G.grade + '</span>' +
    (vLabel ? '<span class="a11y-viol-cnt" style="color:' + G.color + '">' + vLabel + '</span>' : '') +
  '</span>';
}
function renderOverallScorecard() {
  var _scOpen = false;
  window.toggleScorecard = function() {
    _scOpen = !_scOpen;
    document.getElementById('overall-scorecard').style.display = _scOpen ? '' : 'none';
    document.getElementById('scorecard-chevron').style.transform = _scOpen ? '' : 'rotate(-90deg)';
  };
  var scores = D.rows.map(function(r) { return { name: (r.page_name||'').replace(/_/g,' '), score: computePageScore(r), url: r.detail_file||'' }; });
  var avg = Math.round(scores.reduce(function(s,r){return s+r.score;},0) / scores.length);
  var G = scoreGrade(avg);
  var circ = 282.74;
  var arc  = (avg / 100) * circ;
  var dist = {A:0,B:0,C:0,D:0,F:0};
  scores.forEach(function(r) { var g = scoreGrade(r.score).grade; dist[g] = (dist[g]||0) + 1; });
  var gradeColors = {A:'var(--green)',B:'#4ec9b0',C:'var(--yellow)',D:'var(--orange)',F:'var(--red)'};
  var distHtml = ['A','B','C','D','F'].map(function(g) {
    var cnt = dist[g]||0;
    var pct = scores.length ? Math.round(cnt/scores.length*100) : 0;
    var w   = scores.length ? (cnt/scores.length*100) : 0;
    return '<div class="sc-grade-row">' +
      '<span class="sc-grade-lbl" style="color:' + gradeColors[g] + '">' + g + '</span>' +
      '<div class="sc-grade-bar-wrap"><div class="sc-grade-bar" style="width:' + w.toFixed(1) + '%;background:' + gradeColors[g] + '"></div></div>' +
      '<span class="sc-grade-cnt" style="color:' + gradeColors[g] + '">' + cnt + '</span>' +
      '<span class="sc-grade-pct">' + pct + '%</span>' +
    '</div>';
  }).join('');
  var sorted = scores.slice().sort(function(a,b){return b.score-a.score;});
  var best  = sorted.slice(0, 3);
  var worst = sorted.slice(-3).reverse();
  function pageItem(r) {
    var G2 = scoreGrade(r.score);
    var link = r.url ? '<a href="' + r.url + '" target="_blank" class="sc-page-name" title="' + r.name + '">' + r.name + '</a>'
                     : '<span class="sc-page-name" title="' + r.name + '">' + r.name + '</span>';
    return '<div class="sc-page-item">' + link +
      '<span class="sc-page-score" style="color:' + G2.color + '">' + r.score + ' <span style="font-size:10px;font-weight:600">' + G2.grade + '</span></span>' +
    '</div>';
  }
  document.getElementById('overall-scorecard').innerHTML =
    '<div class="sc-wrap">' +
      '<div class="sc-gauge-col">' +
        '<svg width="180" height="180" viewBox="0 0 110 110" style="display:block">' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="var(--bg3)" stroke-width="10"/>' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="' + G.color + '" stroke-width="10"' +
          ' stroke-dasharray="' + arc.toFixed(2) + ' ' + circ.toFixed(2) + '"' +
          ' stroke-linecap="round" transform="rotate(-90 55 55)"/>' +
          '<text x="55" y="46" text-anchor="middle" font-size="24" font-weight="700" fill="' + G.color + '">' + avg + '</text>' +
          '<text x="55" y="62" text-anchor="middle" font-size="13" font-weight="700" fill="' + G.color + '">' + G.grade + '</text>' +
          '<text x="55" y="75" text-anchor="middle" font-size="9" fill="var(--txt2)">' + G.label + '</text>' +
          '<text x="55" y="88" text-anchor="middle" font-size="8" fill="var(--txt3)">' + scores.length + ' pages</text>' +
        '</svg>' +
        '<div class="sc-gauge-meta">' +
          '<div class="sc-gauge-title">Overall Score</div>' +
          '<div class="sc-gauge-sub">Average across all pages</div>' +
        '</div>' +
      '</div>' +
      '<div class="sc-right">' +
        '<div>' +
          '<div class="sc-dist-title">Grade Distribution</div>' +
          distHtml +
        '</div>' +
        '<div>' +
          '<div class="sc-pages-title">&#x1F3C6; Best Performing Pages</div>' +
          '<div class="sc-pages-grid">' + best.map(pageItem).join('') + '</div>' +
        '</div>' +
        '<div>' +
          '<div class="sc-pages-title">&#x26A0;&#xFE0F; Needs Attention</div>' +
          '<div class="sc-pages-grid">' + worst.map(pageItem).join('') + '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
}

// ── Accessibility Scorecard ───────────────────────────────────────
function renderOverallA11yScorecard() {
  var rows = D.rows.filter(function(r){ return r.a11y_score != null; });
  if (!rows.length) return;
  document.getElementById('a11y-scorecard-section').style.display = '';
  var _scOpen = false;
  window.toggleA11yScorecard = function() {
    _scOpen = !_scOpen;
    document.getElementById('overall-a11y-scorecard').style.display = _scOpen ? '' : 'none';
    document.getElementById('a11y-scorecard-chevron').style.transform = _scOpen ? '' : 'rotate(-90deg)';
  };
  var scores = rows.map(function(r) {
    return { name: (r.page_name||'').replace(/_/g,' '), score: r.a11y_score, violations: r.a11y_violations||0, url: r.detail_file||'' };
  });
  var avg = Math.round(scores.reduce(function(s,r){return s+r.score;},0) / scores.length);
  var G = scoreGrade(avg);
  var circ = 282.74;
  var arc  = (avg / 100) * circ;
  var dist = {A:0,B:0,C:0,D:0,F:0};
  scores.forEach(function(r) { var g = scoreGrade(r.score).grade; dist[g] = (dist[g]||0) + 1; });
  var gradeColors = {A:'var(--green)',B:'#4ec9b0',C:'var(--yellow)',D:'var(--orange)',F:'var(--red)'};
  var distHtml = ['A','B','C','D','F'].map(function(g) {
    var cnt = dist[g]||0;
    var pct2 = scores.length ? Math.round(cnt/scores.length*100) : 0;
    var w   = scores.length ? (cnt/scores.length*100) : 0;
    return '<div class="sc-grade-row">' +
      '<span class="sc-grade-lbl" style="color:' + gradeColors[g] + '">' + g + '</span>' +
      '<div class="sc-grade-bar-wrap"><div class="sc-grade-bar" style="width:' + w.toFixed(1) + '%;background:' + gradeColors[g] + '"></div></div>' +
      '<span class="sc-grade-cnt" style="color:' + gradeColors[g] + '">' + cnt + '</span>' +
      '<span class="sc-grade-pct">' + pct2 + '%</span>' +
    '</div>';
  }).join('');
  var totalViol = scores.reduce(function(a,r){return a+r.violations;},0);
  var sorted = scores.slice().sort(function(a,b){return b.score-a.score;});
  var best  = sorted.slice(0, 3);
  var worst = sorted.slice(-3).reverse();
  function a11yPageItem(r) {
    var G2 = scoreGrade(r.score);
    var link = r.url ? '<a href="' + r.url + '" target="_blank" class="sc-page-name" title="' + r.name + '">' + r.name + '</a>'
                     : '<span class="sc-page-name" title="' + r.name + '">' + r.name + '</span>';
    return '<div class="sc-page-item">' + link +
      '<span class="sc-page-score" style="color:' + G2.color + '">' + r.score + ' <span style="font-size:10px;font-weight:600">' + G2.grade + '</span></span>' +
    '</div>';
  }
  document.getElementById('overall-a11y-scorecard').innerHTML =
    '<div class="sc-wrap">' +
      '<div class="sc-gauge-col">' +
        '<svg width="180" height="180" viewBox="0 0 110 110" style="display:block">' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="var(--bg3)" stroke-width="10"/>' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="' + G.color + '" stroke-width="10"' +
          ' stroke-dasharray="' + arc.toFixed(2) + ' ' + circ.toFixed(2) + '"' +
          ' stroke-linecap="round" transform="rotate(-90 55 55)"/>' +
          '<text x="55" y="46" text-anchor="middle" font-size="24" font-weight="700" fill="' + G.color + '">' + avg + '</text>' +
          '<text x="55" y="62" text-anchor="middle" font-size="13" font-weight="700" fill="' + G.color + '">' + G.grade + '</text>' +
          '<text x="55" y="75" text-anchor="middle" font-size="9" fill="var(--txt2)">' + G.label + '</text>' +
          '<text x="55" y="88" text-anchor="middle" font-size="8" fill="var(--txt3)">' + scores.length + ' pages</text>' +
        '</svg>' +
        '<div class="sc-gauge-meta">' +
          '<div class="sc-gauge-title">Overall Accessibility Score</div>' +
          '<div class="sc-gauge-sub">' + totalViol + ' total violation' + (totalViol!==1?'s':'') + ' &bull; WCAG 2.1 AA</div>' +
        '</div>' +
      '</div>' +
      '<div class="sc-right">' +
        '<div>' +
          '<div class="sc-dist-title">Grade Distribution</div>' +
          distHtml +
        '</div>' +
        '<div>' +
          '<div class="sc-pages-title">&#x2705; Most Accessible Pages</div>' +
          '<div class="sc-pages-grid">' + best.map(a11yPageItem).join('') + '</div>' +
        '</div>' +
        '<div>' +
          '<div class="sc-pages-title">&#x26A0;&#xFE0F; Accessibility Needs Attention</div>' +
          '<div class="sc-pages-grid">' + worst.map(a11yPageItem).join('') + '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
}

// ── Header ─────────────────────────────────────────────────────────
document.getElementById('hdr-meta').innerHTML =
  '<span><span class="label">Website</span><a href="' + D.login_url + '">' + D.login_url + '</a></span>' +
  '<span><span class="label">Timestamp</span>' + D.timestamp + '</span>';
document.getElementById('footer-ts').textContent = D.timestamp;

// ── VPAT helpers ────────────────────────────────────────────────────
function _vpatFolder() {
  // Works for both file:// and http:// URLs
  var href = window.location.href;
  var m = href.match(/reports[\/\\]([^\/\\?#]+)[\/\\]/);
  if (m) return m[1];
  var parts = href.replace(/\\/g, '/').split('/');
  var rIdx = parts.lastIndexOf('reports');
  return rIdx >= 0 ? parts[rIdx + 1] : '';
}
function _vpatFilename(folder) {
  return 'vpat_' + folder + '.html';
}
function _vpatRelPath() {
  var folder = _vpatFolder();
  return folder ? _vpatFilename(folder) : '';
}

(function initVpat() {
  // Populate VPAT stats card
  var rows = D.rows || [];
  var a11yRows = rows.filter(function(r){ return r.a11y_score != null; });
  var statsEl = document.getElementById('vpat-stats');
  if (a11yRows.length) {
    var avgScore = Math.round(a11yRows.reduce(function(s,r){return s+r.a11y_score;},0)/a11yRows.length);
    var totalViol = a11yRows.reduce(function(s,r){return s+(r.a11y_violations||0);},0);
    var G = scoreGrade(avgScore);
    statsEl.innerHTML =
      '<div class="vpat-stat"><div class="vpat-stat-val" style="color:'+G.color+'">'+avgScore+'</div><div class="vpat-stat-lbl">Avg A11y Score</div></div>' +
      '<div class="vpat-stat"><div class="vpat-stat-val">'+totalViol+'</div><div class="vpat-stat-lbl">Total Violations</div></div>' +
      '<div class="vpat-stat"><div class="vpat-stat-val">'+a11yRows.length+'</div><div class="vpat-stat-lbl">Pages Scanned</div></div>';
  }

  // Wire the Open button to the pre-generated VPAT file (relative path – works offline)
  var rel = _vpatRelPath();
  var openLink = document.getElementById('vpat-open-link');
  var statusEl = document.getElementById('vpat-status');
  if (rel) {
    openLink.href = rel;
    // Check if file is accessible (http mode) or just set href (file mode)
    if (window.location.protocol !== 'file:') {
      fetch(rel, {method:'HEAD', cache:'no-store'})
        .then(function(r){
          if (!r.ok) { openLink.textContent = '\u26A0\uFE0F VPAT Not Found – Generate Below'; openLink.style.background = '#21262d'; openLink.style.color = 'var(--txt2)'; }
        }).catch(function(){});
    }
  } else {
    openLink.textContent = '\u26A0\uFE0F Open VPAT (run from reports folder)';
    openLink.style.pointerEvents = 'none'; openLink.style.opacity = '.5';
  }

  // Wire header button
  var hdrBtn = document.getElementById('vpat-btn');
  hdrBtn.onclick = function(){ if (rel) window.open(rel, '_blank'); };

  // Check if server is available → show Regenerate button
  fetch('http://127.0.0.1:5000/status', {cache:'no-store'})
    .then(function(r){
      if (r.ok) {
        document.getElementById('vpat-regen-btn').style.display = '';
        statusEl.textContent = 'RapidRadar server connected';
      }
    }).catch(function(){ statusEl.textContent = 'Start RapidRadar to regenerate'; });
})();

function regenVpat() {
  var folder = _vpatFolder();
  if (!folder) { alert('Could not determine report folder from URL.'); return; }
  var btn = document.getElementById('vpat-regen-btn');
  var openLink = document.getElementById('vpat-open-link');
  btn.disabled = true; btn.textContent = '\u23F3 Generating\u2026';
  fetch('http://127.0.0.1:5000/generate-vpat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({folder: folder})
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    btn.disabled = false; btn.textContent = '\u2705 Done \u2013 Open';
    if (d.error) { alert('Error: ' + d.error); btn.textContent = '\u1F504 Regenerate VPAT'; return; }
    var vpatUrl = 'http://127.0.0.1:5000/' + d.vpat_file;
    openLink.href = vpatUrl;
    window.open(vpatUrl, '_blank');
    setTimeout(function(){ btn.textContent = '\u1F504 Regenerate VPAT'; }, 3000);
  })
  .catch(function(e){ btn.disabled = false; btn.textContent = '\u1F504 Regenerate VPAT'; alert('Request failed: ' + e); });
}
function openOrGenerateVpat() {
  var rel = _vpatRelPath();
  if (rel) window.open(rel, '_blank');
}

// ── Login card ─────────────────────────────────────────────────────
var loginCls = !D.login_time_ms ? 'neutral' : D.login_time_ms < 3000 ? 'good' : D.login_time_ms < 8000 ? 'medium' : 'poor';
document.getElementById('login-cards').innerHTML =
  (D.edm_version ? '<div class="login-card" style="border-color:var(--border);background:var(--bg1)">' +
    '<div class="lc-label">&#x1F4CB; EDM Version</div>' +
    '<div class="lc-value neutral">' + D.edm_version + '</div>' +
    '<div class="lc-sub">Application version under test</div>' +
  '</div>' : '') +
  '<div class="login-card">' +
    '<div class="lc-label">🔐 Login Time</div>' +
    '<div class="lc-value ' + loginCls + '">' + loginFms(D.login_time_ms) + '</div>' +
    '<div class="lc-sub">Time to reach post-login page</div>' +
  '</div>' +
  '<div class="login-card">' +
    '<div class="lc-label">📄 Pages Tested</div>' +
    '<div class="lc-value neutral">' + D.rows.length + '</div>' +
    '<div class="lc-sub">Auto-discovered &amp; measured</div>' +
  '</div>' +
  (function() {
    var slowTotal = D.rows.reduce(function(s, r) { return s + (r.slow_api_count || 0); }, 0);
    var scls = slowTotal === 0 ? 'good' : slowTotal < 5 ? 'medium' : 'poor';
    return '<div class="login-card">' +
      '<div class="lc-label">🔴 Total Slow APIs</div>' +
      '<div class="lc-value ' + scls + '">' + slowTotal + '</div>' +
      '<div class="lc-sub">Across all pages (no-cache)</div>' +
    '</div>';
  })() +
  '<div class="login-card">' +
    '<div class="lc-label">⏱ Avg Load Time</div>' +
    '<div class="lc-value ' + (function() {
      var vals = D.rows.filter(function(r){return r.no_cache_ms>0;}).map(function(r){return r.no_cache_ms;});
      if (!vals.length) return 'neutral';
      var avg = vals.reduce(function(a,b){return a+b;},0)/vals.length;
      return avg<=2000?'good':avg<=5000?'medium':'poor';
    })() + '">' +
    (function() {
      var vals = D.rows.filter(function(r){return r.no_cache_ms>0;}).map(function(r){return r.no_cache_ms;});
      if (!vals.length) return '<span class="na">—</span>';
      return Math.round(vals.reduce(function(a,b){return a+b;},0)/vals.length) + ' ms';
    })() +
    '</div><div class="lc-sub">No-cache average</div></div>' +
  (function() {
    var scores = D.rows.map(function(r) { return computePageScore(r); });
    var avg = scores.length ? Math.round(scores.reduce(function(a,b){return a+b;},0)/scores.length) : null;
    var G = avg != null ? scoreGrade(avg) : null;
    return '<div class="login-card" style="border-color:' + (G ? G.border : 'var(--border)') + ';background:' + (G ? G.bg : 'var(--bg1)') + '">' +
      '<div class="lc-label">&#x1F4CA; Avg Perf Score</div>' +
      '<div class="lc-value" style="color:' + (G ? G.color : 'var(--txt1)') + '">' +
        (avg != null ? avg + ' <span style="font-size:16px;font-weight:700">' + G.grade + '</span>' : '\u2014') +
      '</div>' +
      '<div class="lc-sub">' + (G ? G.label : '') + ' \u2022 all pages avg</div>' +
    '</div>';
  })() +
  (function() {
    var rows = D.rows.filter(function(r){ return r.a11y_score != null; });
    if (!rows.length) return '';
    var avg = Math.round(rows.reduce(function(a,r){return a+r.a11y_score;},0)/rows.length);
    var G = scoreGrade(avg);
    var totalViol = rows.reduce(function(a,r){return a+(r.a11y_violations||0);},0);
    return '<div class="login-card" style="border-color:' + G.border + ';background:' + G.bg + '">' +
      '<div class="lc-label">&#x267F; Avg Accessibility Score</div>' +
      '<div class="lc-value" style="color:' + G.color + '">' +
        avg + ' <span style="font-size:16px;font-weight:700">' + G.grade + '</span>' +
      '</div>' +
      '<div class="lc-sub">' + G.label + ' \u2022 ' + totalViol + ' total violation' + (totalViol!==1?'s':'') + '</div>' +
    '</div>';
  })();

// ── Process stats cards ────────────────────────────────────────────
(function() {
  var PS = D.process_stats;
  if (!PS) return;
  var anyFound = ['edm','sql','browser'].some(function(k){ return PS[k] && PS[k].found; });
  if (!anyFound) return;
  document.getElementById('proc-section').style.display = '';
  // start collapsed
  document.getElementById('proc-cards').style.display = 'none';
  document.getElementById('proc-chevron').style.transform = 'rotate(-90deg)';
  var _procOpen = false;
  window.toggleProcSection = function() {
    _procOpen = !_procOpen;
    document.getElementById('proc-cards').style.display = _procOpen ? '' : 'none';
    document.getElementById('proc-chevron').style.transform = _procOpen ? '' : 'rotate(-90deg)';
  };

  var icons = { edm: '🖥️', sql: '🗄️', browser: '🌐' };
  var html = '';

  ['edm','sql','browser'].forEach(function(key) {
    var p = PS[key];
    if (!p) return;
    html += '<div class="proc-card">';
    html += '<div class="proc-card-title"><span class="proc-icon">' + (icons[key]||'⚙️') + '</span>' + (p.label || key) + '</div>';

    if (!p.found) {
      html += '<div class="proc-not-found">Process not detected</div>';
    } else {
      html += '<div class="proc-pid">PID ' + p.pid + ' &bull; ' + p.name + '</div>';

      // CPU avg
      var cpuAvgCls = p.cpu_avg == null ? 'na' : p.cpu_avg < 30 ? 'good' : p.cpu_avg < 70 ? 'medium' : 'poor';
      html += '<div class="proc-metric">' +
        '<span class="pm-label">CPU Avg</span>' +
        '<span class="pm-val ' + cpuAvgCls + '">' + (p.cpu_avg != null ? p.cpu_avg + ' %' : '—') + '</span>' +
        '</div>';

      // CPU peak
      var cpuMaxCls = p.cpu_max == null ? 'na' : p.cpu_max < 50 ? 'good' : p.cpu_max < 85 ? 'medium' : 'poor';
      html += '<div class="proc-metric">' +
        '<span class="pm-label">CPU Peak</span>' +
        '<span class="pm-val ' + cpuMaxCls + '">' + (p.cpu_max != null ? p.cpu_max + ' %' : '—') + '</span>' +
        '</div>';

      // Memory current
      html += '<div class="proc-metric">' +
        '<span class="pm-label">Memory</span>' +
        '<span class="pm-val">' + (p.mem_mb != null ? p.mem_mb.toFixed(0) + ' MB' : '—') + '</span>' +
        '</div>';

      // Memory peak
      html += '<div class="proc-metric">' +
        '<span class="pm-label">Mem Peak</span>' +
        '<span class="pm-val">' + (p.mem_peak_mb != null ? p.mem_peak_mb.toFixed(0) + ' MB' : '—') + '</span>' +
        '</div>';
    }
    html += '</div>';
  });

  document.getElementById('proc-cards').innerHTML = html;
})();

// ── Sorting state ──────────────────────────────────────────────────
var _sortCol = null;
var _sortDir = 1;  // 1 = asc, -1 = desc

function _sortVal(row, col) {
  if (col === 'page_name')   return (row.page_name || '').toLowerCase();
  if (col === 'browser')     return (row.browser || '').toLowerCase();
  if (col === 'no_cache_ms') return row.no_cache_ms || 0;
  if (col === 'with_cache_ms') return row.with_cache_ms || 0;
  if (col === 'cache_gain') {
    var nc = row.no_cache_ms, wc = row.with_cache_ms;
    return (nc > 0 && wc > 0) ? (nc - wc) / nc * 100 : -Infinity;
  }
  if (col === 'slow_api_count') return row.slow_api_count || 0;
  if (col === 'issue_count') {
    // Sort by worst severity first: critical=4, high=3, medium=2, low=1, none=0
    var sevScore = { critical:4, high:3, medium:2, low:1 };
    var issues = row.issues || [];
    if (!issues.length) return 0;
    var maxSev = Math.max.apply(null, issues.map(function(iss) { return sevScore[(iss.sev||'').toLowerCase()] || 0; }));
    return maxSev * 1000 + issues.length;  // tie-break by count
  }
  if (col === 'perf_score') return row.perf_score || 0;
  if (col === 'a11y_score') return row.a11y_score != null ? row.a11y_score : -1;
  return 0;
}

// ── Table render ───────────────────────────────────────────────────
function renderTable() {
  var rows = D.rows.slice();
  if (_sortCol) {
    rows.sort(function(a, b) {
      var av = _sortVal(a, _sortCol), bv = _sortVal(b, _sortCol);
      if (av < bv) return -1 * _sortDir;
      if (av > bv) return  1 * _sortDir;
      return 0;
    });
  }

  var html = '';
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var detailPath = r.detail_file || '';
    var hasA11y = r.a11y_score != null;
    html += '<tr' + (hasA11y ? '' : ' class="no-a11y-row"') + '>' +
      '<td class="sno">' + (i + 1) + '</td>' +
      '<td class="page-col">' +
        '<div class="page-name">' + (r.page_name || '').replace(/_/g, ' ') + '</div>' +
        '<div class="page-url-small"><a href="' + r.page_url + '" target="_blank">' + truncUrl(r.page_url, 60) + '</a></div>' +
      '</td>' +
      '<td style="text-align:center">' + scoreBadge(r.perf_score) + '</td>' +
      '<td style="text-align:center">' + (hasA11y
        ? a11yBadge(r.a11y_score, r.a11y_violations)
        : '<span class="a11y-missing-pill" title="Accessibility audit was skipped for this page. Re-run without --no-accessibility to get results.">\u26A0\uFE0F Not audited</span>') + '</td>' +
      '<td>' + fms(r.no_cache_ms) + '</td>' +
      '<td>' + fms(r.with_cache_ms) + '</td>' +
      '<td class="issues-col">' + issuesSummary(r) + '</td>' +
      '<td>' + slowBadge(r.slow_api_count || 0) + '</td>' +
      '<td>' + (detailPath
        ? '<a class="detail-btn" href="' + detailPath + '" target="_blank">\uD83D\uDD0D View Report</a>'
        : '<span class="na">\u2014</span>') +
      '</td>' +
    '</tr>';
  }
  document.getElementById('pages-tbody').innerHTML =
    html || '<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--txt3)">No data available</td></tr>';
}

// ── Header click handlers ──────────────────────────────────────────
document.querySelectorAll('#summary-table thead th.sortable').forEach(function(th) {
  th.addEventListener('click', function() {
    var col = th.getAttribute('data-col');
    if (_sortCol === col) {
      _sortDir *= -1;
    } else {
      _sortCol = col;
      _sortDir = 1;
    }
    // Update header classes
    document.querySelectorAll('#summary-table thead th.sortable').forEach(function(h) {
      h.classList.remove('sort-asc', 'sort-desc');
    });
    th.classList.add(_sortDir === 1 ? 'sort-asc' : 'sort-desc');
    renderTable();
  });
});

renderTable();
renderOverallScorecard();
renderOverallA11yScorecard();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Per-page detail report template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>RapidRadar</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+CiAgPCEtLSBEYXJrIGJhY2tncm91bmQgLS0+CiAgPGNpcmNsZSBjeD0iMTYiIGN5PSIxNiIgcj0iMTYiIGZpbGw9IiMwZDExMTciLz4KICA8IS0tIFJhZGFyIHJpbmdzIC0tPgogIDxjaXJjbGUgY3g9IjE2IiBjeT0iMTYiIHI9IjEyIiBmaWxsPSJub25lIiBzdHJva2U9IiM1OGE2ZmYiIHN0cm9rZS13aWR0aD0iMS41IiBvcGFjaXR5PSIwLjM1Ii8+CiAgPGNpcmNsZSBjeD0iMTYiIGN5PSIxNiIgcj0iNyIgIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzU4YTZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIG9wYWNpdHk9IjAuNiIvPgogIDwhLS0gQ2VudHJlIGRvdCAtLT4KICA8Y2lyY2xlIGN4PSIxNiIgY3k9IjE2IiByPSIyLjIiIGZpbGw9IiM1OGE2ZmYiLz4KICA8IS0tIFN3ZWVwIGFybSAtLT4KICA8bGluZSB4MT0iMTYiIHkxPSIxNiIgeDI9IjI3IiB5Mj0iNyIgc3Ryb2tlPSIjM2ZiOTUwIiBzdHJva2Utd2lkdGg9IjEuOCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPCEtLSBCbGlwIG9uIHN3ZWVwIC0tPgogIDxjaXJjbGUgY3g9IjIzLjUiIGN5PSI5LjUiIHI9IjIuOCIgZmlsbD0iIzNmYjk1MCIgb3BhY2l0eT0iMC45Ii8+CiAgPCEtLSBGYWludCBlY2hvIGJsaXAgLS0+CiAgPGNpcmNsZSBjeD0iMTEiIGN5PSIyMiIgcj0iMS44IiBmaWxsPSIjNThhNmZmIiBvcGFjaXR5PSIwLjQ1Ii8+Cjwvc3ZnPg==">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ── Reset & base ─────────────────────────────────────────────────────── */
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#0d1117;--bg1:#161b22;--bg2:#1c2128;--bg3:#262c36;
  --border:#30363d;
  --txt1:#e6edf3;--txt2:#8b949e;--txt3:#6e7681;
  --blue:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;
  --orange:#e3b341;--purple:#bc8cff;
  --good:#3fb950;--medium:#e3b341;--poor:#f85149;
  --radius:8px;--radius-sm:4px;
  --shadow:0 2px 12px rgba(0,0,0,.4);
}
/* ── Light (day) theme ────────────────────────────────────────────── */
[data-theme="light"]{
  --bg0:#f6f8fa;--bg1:#ffffff;--bg2:#f0f2f5;--bg3:#e8ecf0;
  --border:#d0d7de;
  --txt1:#1f2328;--txt2:#656d76;--txt3:#9198a1;
  --blue:#0969da;--green:#1a7f37;--yellow:#9a6700;--red:#cf222e;
  --orange:#bc4c00;--purple:#8250df;
  --good:#1a7f37;--medium:#9a6700;--poor:#cf222e;
  --shadow:0 2px 12px rgba(0,0,0,.1);
}
[data-theme="light"] .report-header{background:linear-gradient(135deg,#dce8f5 0%,#b8d0eb 100%)!important}
[data-theme="light"] .header-badge{background:rgba(9,105,218,.08);border-color:rgba(9,105,218,.2)}
[data-theme="light"] .post-data-pre{background:var(--bg3);border-color:var(--border);color:var(--txt1)}
/* ── Theme toggle button ──────────────────────────────────────────── */
.theme-toggle-btn{background:var(--bg2);border:1px solid var(--border);color:var(--txt1);border-radius:20px;padding:5px 12px;font-size:13px;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px;line-height:1.4;white-space:nowrap}
.theme-toggle-btn:hover{background:var(--bg3);border-color:var(--blue);color:var(--blue)}
.jmx-btn{background:rgba(227,179,65,.12);border-color:rgba(227,179,65,.35);color:var(--orange)}
.jmx-btn:hover{background:rgba(227,179,65,.22);border-color:var(--orange);color:var(--orange)}
[data-theme="light"] .jmx-btn{background:rgba(188,76,0,.08);border-color:rgba(188,76,0,.28);color:var(--orange)}
[data-theme="light"] .jmx-btn:hover{background:rgba(188,76,0,.15)}
body{background:var(--bg0);color:var(--txt1);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px;line-height:1.6}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}

/* ── Layout ───────────────────────────────────────────────────────────── */
.container{max-width:1400px;margin:0 auto;padding:24px 20px}

/* ── Header ───────────────────────────────────────────────────────────── */
.report-header{background:linear-gradient(135deg,#0d1b2e 0%,#1a2742 100%);border-bottom:1px solid var(--border);padding:28px 0}
.report-header .container{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px}
.header-left h1{font-size:24px;font-weight:700;color:var(--txt1);display:flex;align-items:center;gap:10px}
.header-left h1 .icon{font-size:28px}
.header-badge{margin-top:6px;display:inline-flex;align-items:center;gap:8px;background:rgba(88,166,255,.12);border:1px solid rgba(88,166,255,.25);border-radius:20px;padding:3px 12px;font-size:12px;color:var(--blue)}
.header-right{text-align:right}
.header-meta{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--txt2)}
.header-meta span{display:flex;align-items:center;gap:6px;justify-content:flex-end}
.header-meta .label{color:var(--txt3)}

/* ── Section ──────────────────────────────────────────────────────────── */
.section{margin-bottom:28px}
.section-title{font-size:17px;font-weight:600;color:var(--txt1);margin-bottom:14px;display:flex;align-items:center;gap:8px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.section-title .badge{font-size:11px;background:var(--bg3);border-radius:10px;padding:2px 8px;color:var(--txt2);font-weight:400}

/* ── Summary cards ────────────────────────────────────────────────────── */
.cards-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(195px,1fr));gap:14px;margin-bottom:28px}
.card{background:var(--bg1);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;transition:transform .15s}
.card:hover{transform:translateY(-2px)}
.card-label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt2);margin-bottom:8px}
.card-value{font-size:28px;font-weight:700;line-height:1}
.card-sub{font-size:11px;color:var(--txt3);margin-top:5px}
.card-icon{font-size:20px;margin-bottom:6px}
.status-good{color:var(--good)}
.status-medium{color:var(--medium)}
.status-poor{color:var(--poor)}
.status-neutral{color:var(--blue)}

/* ── Table ────────────────────────────────────────────────────────────── */
.table-wrap{overflow-x:auto;border-radius:var(--radius);border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;background:var(--bg1);font-size:13px}
th{background:var(--bg2);color:var(--txt2);font-weight:600;padding:10px 14px;text-align:left;white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--txt1)}
td{padding:9px 14px;border-top:1px solid var(--border);vertical-align:middle;color:var(--txt1)}
tr:hover td{background:var(--bg2)}
.url-cell{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.url-cell a{color:var(--txt1);font-size:12px}
.url-cell a:hover{color:var(--blue)}
.method-badge{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700;letter-spacing:.05em}
.method-get{background:rgba(63,185,80,.15);color:var(--green)}
.method-post{background:rgba(88,166,255,.15);color:var(--blue)}
.method-put{background:rgba(227,179,65,.15);color:var(--orange)}
.method-delete{background:rgba(248,81,73,.15);color:var(--red)}
.method-other{background:var(--bg3);color:var(--txt2)}
.dur-good{color:var(--good)}
.dur-medium{color:var(--medium)}
.dur-poor{color:var(--red);font-weight:700}
.status-2xx{color:var(--green)}
.status-3xx{color:var(--blue)}
.status-4xx,.status-5xx{color:var(--red)}
.status-err{color:var(--orange)}
.from-cache{color:var(--purple);font-size:11px}

/* ── Chart containers ─────────────────────────────────────────────────── */
.chart-row{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:28px}
@media(max-width:820px){.chart-row{grid-template-columns:1fr}}
.chart-box{background:var(--bg1);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.chart-box h3{font-size:14px;font-weight:600;color:var(--txt2);margin-bottom:14px}
.chart-canvas-wrap{position:relative;height:280px}

/* ── Timing comparison table ──────────────────────────────────────────── */
.timing-table td:nth-child(1){color:var(--txt2);font-size:12px}
.timing-table td:nth-child(2),.timing-table td:nth-child(3){font-weight:600;font-size:13px}
.timing-highlight td{background:var(--bg3)!important;border-top:2px solid var(--border)}

/* ── Web Vitals ───────────────────────────────────────────────────────── */
.vitals-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:12px}
.vital-card{background:var(--bg1);border:1px solid var(--border);border-radius:var(--radius);padding:16px;text-align:center}
.vital-name{font-size:11px;text-transform:uppercase;color:var(--txt2);letter-spacing:.06em;margin-bottom:4px}
.vital-value{font-size:26px;font-weight:700}
.vital-range{font-size:10px;color:var(--txt3);margin-top:4px}
.vital-bar-track{height:4px;background:var(--bg3);border-radius:2px;margin-top:8px;overflow:hidden}
.vital-bar-fill{height:100%;border-radius:2px}

/* ── Issues ───────────────────────────────────────────────────────────── */
.issues-list{display:flex;flex-direction:column;gap:10px}
.issue-card{background:var(--bg1);border:1px solid var(--border);border-left-width:3px;border-radius:var(--radius);padding:14px 18px;display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:start}
.issue-card.sev-critical{border-left-color:var(--red)}
.issue-card.sev-high{border-left-color:var(--orange)}
.issue-card.sev-medium{border-left-color:var(--yellow)}
.issue-card.sev-low{border-left-color:var(--blue)}
.issue-icon{font-size:18px;margin-top:2px}
.issue-title{font-weight:600;font-size:13px;color:var(--txt1);margin-bottom:3px}
.issue-desc{font-size:12px;color:var(--txt2);margin-bottom:5px}
.issue-rec{font-size:11px;color:var(--txt3)}
.issue-rec strong{color:var(--blue)}
.sev-badge{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:700;white-space:nowrap;align-self:start}
.sev-critical{background:rgba(248,81,73,.15);color:var(--red)}
.sev-high{background:rgba(227,179,65,.15);color:var(--orange)}
.sev-medium{background:rgba(210,153,34,.15);color:var(--yellow)}
.sev-low{background:rgba(88,166,255,.12);color:var(--blue)}

/* ── Empty state ──────────────────────────────────────────────────────── */
.empty-state{text-align:center;padding:32px 16px;color:var(--txt3);background:var(--bg1);border-radius:var(--radius);border:1px dashed var(--border)}
.empty-state .icon{font-size:32px;margin-bottom:8px}

/* ── Footer ───────────────────────────────────────────────────────────── */
.report-footer{text-align:center;padding:24px;color:var(--txt3);font-size:12px;border-top:1px solid var(--border);margin-top:32px}

/* ── Tabs ─────────────────────────────────────────────────────────────── */
.tab-bar{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:16px}
.tab-btn{padding:7px 16px;font-size:13px;border:none;background:none;color:var(--txt2);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;border-radius:var(--radius-sm) var(--radius-sm) 0 0}
.tab-btn:hover{color:var(--txt1);background:var(--bg2)}
.tab-btn.active{color:var(--blue);border-bottom-color:var(--blue)}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* ── Comparison bar ──────────────────────────────────────────────────── */
.compare-bar{height:8px;border-radius:4px;display:inline-block;min-width:4px;max-width:200px;vertical-align:middle;margin-left:8px}

/* ── Expandable detail rows ───────────────────────────────────────────── */
.expand-btn{background:none;border:1px solid var(--border);color:var(--txt2);border-radius:3px;padding:1px 6px;font-size:11px;cursor:pointer;margin-left:6px;vertical-align:middle;transition:all .15s}
.expand-btn:hover{background:var(--bg3);color:var(--txt1)}
.detail-row{background:var(--bg2)}
.detail-row td{padding:0!important;border-top:none}
.detail-inner{padding:14px 20px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
@media(max-width:900px){.detail-inner{grid-template-columns:1fr}}
.detail-section{min-width:0}
.detail-section h4{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt2);margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)}
.kv-table{width:100%;border-collapse:collapse;font-size:12px}
.kv-table tr:hover td{background:var(--bg3)}
.kv-table td{padding:3px 6px;border-bottom:1px solid rgba(48,54,61,.5);vertical-align:top;word-break:break-all}
.kv-table td:first-child{color:var(--txt2);white-space:nowrap;padding-right:12px;width:35%}
.kv-table td:last-child{color:var(--txt1);font-family:ui-monospace,'Cascadia Code',Consolas,monospace;font-size:11px}
.no-data{color:var(--txt3);font-size:12px;font-style:italic}
.post-data-pre{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:11px;font-family:ui-monospace,monospace;overflow-x:auto;white-space:pre-wrap;word-break:break-all;color:var(--txt1);max-height:200px;overflow-y:auto}
/* ── Performance Score section ──────────────────────────────────────────── */
.score-layout{display:flex;gap:28px;align-items:flex-start;flex-wrap:wrap;padding:4px 0}
.score-gauge-wrap{display:flex;flex-direction:column;align-items:center;min-width:160px}
.score-gauge-meta{text-align:center;margin-top:10px}
.score-gauge-title{font-size:13px;font-weight:600;color:var(--txt1)}
.score-gauge-sub{font-size:11px;color:var(--txt3)}
.score-breakdown{flex:1;min-width:280px}
.score-breakdown-title{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt2);margin-bottom:14px;font-weight:600;padding-bottom:6px;border-bottom:1px solid var(--border)}
.score-row{display:grid;grid-template-columns:120px 1fr 44px 90px;align-items:center;gap:10px;margin-bottom:10px;font-size:12px}
.score-row-label{color:var(--txt1);font-weight:500}
.score-row-weight{color:var(--txt3);font-size:10px;margin-left:4px}
.score-row-bar-wrap{height:8px;background:var(--bg3);border-radius:4px;overflow:hidden}
.score-row-bar{height:100%;border-radius:4px;transition:width .5s ease}
.score-row-val{font-weight:700;text-align:right;font-size:13px}
.score-row-metric{color:var(--txt3);font-size:11px}
/* ── Accessibility section ─────────────────────────────────────────────── */
.a11y-layout{display:flex;gap:28px;align-items:flex-start;flex-wrap:wrap;padding:4px 0}
.a11y-gauge-wrap{display:flex;flex-direction:column;align-items:center;min-width:160px}
.a11y-gauge-meta{text-align:center;margin-top:10px}
.a11y-gauge-title{font-size:13px;font-weight:600;color:var(--txt1)}
.a11y-gauge-sub{font-size:11px;color:var(--txt3)}
.a11y-right{flex:1;min-width:300px;display:flex;flex-direction:column;gap:18px}
.a11y-impact-row{display:grid;grid-template-columns:70px 1fr 36px;align-items:center;gap:10px;margin-bottom:8px;font-size:12px}
.a11y-impact-lbl{font-weight:600;font-size:12px}
.a11y-impact-bar-wrap{height:8px;background:var(--bg3);border-radius:4px;overflow:hidden}
.a11y-impact-bar{height:100%;border-radius:4px}
.a11y-impact-cnt{font-weight:700;text-align:right}
.a11y-sub-title{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt2);font-weight:600;padding-bottom:6px;border-bottom:1px solid var(--border);margin-bottom:10px}
.a11y-violations-table table{font-size:12px}
.a11y-violations-table th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding:7px 10px}
.a11y-violations-table td{padding:8px 10px;font-size:12px;vertical-align:top}
.impact-critical{color:var(--red);font-weight:700}
.impact-serious{color:var(--orange);font-weight:700}
.impact-moderate{color:var(--yellow);font-weight:600}
.impact-minor{color:var(--blue);font-weight:500}
.a11y-node-pre{font-family:ui-monospace,'Cascadia Code',Consolas,monospace;font-size:10px;background:var(--bg3);border:1px solid var(--border);border-radius:3px;padding:4px 6px;margin-top:4px;white-space:pre-wrap;word-break:break-all;color:var(--txt1);max-height:120px;overflow-y:auto}
.a11y-tags{display:flex;flex-wrap:wrap;gap:3px;margin-top:4px}
.a11y-tag{font-size:9px;padding:1px 5px;border-radius:3px;background:var(--bg3);color:var(--txt2);border:1px solid var(--border)}
.a11y-help-link{font-size:11px;color:var(--blue)}
.a11y-na{color:var(--txt3);font-style:italic;padding:24px;text-align:center}
</style>
</head>
<body>

<!-- ═══════════════════════════  HEADER  ══════════════════════════════ -->
<header class="report-header">
  <div class="container">
    <div class="header-left">
      <h1><span class="icon">🚀</span> RapidRadar</h1>
      <div class="header-badge" id="hdr-browser"></div>
      <div style="margin-top:8px;font-size:12px;font-weight:700;color:#58a6ff;letter-spacing:.04em">Created by OPUS EDM PERF TEAM</div>
    </div>
    <div style="display:flex;align-items:flex-start;gap:14px">
      <div style="display:flex;flex-direction:column;gap:8px">
        <button class="theme-toggle-btn" id="theme-toggle" onclick="toggleTheme()" title="Toggle day / night theme">
          <span id="theme-icon">☀️</span><span id="theme-label">Day</span>
        </button>
        <button class="theme-toggle-btn jmx-btn" onclick="convertToJmx()" title="Download JMeter test plan for all captured APIs">
          ⬇ Convert to JMX
        </button>
      </div>
      <div class="header-right">
        <div class="header-meta" id="hdr-meta"></div>
      </div>
    </div>
  </div>
</header>

<!-- ═══════════════════════════  BODY  ═══════════════════════════════ -->
<div class="container">

  <!-- Summary cards -->
  <div class="cards-grid" id="summary-cards"></div>

  <!-- Performance Score -->
  <div class="section">
    <div class="section-title">&#x1F4CA; Performance Score</div>
    <div id="perf-score-container"></div>
  </div>

  <!-- Accessibility -->
  <div class="section">
    <div class="section-title">&#x267F; Accessibility Audit <span class="badge" id="a11y-badge"></span></div>
    <div id="a11y-container"></div>
  </div>

  <!-- Charts row -->
  <div class="chart-row">
    <div class="chart-box">
      <h3>Load Time Comparison</h3>
      <div class="chart-canvas-wrap"><canvas id="chartLoadTime"></canvas></div>
    </div>
    <div class="chart-box">
      <h3>Resource Distribution (No-Cache Run)</h3>
      <div class="chart-canvas-wrap"><canvas id="chartResourceType"></canvas></div>
    </div>
  </div>

  <!-- Timing Breakdown -->
  <div class="section">
    <div class="section-title">⏱ Timing Breakdown</div>
    <div class="table-wrap">
      <table id="timing-table" class="timing-table"></table>
    </div>
  </div>

  <!-- Core Web Vitals -->
  <div class="section">
    <div class="section-title">🌐 Core Web Vitals <span class="badge" id="vitals-run-badge"></span></div>
    <div class="tab-bar">
      <button class="tab-btn active" onclick="switchTab('vitals','no-cache')">No Cache</button>
      <button class="tab-btn" onclick="switchTab('vitals','with-cache')">With Cache</button>
    </div>
    <div id="vitals-no-cache" class="tab-pane active"><div class="vitals-grid" id="vitals-grid-nc"></div></div>
    <div id="vitals-with-cache" class="tab-pane"><div class="vitals-grid" id="vitals-grid-wc"></div></div>
  </div>

  <!-- Performance Issues -->
  <div class="section">
    <div class="section-title">⚠️ Performance Issues <span class="badge" id="issues-count-badge"></span></div>
    <div class="issues-list" id="issues-list"></div>
  </div>

  <!-- Slow APIs -->
  <div class="section">
    <div class="section-title">🔴 Slow API Calls <span class="badge" id="slow-apis-badge"></span></div>
    <div class="tab-bar">
      <button class="tab-btn active" onclick="switchTab('apis','no-cache')">No Cache</button>
      <button class="tab-btn" onclick="switchTab('apis','with-cache')">With Cache</button>
    </div>
    <div id="apis-no-cache" class="tab-pane active">
      <div class="table-wrap"><table id="slow-apis-nc-table"></table></div>
    </div>
    <div id="apis-with-cache" class="tab-pane">
      <div class="table-wrap"><table id="slow-apis-wc-table"></table></div>
    </div>
  </div>

  <!-- Slow / Large Resources -->
  <div class="section">
    <div class="section-title">📦 Slow &amp; Large Resources <span class="badge" id="slow-res-badge"></span></div>
    <div class="tab-bar">
      <button class="tab-btn active" onclick="switchResourceTab('slow')">Slow (&gt;1 s)</button>
      <button class="tab-btn" onclick="switchResourceTab('large')">Large (&gt;500 KB)</button>
      <button class="tab-btn" onclick="switchResourceTab('blocking')">Render-Blocking</button>
    </div>
    <div id="res-slow" class="tab-pane active">
      <div class="table-wrap"><table id="slow-res-table"></table></div>
    </div>
    <div id="res-large" class="tab-pane">
      <div class="table-wrap"><table id="large-res-table"></table></div>
    </div>
    <div id="res-blocking" class="tab-pane">
      <div class="table-wrap"><table id="blocking-res-table"></table></div>
    </div>
  </div>

  <!-- All API Calls -->
  <div class="section">
    <div class="section-title">📡 All API Calls <span class="badge" id="all-apis-badge"></span></div>
    <div class="tab-bar">
      <button class="tab-btn active" onclick="switchTab('allapis','no-cache')">No Cache</button>
      <button class="tab-btn" onclick="switchTab('allapis','with-cache')">With Cache</button>
    </div>
    <div id="allapis-no-cache" class="tab-pane active">
      <div class="table-wrap"><table id="all-apis-nc-table"></table></div>
    </div>
    <div id="allapis-with-cache" class="tab-pane">
      <div class="table-wrap"><table id="all-apis-wc-table"></table></div>
    </div>
  </div>

</div><!-- /container -->

<footer class="report-footer">
  <strong>RapidRadar</strong> &bull; OPUS EDM PERF TEAM &bull; <span id="footer-ts"></span>
</footer>

<script>
// ═══════════════════════  DATA INJECTION  ════════════════════════════
const D = __REPORT_DATA_PLACEHOLDER__;

// ═══════════════════════  THEME HELPERS  ═════════════════════════════
var _charts = {};
function getCSSVar(v) {
  return getComputedStyle(document.documentElement).getPropertyValue(v).trim();
}

// ═══════════════════════  HELPERS  ═══════════════════════════════════
function fms(v, decimals) {
  if (v === null || v === undefined) return '<span style="color:var(--txt3)">N/A</span>';
  var n = parseFloat(v);
  if (isNaN(n)) return '<span style="color:var(--txt3)">N/A</span>';
  if (decimals !== undefined) return n.toFixed(decimals) + ' ms';
  return Math.round(n) + ' ms';
}
function fmsSafe(v) {
  if (v === null || v === undefined || isNaN(parseFloat(v))) return 'N/A';
  return Math.round(parseFloat(v)) + ' ms';
}
function fb(bytes) {
  if (!bytes || bytes === 0) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}
function pctOf(a, b) {
  if (!b || b === 0) return '—';
  return ((1 - a / b) * 100).toFixed(1) + '% faster';
}
function statusClass(ms, good, medium) {
  if (ms === null || ms === undefined) return '';
  var n = parseFloat(ms);
  if (n <= good) return 'status-good';
  if (n <= medium) return 'status-medium';
  return 'status-poor';
}
function durClass(ms) {
  if (ms < 1000) return 'dur-good';
  if (ms < 3000) return 'dur-medium';
  return 'dur-poor';
}
function methodBadge(method) {
  var m = (method || 'GET').toUpperCase();
  var cls = { GET:'method-get', POST:'method-post', PUT:'method-put', DELETE:'method-delete' }[m] || 'method-other';
  return '<span class="method-badge ' + cls + '">' + m + '</span>';
}
function httpStatusClass(s) {
  if (!s || s === 'failed') return 'status-err';
  var code = parseInt(s);
  if (code >= 200 && code < 300) return 'status-2xx';
  if (code >= 300 && code < 400) return 'status-3xx';
  if (code >= 400 && code < 500) return 'status-4xx';
  if (code >= 500) return 'status-5xx';
  return '';
}
function truncUrl(url, n) {
  n = n || 80;
  if (!url) return '';
  if (url.length <= n) return url;
  return url.substring(0, n) + '…';
}
function resourceName(url) {
  try { return decodeURIComponent(new URL(url).pathname.split('/').pop() || url); }
  catch(e) { return url; }
}
function el(id) { return document.getElementById(id); }

function emptyState(msg) {
  return '<tr><td colspan="10"><div class="empty-state"><div class="icon">✅</div>' + msg + '</div></td></tr>';
}

// ═══════════════════════  TAB SWITCHING  ═════════════════════════════
var tabState = {};
function switchTab(group, name) {
  var panes = document.querySelectorAll('[id^="' + group + '-"]');
  panes.forEach(function(p) {
    p.classList.remove('active');
    p.style.display = 'none';
  });
  var target = document.getElementById(group + '-' + name);
  if (target) { target.classList.add('active'); target.style.display = 'block'; }
  var parent = target && target.closest('.section');
  if (parent) {
    parent.querySelectorAll('.tab-btn').forEach(function(b, i) {
      b.classList.toggle('active', b.textContent.toLowerCase().includes(name.replace('-',' ')));
    });
  }
}
function switchResourceTab(name) {
  ['slow','large','blocking'].forEach(function(n) {
    var p = el('res-' + n);
    if (p) { p.classList.remove('active'); p.style.display = 'none'; }
  });
  var target = el('res-' + name);
  if (target) { target.classList.add('active'); target.style.display = 'block'; }
}

// ═══════════════════════  HEADER  ════════════════════════════════════
function renderHeader() {
  el('hdr-browser').innerHTML =
    '<span>🖥</span> ' + (D.browser_display || D.browser_key);
  var loginBadge = D.login_time_ms
    ? '<span><span class="label">🔐 Login Time</span><strong style="color:' +
      (D.login_time_ms < 3000 ? 'var(--green)' : D.login_time_ms < 8000 ? 'var(--yellow)' : 'var(--red)') +
      '">' + D.login_time_ms + ' ms</strong></span>'
    : '';
  el('hdr-meta').innerHTML =
    '<span><span class="label">Timestamp</span>' + D.timestamp + '</span>' +
    loginBadge +
    (D.edm_version ? '<span><span class="label">EDM Version</span>' + D.edm_version + '</span>' : '') +
    '<span><span class="label">Test Page URL</span><a href="' + D.page_url + '">' + truncUrl(D.page_url, 60) + '</a></span>';
  el('footer-ts').textContent = D.timestamp;
}

// ═══════════════════════  SUMMARY CARDS  ═════════════════════════════
function renderCards() {
  var nc = D.no_cache, wc = D.with_cache;
  var nlt = nc.timing.total_load_time, wlt = wc.timing.total_load_time;
  var improvement = (nlt > 0) ? ((nlt - wlt) / nlt * 100) : 0;

  var slowTotal = nc.slow_apis.length + wc.slow_apis.length;
  var totalSlowRes = nc.slow_resources.length;

  function card(icon, label, value, cls, sub) {
    return '<div class="card"><div class="card-icon">' + icon + '</div>' +
      '<div class="card-label">' + label + '</div>' +
      '<div class="card-value ' + (cls||'') + '">' + value + '</div>' +
      (sub ? '<div class="card-sub">' + sub + '</div>' : '') + '</div>';
  }

  el('summary-cards').innerHTML =
    (D.edm_version ? card('📋', 'EDM Version', D.edm_version, 'status-neutral', 'Application version under test') : '') +
    card('🔐', 'Login Time',
      D.login_time_ms ? D.login_time_ms + ' ms' : '—',
      !D.login_time_ms ? '' : D.login_time_ms < 3000 ? 'status-good' : D.login_time_ms < 8000 ? 'status-medium' : 'status-poor',
      'Time to reach post-login page') +
    card('⌛', 'No-Cache Load Time', fmsSafe(nlt), statusClass(nlt, 2000, 5000), 'Cold / first load') +
    card('⚡', 'With-Cache Load Time', fmsSafe(wlt), statusClass(wlt, 1500, 4000), 'Warm / repeat load') +
    card('📈', 'Cache Improvement', improvement > 0 ? improvement.toFixed(1) + '%' : '—',
      improvement > 30 ? 'status-good' : improvement > 10 ? 'status-medium' : 'status-neutral',
      'Speed gain from cache') +
    card('🌐', 'Browser', D.browser_display || D.browser_key, 'status-neutral', 'Test environment') +
    card('🔴', 'Slow APIs (>' + D.slow_api_threshold + ' ms)', nc.slow_apis.length,
      nc.slow_apis.length === 0 ? 'status-good' : nc.slow_apis.length < 3 ? 'status-medium' : 'status-poor',
      'No-cache run') +
    card('📦', 'Total Requests', nc.total_requests, 'status-neutral',
      'No-cache · ' + fb(nc.total_size)) +
    card('🐢', 'Slow Resources (>1 s)', nc.slow_resources.length,
      nc.slow_resources.length === 0 ? 'status-good' : 'status-medium', 'No-cache run') +
    card('📉', 'Page Title',
      '<span style="font-size:14px;font-weight:600">' + truncUrl(nc.page_title || '—', 28) + '</span>',
      '', '');
}

// ═════════════════════════  CHARTS  ══════════════════════════════════
function renderCharts() {
  if (_charts.loadTime)     { _charts.loadTime.destroy();     _charts.loadTime = null; }
  if (_charts.resourceType) { _charts.resourceType.destroy(); _charts.resourceType = null; }

  var nc = D.no_cache.timing, wc = D.with_cache.timing;
  var tickColor = getCSSVar('--txt2');
  var gridColor = getCSSVar('--border');
  var bg2Color  = getCSSVar('--bg2');
  var defaults = { responsive: true, maintainAspectRatio: false,
    plugins: { legend: { labels: { color: tickColor, font:{ size:12 } } } } };

  // ── Load time grouped bar ───────────────────────────────────────────
  _charts.loadTime = new Chart(el('chartLoadTime'), {
    type: 'bar',
    data: {
      labels: ['TTFB', 'DOM Interactive', 'DOM Loaded', 'Total Load'],
      datasets: [
        { label: 'No Cache', data: [nc.ttfb, nc.dom_interactive, nc.dom_content_loaded, nc.total_load_time],
          backgroundColor: 'rgba(248,81,73,0.75)', borderColor: 'rgba(248,81,73,1)', borderWidth: 1 },
        { label: 'With Cache', data: [wc.ttfb, wc.dom_interactive, wc.dom_content_loaded, wc.total_load_time],
          backgroundColor: 'rgba(63,185,80,0.75)', borderColor: 'rgba(63,185,80,1)', borderWidth: 1 }
      ]
    },
    options: Object.assign({}, defaults, {
      plugins: Object.assign({}, defaults.plugins, {
        tooltip: { callbacks: { label: function(ctx) { return ctx.dataset.label + ': ' + Math.round(ctx.parsed.y) + ' ms'; } } }
      }),
      scales: {
        x: { ticks: { color: tickColor }, grid: { color: gridColor } },
        y: { ticks: { color: tickColor, callback: function(v){ return v + ' ms'; } }, grid: { color: gridColor } }
      }
    })
  });

  // ── Resource distribution donut ─────────────────────────────────────
  var typeCounts = {};
  (D.no_cache.resources || []).forEach(function(r) {
    var t = r.type || 'other';
    typeCounts[t] = (typeCounts[t] || 0) + 1;
  });
  var labels = Object.keys(typeCounts);
  var counts = labels.map(function(l) { return typeCounts[l]; });
  var palette = ['#58a6ff','#3fb950','#d29922','#f85149','#bc8cff','#e3b341','#79c0ff','#56d364','#ff7b72'];

  _charts.resourceType = new Chart(el('chartResourceType'), {
    type: 'doughnut',
    data: {
      labels: labels,
      datasets: [{ data: counts, backgroundColor: palette.slice(0, labels.length), borderWidth: 2, borderColor: bg2Color }]
    },
    options: Object.assign({}, defaults, {
      plugins: Object.assign({}, defaults.plugins, {
        tooltip: { callbacks: { label: function(ctx) { return ctx.label + ': ' + ctx.parsed + ' requests'; } } }
      }),
      cutout: '60%'
    })
  });
}

// ═══════════════════════  TIMING TABLE  ══════════════════════════════
function renderTimingTable() {
  var nc = D.no_cache.timing, wc = D.with_cache.timing;
  var rows = [
    ['DNS Lookup',           nc.dns_lookup,         wc.dns_lookup,         600,  1500],
    ['TCP Connection',       nc.tcp_connect,        wc.tcp_connect,        300,  1000],
    ['Time to First Byte',   nc.ttfb,               wc.ttfb,               200,  600],
    ['Server Response',      nc.server_response,    wc.server_response,    400,  1500],
    ['DOM Interactive',      nc.dom_interactive,    wc.dom_interactive,    2000, 5000],
    ['DOM Content Loaded',   nc.dom_content_loaded, wc.dom_content_loaded, 2500, 5000],
    ['DOM Processing',       nc.dom_processing,     wc.dom_processing,     1000, 3000],
    ['Browser Load Event',   nc.dom_load_event,     wc.dom_load_event,     2500, 5000],
    ['First Contentful Paint (FCP)', nc.fcp,        wc.fcp,                1800, 3000],
    ['Largest Contentful Paint (LCP)', nc.lcp,      wc.lcp,                2500, 4000],
  ];
  var html = '<thead><tr><th>Metric</th><th>No Cache</th><th>With Cache</th><th>Difference</th></tr></thead><tbody>';
  rows.forEach(function(r) {
    var label = r[0], ncVal = r[1], wcVal = r[2], good = r[3], med = r[4];
    var diff = (ncVal !== null && wcVal !== null) ? (parseFloat(wcVal) - parseFloat(ncVal)) : null;
    var diffStr = (diff !== null) ? ((diff > 0 ? '+' : '') + Math.round(diff) + ' ms') : '—';
    var diffCls = (diff !== null) ? (diff < 0 ? 'status-good' : diff > 0 ? 'status-poor' : '') : '';
    html += '<tr>' +
      '<td style="color:var(--txt2)">' + label + '</td>' +
      '<td class="' + statusClass(ncVal, good, med) + '">' + fmsSafe(ncVal) + '</td>' +
      '<td class="' + statusClass(wcVal, good, med) + '">' + fmsSafe(wcVal) + '</td>' +
      '<td class="' + diffCls + '">' + diffStr + '</td></tr>';
  });
  // Total row – wall-clock time from navigation start to last network activity
  var ncTotal = D.no_cache.timing.total_load_time, wcTotal = D.with_cache.timing.total_load_time;
  var totalDiff = wcTotal - ncTotal;
  html += '<tr class="timing-highlight">' +
    '<td style="font-weight:700">Total Load Time <span style="font-size:11px;font-weight:400;color:var(--txt3)">(wall-clock, incl. all XHR)</span></td>' +
    '<td class="' + statusClass(ncTotal, 2000, 5000) + '" style="font-weight:700">' + fmsSafe(ncTotal) + '</td>' +
    '<td class="' + statusClass(wcTotal, 2000, 5000) + '" style="font-weight:700">' + fmsSafe(wcTotal) + '</td>' +
    '<td class="' + (totalDiff < 0 ? 'status-good' : 'status-poor') + '" style="font-weight:700">' +
    (totalDiff > 0 ? '+' : '') + Math.round(totalDiff) + ' ms (' + pctOf(wcTotal, ncTotal) + ')</td></tr>';
  html += '</tbody>';
  el('timing-table').innerHTML = html;
}

// ═════════════════════════  WEB VITALS  ══════════════════════════════
function renderVitals(containerId, timing) {
  var vitals = [
    { key:'fcp', name:'FCP', desc:'First Contentful Paint', val: timing.fcp,
      good:1800, medium:3000, unit:'ms', fmt: fmsSafe },
    { key:'lcp', name:'LCP', desc:'Largest Contentful Paint', val: timing.lcp,
      good:2500, medium:4000, unit:'ms', fmt: fmsSafe },
    { key:'cls', name:'CLS', desc:'Cumulative Layout Shift', val: timing.cls,
      good:0.1, medium:0.25, unit:'', fmt: function(v){ return v === null ? 'N/A' : parseFloat(v).toFixed(3); } },
    { key:'ttfb', name:'TTFB', desc:'Time to First Byte', val: timing.ttfb,
      good:200, medium:600, unit:'ms', fmt: fmsSafe },
  ];
  var html = '';
  vitals.forEach(function(v) {
    var val = v.val;
    var isNull = val === null || val === undefined;
    var sc = isNull ? 'status-neutral' : statusClass(val, v.good, v.medium);
    var colorMap = { 'status-good':'var(--good)', 'status-medium':'var(--medium)', 'status-poor':'var(--poor)', 'status-neutral':'var(--blue)' };
    var barColor = colorMap[sc] || 'var(--blue)';
    var pct = isNull ? 0 : Math.min(100, (parseFloat(val) / (v.medium * 1.5)) * 100);
    html += '<div class="vital-card">' +
      '<div class="vital-name">' + v.name + '</div>' +
      '<div class="vital-value ' + sc + '">' + v.fmt(val) + '</div>' +
      '<div class="vital-range" style="color:var(--txt3)">' + v.desc + '</div>' +
      '<div class="vital-bar-track"><div class="vital-bar-fill" style="width:' + pct + '%;background:' + barColor + '"></div></div>' +
      '<div class="vital-range" style="margin-top:6px">Good &le; ' + v.good + (v.unit?v.unit:'') + ' &bull; Poor &gt; ' + v.medium + (v.unit?v.unit:'') + '</div>' +
      '</div>';
  });
  el(containerId).innerHTML = html;
}

// ═══════════════════════  ISSUES ANALYSIS  ═══════════════════════════
function analyzeIssues() {
  var nc = D.no_cache, wc = D.with_cache;
  var issues = [];

  function issue(sev, icon, cat, title, desc, rec) {
    issues.push({ sev:sev, icon:icon, cat:cat, title:title, desc:desc, rec:rec });
  }

  // TTFB
  var ttfb = nc.timing.ttfb;
  if (ttfb > 1000) issue('critical','🔴','Server','High Time to First Byte: ' + fmsSafe(ttfb),
    'The server took ' + fmsSafe(ttfb) + ' to send the first byte. This delays every subsequent asset.',
    '<strong>Fix:</strong> Profile server-side code, optimise database queries, add server-level caching (Redis, Memcached), or place a CDN in front of the origin.');
  else if (ttfb > 600) issue('high','🟠','Server','Elevated TTFB: ' + fmsSafe(ttfb),
    'TTFB of ' + fmsSafe(ttfb) + ' means the server is slower than recommended (&le;600 ms).',
    '<strong>Fix:</strong> Review server-side processing bottlenecks and consider response caching.');

  // Slow APIs
  if (nc.slow_apis.length > 0) {
    var worst = nc.slow_apis.sort(function(a,b){return b.duration-a.duration;})[0];
    issue(nc.slow_apis.length >= 3 ? 'critical' : 'high', '⏳', 'API Performance',
      nc.slow_apis.length + ' API call(s) exceeded ' + D.slow_api_threshold + ' ms',
      'The slowest call to <code>' + truncUrl(worst.url, 70) + '</code> took ' + fmsSafe(worst.duration) + '.',
      '<strong>Fix:</strong> Profile slow endpoints, add database indexes, paginate heavy data sets, or introduce caching layers.');
  }

  // FCP
  if (nc.timing.fcp && nc.timing.fcp > 3000) issue('high','🎨','Rendering','Slow First Contentful Paint: ' + fmsSafe(nc.timing.fcp),
    'Users wait over 3 s before seeing any content. This hurts perceived load speed and SEO.',
    '<strong>Fix:</strong> Eliminate render-blocking scripts/stylesheets, inline critical CSS, use &lt;link rel="preload"&gt; for key assets.');

  // LCP
  if (nc.timing.lcp && nc.timing.lcp > 4000) issue('high','🖼','Rendering','Slow Largest Contentful Paint: ' + fmsSafe(nc.timing.lcp),
    'The main viewport content took ' + fmsSafe(nc.timing.lcp) + ' to appear. Google recommends &lt; 2.5 s.',
    '<strong>Fix:</strong> Optimise hero images (WebP, lazy-load non-hero images), preload LCP resource, reduce server latency.');

  // CLS
  if (nc.timing.cls && parseFloat(nc.timing.cls) > 0.25) issue('medium','📐','Stability','High Cumulative Layout Shift: ' + parseFloat(nc.timing.cls).toFixed(3),
    'Significant visual instability detected. Elements are shifting after initial render.',
    '<strong>Fix:</strong> Add explicit width/height to images and embeds, avoid dynamically inserted content above the fold.');

  // Large resources
  if (nc.large_resources.length > 0) {
    var largestR = nc.large_resources[0];
    issue('medium','📦','Resource Size',
      nc.large_resources.length + ' resource(s) larger than 500 KB',
      'Largest: ' + resourceName(largestR.name) + ' (' + fb(largestR.size) + ').',
      '<strong>Fix:</strong> Compress images (WebP/AVIF), enable Brotli/gzip, code-split JavaScript bundles, tree-shake unused CSS.');
  }

  // Slow resources
  if (nc.slow_resources.length > 0) {
    issue('medium','🐌','Resource Loading',
      nc.slow_resources.length + ' resource(s) took over 1 s to load',
      'Slow resources extend the critical loading path.',
      '<strong>Fix:</strong> Host assets on a CDN, enable HTTP/2, use resource hints (&lt;link rel="preconnect"&gt;).');
  }

  // Render-blocking
  if (nc.render_blocking && nc.render_blocking.length > 0) {
    issue('medium','🚧','Render Blocking',
      nc.render_blocking.length + ' render-blocking script(s)/stylesheet(s) detected',
      'These resources block paint until they finish downloading and parsing.',
      '<strong>Fix:</strong> Add <code>async</code> or <code>defer</code> to scripts, inline critical CSS, load non-critical CSS asynchronously.');
  }

  // Too many requests
  if (nc.total_requests > 150) {
    issue('medium','🌐','Network',
      'High number of requests: ' + nc.total_requests,
      'Over 150 HTTP requests increases connection overhead and queuing delays.',
      '<strong>Fix:</strong> Bundle JavaScript/CSS, use CSS sprites for small icons, audit and remove unused third-party scripts.');
  }

  // Large total page weight
  if (nc.total_size > 5 * 1024 * 1024) {
    issue('medium','⚖️','Page Weight',
      'Total page weight exceeds 5 MB: ' + fb(nc.total_size),
      'Large page weight increases bandwidth usage and extends load time on slower connections.',
      '<strong>Fix:</strong> Audit and remove unused libraries, lazy-load below-the-fold content, use image compression.');
  }

  return issues;
}

function renderIssues() {
  var issues = analyzeIssues();
  el('issues-count-badge').textContent = issues.length + (issues.length === 1 ? ' issue' : ' issues');
  if (issues.length === 0) {
    el('issues-list').innerHTML = '<div class="empty-state"><div class="icon">🎉</div>No significant performance issues detected!</div>';
    return;
  }
  var order = { critical:0, high:1, medium:2, low:3 };
  issues.sort(function(a,b){ return (order[a.sev]||9) - (order[b.sev]||9); });
  el('issues-list').innerHTML = issues.map(function(iss) {
    return '<div class="issue-card sev-' + iss.sev + '">' +
      '<div class="issue-icon">' + iss.icon + '</div>' +
      '<div><div class="issue-title">' + iss.title + '</div>' +
      '<div class="issue-desc">' + iss.desc + '</div>' +
      '<div class="issue-rec">' + iss.rec + '</div></div>' +
      '<span class="sev-badge sev-' + iss.sev + '">' + iss.sev.toUpperCase() + '</span></div>';
  }).join('');
}

// ═════════════════════════  SLOW APIS TABLE  ══════════════════════════

// Sensitive header keys to mask in the report (case-insensitive)
var MASKED_HEADERS = ['authorization','cookie','set-cookie','x-api-key',
  'x-auth-token','x-csrf-token','proxy-authorization'];

function maskHeaderValue(key, val) {
  if (MASKED_HEADERS.indexOf(key.toLowerCase()) !== -1) {
    return '*** (masked for security) ***';
  }
  return val;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function kvTableHtml(obj) {
  var keys = Object.keys(obj || {});
  if (keys.length === 0) return '<span class="no-data">None</span>';
  var rows = keys.map(function(k) {
    return '<tr><td>' + escHtml(k) + '</td><td>' +
      escHtml(String(maskHeaderValue(k, Array.isArray(obj[k]) ? obj[k].join(', ') : obj[k]))) +
      '</td></tr>';
  }).join('');
  return '<table class="kv-table"><tbody>' + rows + '</tbody></table>';
}

var _detailCounter = 0;

function apiTableHtml(apis, threshold) {
  var emptyMsg = threshold > 0
    ? 'No API calls exceeded the ' + threshold + ' ms threshold on this run.'
    : 'No API/XHR/Fetch calls recorded on this run.';

  if (!apis || apis.length === 0) {
    return '<thead><tr><th></th><th>Method</th><th>URL</th><th>Type</th><th>Status</th><th>Duration</th><th>Size</th></tr></thead><tbody>' +
      emptyState(emptyMsg) + '</tbody>';
  }

  var sorted = apis.slice().sort(function(a,b){ return b.duration - a.duration; });
  var html = '<thead><tr>' +
    '<th style="width:28px"></th>' +
    '<th>Method</th><th>URL</th><th>Type</th>' +
    '<th>Status</th><th>Duration</th><th>Size</th>' +
    '</tr></thead><tbody>';

  sorted.forEach(function(a) {
    var uid = 'api-detail-' + (++_detailCounter);
    var hasQuery  = a.query_params   && Object.keys(a.query_params).length   > 0;
    var hasPost   = a.post_data      && a.post_data.trim().length             > 0;
    var hasReqHdr = a.request_headers && Object.keys(a.request_headers).length > 0;
    var hasDetail = hasQuery || hasPost || hasReqHdr;

    // Main row
    html += '<tr>' +
      '<td style="text-align:center">' +
        (hasDetail ? '<button class="expand-btn" onclick="toggleDetail(\'' + uid + '\')" title="Request headers &amp; params">+</button>' : '') +
      '</td>' +
      '<td>' + methodBadge(a.method) + '</td>' +
      '<td class="url-cell"><a href="' + escHtml(a.url) + '" title="' + escHtml(a.url) + '">' +
        escHtml(truncUrl(a.url, 80)) + '</a></td>' +
      '<td style="color:var(--txt2);font-size:12px">' + escHtml(a.resource_type || '') + '</td>' +
      '<td class="' + httpStatusClass(a.status) + '">' + (a.status || '\u2014') + '</td>' +
      '<td class="' + durClass(a.duration) + '">' + fmsSafe(a.duration) + '</td>' +
      '<td>' + fb(a.size) + '</td>' +
    '</tr>';

    // Expandable detail row
    if (hasDetail) {
      var qpHtml  = kvTableHtml(a.query_params || {});
      var hdrHtml = kvTableHtml(a.request_headers || {});
      var postHtml = '<span class="no-data">None</span>';
      if (hasPost) {
        var pd = a.post_data.trim();
        try { pd = JSON.stringify(JSON.parse(pd), null, 2); } catch(e) {}
        postHtml = '<pre class="post-data-pre">' + escHtml(pd) + '</pre>';
      }
      html += '<tr class="detail-row" id="' + uid + '" style="display:none">' +
        '<td colspan="7"><div class="detail-inner">' +
          '<div class="detail-section"><h4>Request Headers</h4>' + hdrHtml + '</div>' +
          '<div class="detail-section"><h4>Query Parameters</h4>' + qpHtml + '</div>' +
          '<div class="detail-section"><h4>Request Body</h4>' + postHtml + '</div>' +
        '</div></td></tr>';
    }
  });

  return html + '</tbody>';
}

function toggleDetail(uid) {
  var row = document.getElementById(uid);
  if (!row) return;
  var visible = row.style.display !== 'none';
  row.style.display = visible ? 'none' : 'table-row';
  var mainRow = row.previousElementSibling;
  var btn = mainRow && mainRow.querySelector('.expand-btn');
  if (btn) btn.textContent = visible ? '+' : '\u2212';
}

function renderSlowApis() {
  var total = D.no_cache.slow_apis.length + D.with_cache.slow_apis.length;
  el('slow-apis-badge').textContent = total + (total === 1 ? ' call' : ' calls');
  el('slow-apis-nc-table').innerHTML = apiTableHtml(D.no_cache.slow_apis, D.slow_api_threshold);
  el('slow-apis-wc-table').innerHTML = apiTableHtml(D.with_cache.slow_apis, D.slow_api_threshold);
}

function renderAllApis() {
  var ncCount = D.no_cache.api_calls.length;
  var wcCount = D.with_cache.api_calls.length;
  el('all-apis-badge').textContent = ncCount + ' no-cache / ' + wcCount + ' with-cache';
  el('all-apis-nc-table').innerHTML = apiTableHtml(D.no_cache.api_calls, 0);
  el('all-apis-wc-table').innerHTML = apiTableHtml(D.with_cache.api_calls, 0);
}

// ═══════════════════════  RESOURCE TABLES  ═══════════════════════════
function resRow(r, showDuration, showSize) {
  var dur = showDuration ? '<td class="' + durClass(r.duration) + '">' + fmsSafe(r.duration) + '</td>' : '';
  var sz  = showSize ? '<td>' + fb(r.size) + '</td>' : '';
  var cache = r.from_cache ? '<span class="from-cache"> ✦ cached</span>' : '';
  return '<tr>' +
    '<td class="url-cell"><a href="' + r.name + '" title="' + r.name + '">' +
      resourceName(r.name) + cache + '</a></td>' +
    '<td><span class="method-badge method-other">' + (r.type || 'other').toUpperCase() + '</span></td>' +
    dur + sz + '</tr>';
}

function renderResourceTables() {
  var nc = D.no_cache;
  var total = nc.slow_resources.length + nc.large_resources.length;
  el('slow-res-badge').textContent = total + ' resources';

  // Slow
  var slowHtml = '<thead><tr><th>Resource</th><th>Type</th><th>Duration</th><th>Size</th></tr></thead><tbody>';
  if (!nc.slow_resources || nc.slow_resources.length === 0)
    slowHtml += emptyState('No resources took longer than 1 second to load.');
  else
    slowHtml += nc.slow_resources.map(function(r){ return resRow(r, true, true); }).join('');
  el('slow-res-table').innerHTML = slowHtml + '</tbody>';

  // Large
  var largeHtml = '<thead><tr><th>Resource</th><th>Type</th><th>Decoded Size</th><th>Transfer Size</th></tr></thead><tbody>';
  if (!nc.large_resources || nc.large_resources.length === 0)
    largeHtml += emptyState('No resources exceed 500 KB.');
  else
    largeHtml += nc.large_resources.map(function(r) {
      return '<tr><td class="url-cell"><a href="' + r.name + '" title="' + r.name + '">' + resourceName(r.name) + '</a></td>' +
        '<td><span class="method-badge method-other">' + (r.type||'other').toUpperCase() + '</span></td>' +
        '<td>' + fb(r.size) + '</td><td>' + fb(r.transfer_size) + '</td></tr>';
    }).join('');
  el('large-res-table').innerHTML = largeHtml + '</tbody>';

  // Render-blocking
  var blkHtml = '<thead><tr><th>Resource</th><th>Type</th><th>Duration</th><th>Start Time</th></tr></thead><tbody>';
  if (!nc.render_blocking || nc.render_blocking.length === 0)
    blkHtml += emptyState('No significant render-blocking resources detected.');
  else
    blkHtml += nc.render_blocking.map(function(r) {
      return '<tr><td class="url-cell"><a href="' + r.name + '" title="' + r.name + '">' + resourceName(r.name) + '</a></td>' +
        '<td><span class="method-badge method-other">' + (r.type||'other').toUpperCase() + '</span></td>' +
        '<td class="' + durClass(r.duration) + '">' + fmsSafe(r.duration) + '</td>' +
        '<td>' + fmsSafe(r.start_time) + '</td></tr>';
    }).join('');
  el('blocking-res-table').innerHTML = blkHtml + '</tbody>';
}

// ═══════════════════════  JMX EXPORT  ════════════════════════════════
function getPageName() {
  try {
    var path = new URL(D.page_url).pathname;
    var segs = path.split('/').filter(function(s){ return s.length > 0; });
    var raw  = segs.length > 0 ? decodeURIComponent(segs[segs.length - 1]) : 'Page';
    return raw.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'Page';
  } catch(e) { return 'Page'; }
}

function escXml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function _httpSamplerXml(a) {
  var method = (a.method || 'GET').toUpperCase();
  var parsed;
  try { parsed = new URL(a.url); }
  catch(e) {
    parsed = { hostname: '', port: '', protocol: 'http:', pathname: a.url, search: '' };
  }
  var domain = escXml(parsed.hostname);
  var port   = parsed.port || '';
  var proto  = (parsed.protocol || 'http:').replace(':', '');
  var path   = escXml(parsed.pathname);
  var label  = escXml(method + ' ' + (parsed.pathname || a.url));

  // Body vs. arguments section
  var argsSection;
  if (a.post_data && a.post_data.trim().length > 0) {
    argsSection =
      '            <boolProp name="HTTPSampler.postBodyRaw">true</boolProp>\n' +
      '            <elementProp name="HTTPsampler.Arguments" elementType="Arguments">\n' +
      '              <collectionProp name="Arguments.arguments">\n' +
      '                <elementProp name="" elementType="HTTPArgument">\n' +
      '                  <boolProp name="HTTPArgument.always_encode">false</boolProp>\n' +
      '                  <stringProp name="Argument.value">' + escXml(a.post_data.trim()) + '</stringProp>\n' +
      '                  <stringProp name="Argument.metadata">=</stringProp>\n' +
      '                </elementProp>\n' +
      '              </collectionProp>\n' +
      '            </elementProp>';
  } else {
    var params     = a.query_params || {};
    var paramKeys  = Object.keys(params);
    var argsInner  = paramKeys.map(function(k) {
      var v = Array.isArray(params[k]) ? params[k].join(',') : params[k];
      return (
        '                <elementProp name="' + escXml(k) + '" elementType="HTTPArgument">\n' +
        '                  <boolProp name="HTTPArgument.always_encode">true</boolProp>\n' +
        '                  <stringProp name="Argument.value">' + escXml(String(v)) + '</stringProp>\n' +
        '                  <stringProp name="Argument.metadata">=</stringProp>\n' +
        '                  <boolProp name="HTTPArgument.use_equals">true</boolProp>\n' +
        '                  <stringProp name="Argument.name">' + escXml(k) + '</stringProp>\n' +
        '                </elementProp>'
      );
    }).join('\n');
    argsSection =
      '            <elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" enabled="true">\n' +
      '              <collectionProp name="Arguments.arguments">\n' +
      (argsInner ? argsInner + '\n' : '') +
      '              </collectionProp>\n' +
      '            </elementProp>';
  }

  // Build header manager if request_headers exist
  var headers = a.request_headers || {};
  var headerKeys = Object.keys(headers);
  var headerManagerXml = '';
  
  if (headerKeys.length > 0) {
    var headersInner = headerKeys.map(function(k) {
      return (
        '                <elementProp name="" elementType="Header">\n' +
        '                  <stringProp name="Header.name">' + escXml(k) + '</stringProp>\n' +
        '                  <stringProp name="Header.value">' + escXml(String(headers[k])) + '</stringProp>\n' +
        '                </elementProp>'
      );
    }).join('\n');
    
    headerManagerXml =
      '            <HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="HTTP Header Manager" enabled="true">\n' +
      '              <collectionProp name="HeaderManager.headers">\n' +
      headersInner + '\n' +
      '              </collectionProp>\n' +
      '            </HeaderManager>\n' +
      '            <hashTree/>\n';
  }
  
  // Build response assertion for status code
  var statusCode = a.status || 200;
  var responseAssertionXml =
    '            <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="Response Code Assertion" enabled="true">\n' +
    '              <collectionProp name="Asserion.test_strings">\n' +
    '                <stringProp name="49586">' + statusCode + '</stringProp>\n' +
    '              </collectionProp>\n' +
    '              <stringProp name="Assertion.custom_message"></stringProp>\n' +
    '              <stringProp name="Assertion.test_field">Assertion.response_code</stringProp>\n' +
    '              <boolProp name="Assertion.assume_success">false</boolProp>\n' +
    '              <intProp name="Assertion.test_type">8</intProp>\n' +
    '            </ResponseAssertion>\n' +
    '            <hashTree/>';

  return (
    '          <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="' + label + '" enabled="true">\n' +
    argsSection + '\n' +
    '            <stringProp name="HTTPSampler.domain">' + domain + '</stringProp>\n' +
    '            <stringProp name="HTTPSampler.port">' + escXml(port) + '</stringProp>\n' +
    '            <stringProp name="HTTPSampler.protocol">' + proto + '</stringProp>\n' +
    '            <stringProp name="HTTPSampler.contentEncoding">UTF-8</stringProp>\n' +
    '            <stringProp name="HTTPSampler.path">' + path + '</stringProp>\n' +
    '            <stringProp name="HTTPSampler.method">' + method + '</stringProp>\n' +
    '            <boolProp name="HTTPSampler.follow_redirects">true</boolProp>\n' +
    '            <boolProp name="HTTPSampler.auto_redirects">false</boolProp>\n' +
    '            <boolProp name="HTTPSampler.use_keepalive">true</boolProp>\n' +
    '            <boolProp name="HTTPSampler.DO_MULTIPART_POST">false</boolProp>\n' +
    '          </HTTPSamplerProxy>\n' +
    '          <hashTree>\n' +
    headerManagerXml +
    responseAssertionXml + '\n' +
    '          </hashTree>'
  );
}

function buildJmx() {
  // Collect unique APIs from both runs (no-cache primary, with-cache fills gaps)
  var seen = {}, apis = [];
  function addRun(list) {
    (list || []).forEach(function(a) {
      var key = (a.method || 'GET').toUpperCase() + '|' + a.url;
      if (!seen[key]) { seen[key] = true; apis.push(a); }
    });
  }
  addRun(D.no_cache.api_calls);
  addRun(D.with_cache.api_calls);

  var txName   = getPageName();
  var samplers = apis.map(_httpSamplerXml).join('\n');

  return (
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">\n' +
    '  <hashTree>\n' +
    '    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="RapidRadar Test Plan" enabled="true">\n' +
    '      <stringProp name="TestPlan.comments">Generated by RapidRadar \u2013 OPUS EDM PERF TEAM</stringProp>\n' +
    '      <boolProp name="TestPlan.functional_mode">false</boolProp>\n' +
    '      <boolProp name="TestPlan.tearDown_on_shutdown">true</boolProp>\n' +
    '      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>\n' +
    '      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">\n' +
    '        <collectionProp name="Arguments.arguments"/>\n' +
    '      </elementProp>\n' +
    '    </TestPlan>\n' +
    '    <hashTree>\n' +
    '      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="RapidRadar" enabled="true">\n' +
    '        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>\n' +
    '        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">\n' +
    '          <boolProp name="LoopController.continue_forever">false</boolProp>\n' +
    '          <stringProp name="LoopController.loops">1</stringProp>\n' +
    '        </elementProp>\n' +
    '        <stringProp name="ThreadGroup.num_threads">1</stringProp>\n' +
    '        <stringProp name="ThreadGroup.ramp_time">1</stringProp>\n' +
    '        <boolProp name="ThreadGroup.scheduler">false</boolProp>\n' +
    '        <stringProp name="ThreadGroup.duration"></stringProp>\n' +
    '        <stringProp name="ThreadGroup.delay"></stringProp>\n' +
    '        <boolProp name="ThreadGroup.same_user_on_next_iteration">true</boolProp>\n' +
    '      </ThreadGroup>\n' +
    '      <hashTree>\n' +
    '        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="' + escXml(txName) + '" enabled="true">\n' +
    '          <boolProp name="TransactionController.includeTimers">false</boolProp>\n' +
    '          <boolProp name="TransactionController.parent">false</boolProp>\n' +
    '        </TransactionController>\n' +
    '        <hashTree>\n' +
    samplers + '\n' +
    '        </hashTree>\n' +
    '      </hashTree>\n' +
    '    </hashTree>\n' +
    '  </hashTree>\n' +
    '</jmeterTestPlan>'
  );
}

function convertToJmx() {
  var jmx     = buildJmx();
  var fname   = getPageName() + '_RapidRadar.jmx';
  var blob    = new Blob([jmx], { type: 'application/xml' });
  var objUrl  = URL.createObjectURL(blob);
  var link    = document.createElement('a');
  link.href     = objUrl;
  link.download = fname;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(objUrl);
}

// ═══════════════════════  ACCESSIBILITY  ═════════════════════════════
function renderAccessibility() {
  var a11y = D.accessibility;
  var badge = el('a11y-badge');
  var container = el('a11y-container');
  if (!a11y || a11y.score == null) {
    if (badge)     badge.textContent = '';
    if (container) container.innerHTML = '<div class="a11y-na">Accessibility audit was not run for this page.<br>Re-run without <code>--no-accessibility</code> to enable WCAG 2.1 AA auditing.</div>';
    return;
  }
  var score = a11y.score;
  function a11yGrade(s) {
    if (s>=90) return {grade:'A',label:'Excellent',  color:'var(--green)',  bg:'rgba(63,185,80,.12)',   border:'rgba(63,185,80,.35)'};
    if (s>=75) return {grade:'B',label:'Good',       color:'#4ec9b0',       bg:'rgba(78,201,176,.12)',  border:'rgba(78,201,176,.35)'};
    if (s>=50) return {grade:'C',label:'Needs Work', color:'var(--yellow)', bg:'rgba(210,153,34,.12)',  border:'rgba(210,153,34,.35)'};
    if (s>=25) return {grade:'D',label:'Poor',       color:'var(--orange)', bg:'rgba(227,179,65,.12)',  border:'rgba(227,179,65,.35)'};
    return        {grade:'F',label:'Critical',  color:'var(--red)',    bg:'rgba(248,81,73,.12)',    border:'rgba(248,81,73,.35)'};
  }
  var G = a11yGrade(score);
  var circ = 282.74;
  var arc  = (score / 100) * circ;
  if (badge) {
    badge.textContent = score + ' ' + G.grade;
    badge.style.background = G.bg;
    badge.style.color = G.color;
    badge.style.border = '1px solid ' + G.border;
    badge.style.borderRadius = '6px';
    badge.style.padding = '1px 8px';
    badge.style.fontWeight = '700';
    badge.style.fontSize = '12px';
  }
  var vcounts = a11y.violation_counts || {};
  var impacts = [
    {key:'critical', label:'Critical', color:'var(--red)'},
    {key:'serious',  label:'Serious',  color:'var(--orange)'},
    {key:'moderate', label:'Moderate', color:'var(--yellow)'},
    {key:'minor',    label:'Minor',    color:'var(--blue)'},
  ];
  var totalViols = a11y.total_violations || 0;
  var impactHtml = impacts.map(function(imp) {
    var cnt = vcounts[imp.key] || 0;
    var w = totalViols > 0 ? (cnt / totalViols * 100) : 0;
    return '<div class="a11y-impact-row">' +
      '<span class="a11y-impact-lbl" style="color:' + imp.color + '">' + imp.label + '</span>' +
      '<div class="a11y-impact-bar-wrap"><div class="a11y-impact-bar" style="width:' + w.toFixed(1) + '%;background:' + imp.color + '"></div></div>' +
      '<span class="a11y-impact-cnt" style="color:' + imp.color + '">' + cnt + '</span>' +
    '</div>';
  }).join('');
  var violations = a11y.violations || [];
  var violHtml = '';
  if (violations.length === 0) {
    violHtml = '<div style="color:var(--green);font-size:13px;padding:12px 0">✓ No violations found</div>';
  } else {
    var rows = violations.map(function(v) {
      var impCls = 'impact-' + (v.impact || 'minor');
      var nodes = (v.nodes || []).slice(0, 3).map(function(n) {
        return '<div class="a11y-node-pre">' + (n.html || '').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';
      }).join('');
      var tags = (v.tags || []).map(function(t) {
        return '<span class="a11y-tag">' + t + '</span>';
      }).join('');
      return '<tr>' +
        '<td><span class="' + impCls + '">' + (v.impact || '—') + '</span></td>' +
        '<td><strong>' + (v.id || '') + '</strong><br><span style="color:var(--txt2);font-size:11px">' + (v.description || '') + '</span>' +
          (tags ? '<div class="a11y-tags">' + tags + '</div>' : '') +
          (v.helpUrl ? '<div><a class="a11y-help-link" href="' + v.helpUrl + '" target="_blank">Learn more ↗</a></div>' : '') +
        '</td>' +
        '<td>' + (v.nodes||[]).length + ' node' + ((v.nodes||[]).length!==1?'s':'') + nodes + '</td>' +
      '</tr>';
    }).join('');
    violHtml = '<div class="a11y-violations-table"><table class="data-table">' +
      '<thead><tr><th>Impact</th><th>Rule / Description</th><th>Affected Nodes</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>';
  }
  var passCount  = (a11y.passes  || []).length;
  var incomCount = (a11y.incomplete || []).length;
  container.innerHTML =
    '<div class="a11y-layout">' +
      '<div class="a11y-gauge-wrap">' +
        '<svg width="160" height="160" viewBox="0 0 110 110" style="display:block">' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="var(--bg3)" stroke-width="10"/>' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="' + G.color + '" stroke-width="10"' +
          ' stroke-dasharray="' + arc.toFixed(2) + ' ' + circ.toFixed(2) + '"' +
          ' stroke-linecap="round" transform="rotate(-90 55 55)"/>' +
          '<text x="55" y="49" text-anchor="middle" font-size="24" font-weight="700" fill="' + G.color + '">' + score + '</text>' +
          '<text x="55" y="65" text-anchor="middle" font-size="13" font-weight="700" fill="' + G.color + '">' + G.grade + '</text>' +
          '<text x="55" y="78" text-anchor="middle" font-size="9" fill="var(--txt2)">' + G.label + '</text>' +
        '</svg>' +
        '<div class="a11y-gauge-meta">' +
          '<div class="a11y-gauge-title">Accessibility Score</div>' +
          '<div class="a11y-gauge-sub">WCAG 2.1 AA</div>' +
        '</div>' +
      '</div>' +
      '<div class="a11y-right">' +
        '<div>' +
          '<div class="a11y-sub-title">Violation Breakdown (' + totalViols + ' total)</div>' +
          impactHtml +
          '<div style="font-size:11px;color:var(--txt3);margin-top:6px">' +
            '✓ ' + passCount + ' rules passed &nbsp;|&nbsp; ⚠ ' + incomCount + ' incomplete' +
          '</div>' +
        '</div>' +
        '<div>' +
          '<div class="a11y-sub-title">Violations Detail</div>' +
          violHtml +
        '</div>' +
      '</div>' +
    '</div>';
}

// ═══════════════════════  PERFORMANCE SCORE  ═════════════════════════════
function computeScore() {
  var timing = D.no_cache.timing;
  var slow   = D.no_cache.slow_apis.length;
  function safe(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }
  var lt = safe(timing.total_load_time);
  var sLT = lt<=1000?100:lt<=2000?90:lt<=3000?75:lt<=5000?50:lt<=8000?25:10;
  var ttfb = safe(timing.ttfb);
  var sTTFB = ttfb<=200?100:ttfb<=600?80:ttfb<=1000?50:20;
  var fcp = timing.fcp; var sFCP = fcp==null?80:safe(fcp)<=1800?100:safe(fcp)<=3000?70:30;
  var lcp = timing.lcp; var sLCP = lcp==null?80:safe(lcp)<=2500?100:safe(lcp)<=4000?70:20;
  var clsV = timing.cls!=null?safe(timing.cls):0;
  var sCLS = clsV<=0.1?100:clsV<=0.25?70:30;
  var sAPI = slow===0?100:slow<=2?70:slow<=5?40:10;
  var ps = D.no_cache.total_size||0; var sPS = ps<=1048576?100:ps<=3145728?80:ps<=5242880?60:ps<=10485760?30:10;
  return Math.min(100,Math.max(0,Math.round(sLT*0.30+sLCP*0.20+sAPI*0.10+sTTFB*0.10+sFCP*0.10+sCLS*0.10+sPS*0.10)));
}
function scoreGrade(s) {
  if (s>=90) return {grade:'A',label:'Excellent',  color:'var(--green)',  bg:'rgba(63,185,80,.12)',   border:'rgba(63,185,80,.35)'};
  if (s>=75) return {grade:'B',label:'Good',       color:'#4ec9b0',       bg:'rgba(78,201,176,.12)',  border:'rgba(78,201,176,.35)'};
  if (s>=50) return {grade:'C',label:'Needs Work', color:'var(--yellow)', bg:'rgba(210,153,34,.12)',  border:'rgba(210,153,34,.35)'};
  if (s>=25) return {grade:'D',label:'Poor',       color:'var(--orange)', bg:'rgba(227,179,65,.12)',  border:'rgba(227,179,65,.35)'};
  return        {grade:'F',label:'Critical',  color:'var(--red)',    bg:'rgba(248,81,73,.12)',    border:'rgba(248,81,73,.35)'};
}
function renderScore() {
  var score  = computeScore();
  var G      = scoreGrade(score);
  var circ   = 282.74;
  var arc    = (score / 100) * circ;
  var timing = D.no_cache.timing;
  var slow   = D.no_cache.slow_apis.length;
  function safe(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }
  function valStr(v) { return v==null?'N/A':Math.round(safe(v))+' ms'; }
  function barColor(v) { return v>=90?'var(--green)':v>=70?'#4ec9b0':v>=50?'var(--yellow)':v>=25?'var(--orange)':'var(--red)'; }
  var lt = safe(timing.total_load_time);
  var sLT  = lt<=1000?100:lt<=2000?90:lt<=3000?75:lt<=5000?50:lt<=8000?25:10;
  var ttfb = safe(timing.ttfb);
  var sTTFB = ttfb<=200?100:ttfb<=600?80:ttfb<=1000?50:20;
  var sFCP = timing.fcp==null?80:safe(timing.fcp)<=1800?100:safe(timing.fcp)<=3000?70:30;
  var sLCP = timing.lcp==null?80:safe(timing.lcp)<=2500?100:safe(timing.lcp)<=4000?70:20;
  var clsV = timing.cls!=null?safe(timing.cls):0;
  var sCLS = clsV<=0.1?100:clsV<=0.25?70:30;
  var sAPI = slow===0?100:slow<=2?70:slow<=5?40:10;
  var ps = D.no_cache.total_size||0; var sPS = ps<=1048576?100:ps<=3145728?80:ps<=5242880?60:ps<=10485760?30:10;
  function fbSize(b) { return b==null||b===0?'N/A':b<1048576?(b/1024).toFixed(0)+' KB':(b/1048576).toFixed(1)+' MB'; }
  var components = [
    {label:'Load Time', weight:'30%', sc:sLT,  raw:valStr(timing.total_load_time)},
    {label:'LCP',       weight:'20%', sc:sLCP, raw:valStr(timing.lcp)},
    {label:'Slow APIs', weight:'10%', sc:sAPI, raw:slow+' call'+(slow!==1?'s':'')},
    {label:'TTFB',      weight:'10%', sc:sTTFB,raw:valStr(timing.ttfb)},
    {label:'FCP',       weight:'10%', sc:sFCP, raw:valStr(timing.fcp)},
    {label:'CLS',       weight:'10%', sc:sCLS, raw:timing.cls!=null?parseFloat(timing.cls).toFixed(3):'N/A'},
    {label:'Page Size', weight:'10%', sc:sPS,  raw:fbSize(D.no_cache.total_size)},
  ];
  var breakdown = components.map(function(c) {
    var bc = barColor(c.sc);
    return '<div class="score-row">' +
      '<span class="score-row-label">' + c.label + '<span class="score-row-weight"> (' + c.weight + ')</span></span>' +
      '<div class="score-row-bar-wrap"><div class="score-row-bar" style="width:' + c.sc + '%;background:' + bc + '"></div></div>' +
      '<span class="score-row-val" style="color:' + bc + '">' + c.sc + '</span>' +
      '<span class="score-row-metric">' + c.raw + '</span>' +
    '</div>';
  }).join('');
  el('perf-score-container').innerHTML =
    '<div class="score-layout">' +
      '<div class="score-gauge-wrap">' +
        '<svg width="160" height="160" viewBox="0 0 110 110" style="display:block">' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="var(--bg3)" stroke-width="10"/>' +
          '<circle cx="55" cy="55" r="45" fill="none" stroke="' + G.color + '" stroke-width="10"' +
          ' stroke-dasharray="' + arc.toFixed(2) + ' ' + circ.toFixed(2) + '"' +
          ' stroke-linecap="round" transform="rotate(-90 55 55)"/>' +
          '<text x="55" y="49" text-anchor="middle" font-size="24" font-weight="700" fill="' + G.color + '">' + score + '</text>' +
          '<text x="55" y="65" text-anchor="middle" font-size="13" font-weight="700" fill="' + G.color + '">' + G.grade + '</text>' +
          '<text x="55" y="78" text-anchor="middle" font-size="9" fill="var(--txt2)">' + G.label + '</text>' +
        '</svg>' +
        '<div class="score-gauge-meta">' +
          '<div class="score-gauge-title">Performance Score</div>' +
          '<div class="score-gauge-sub">Based on no-cache run</div>' +
        '</div>' +
      '</div>' +
      '<div class="score-breakdown">' +
        '<div class="score-breakdown-title">Component Breakdown</div>' +
        breakdown +
      '</div>' +
    '</div>';
}

// ═══════════════════════  THEME  ══════════════════════════════════════
function getAutoTheme() {
  var h = new Date().getHours();
  return (h >= 6 && h < 20) ? 'light' : 'dark';
}
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : '');
  var icon  = document.getElementById('theme-icon');
  var label = document.getElementById('theme-label');
  if (icon)  icon.textContent  = t === 'light' ? '🌙' : '☀️';
  if (label) label.textContent = t === 'light' ? 'Night' : 'Day';
}
function toggleTheme() {
  var cur  = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  var next = cur === 'light' ? 'dark' : 'light';
  localStorage.setItem('rr-theme', next);
  applyTheme(next);
  renderCharts();
}

// ═══════════════════════════  BOOT  ══════════════════════════════════
(function init() {
  // Apply theme: saved preference or auto-detect from time of day
  var savedTheme = localStorage.getItem('rr-theme');
  applyTheme(savedTheme || getAutoTheme());

  renderHeader();
  renderCards();
  renderScore();
  renderAccessibility();
  renderTimingTable();
  renderVitals('vitals-grid-nc', D.no_cache.timing);
  renderVitals('vitals-grid-wc', D.with_cache.timing);
  renderIssues();
  renderSlowApis();
  renderAllApis();
  renderResourceTables();

  // Charts need DOM to be ready  – they are already in DOM at this point
  renderCharts();

  // Initialise tab pane visibility (CSS-only issue: first pane shown, rest hidden)
  document.querySelectorAll('.tab-pane:not(.active)').forEach(function(p) {
    p.style.display = 'none';
  });
})();
</script>
</body>
</html>"""
