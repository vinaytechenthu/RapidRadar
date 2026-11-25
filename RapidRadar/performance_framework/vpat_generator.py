"""
performance_framework/vpat_generator.py
========================================
Generates a VPAT 2.4 (WCAG 2.1 Edition) accessibility conformance report
from axe-core scan results collected during a RapidRadar test run.

Usage
-----
    from performance_framework.vpat_generator import generate_vpat_from_folder
    generate_vpat_from_folder(report_folder, output_file)
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote


# ---------------------------------------------------------------------------
# WCAG 2.1 criteria definition
# Each tuple: (criterion_id, level, name, description)
# ---------------------------------------------------------------------------
WCAG_A: List[Tuple[str, str, str, str]] = [
    ("1.1.1", "A", "Non-text Content",
     "All non-text content has a text alternative that serves the equivalent purpose."),
    ("1.2.1", "A", "Audio-only and Video-only (Prerecorded)",
     "Prerecorded audio-only or video-only content has an alternative."),
    ("1.2.2", "A", "Captions (Prerecorded)",
     "Captions are provided for all prerecorded audio content in synchronized media."),
    ("1.2.3", "A", "Audio Description or Media Alternative (Prerecorded)",
     "An alternative for time-based media or audio description is provided."),
    ("1.3.1", "A", "Info and Relationships",
     "Information, structure, and relationships conveyed through presentation can be programmatically determined."),
    ("1.3.2", "A", "Meaningful Sequence",
     "The correct reading sequence can be programmatically determined."),
    ("1.3.3", "A", "Sensory Characteristics",
     "Instructions provided rely on more than shape, size, visual location, orientation, or sound."),
    ("1.4.1", "A", "Use of Color",
     "Color is not the only visual means of conveying information or prompting a response."),
    ("1.4.2", "A", "Audio Control",
     "Audio that plays automatically can be stopped or controlled."),
    ("2.1.1", "A", "Keyboard",
     "All functionality is operable through a keyboard interface."),
    ("2.1.2", "A", "No Keyboard Trap",
     "Keyboard focus can be moved away from any component using only the keyboard."),
    ("2.1.4", "A", "Character Key Shortcuts",
     "Keyboard shortcuts using only letter, punctuation, number, or symbol characters can be remapped or turned off."),
    ("2.2.1", "A", "Timing Adjustable",
     "Time limits can be turned off, adjusted, or extended."),
    ("2.2.2", "A", "Pause, Stop, Hide",
     "Moving, blinking, scrolling, or auto-updating content can be paused, stopped, or hidden."),
    ("2.3.1", "A", "Three Flashes or Below Threshold",
     "Content does not flash more than three times per second."),
    ("2.4.1", "A", "Bypass Blocks",
     "A mechanism is available to bypass blocks of content that are repeated on multiple pages."),
    ("2.4.2", "A", "Page Titled",
     "Web pages have titles that describe topic or purpose."),
    ("2.4.3", "A", "Focus Order",
     "Focusable components receive focus in an order that preserves meaning and operability."),
    ("2.4.4", "A", "Link Purpose (In Context)",
     "The purpose of each link can be determined from the link text alone or from its context."),
    ("2.5.1", "A", "Pointer Gestures",
     "All functionality that uses multipoint or path-based gestures can be operated with a single pointer."),
    ("2.5.2", "A", "Pointer Cancellation",
     "For single-pointer functionality, at least one of the following is true: no down-event, abort/undo, up reversal, or essential."),
    ("2.5.3", "A", "Label in Name",
     "For UI components with text labels, the accessible name contains the visible text."),
    ("2.5.4", "A", "Motion Actuation",
     "Functionality that can be operated by device or user motion can also be operated by UI components."),
    ("3.1.1", "A", "Language of Page",
     "The default human language of each Web page can be programmatically determined."),
    ("3.2.1", "A", "On Focus",
     "Receiving focus does not initiate a change of context."),
    ("3.2.2", "A", "On Input",
     "Changing the setting of any UI component does not automatically cause a change of context."),
    ("3.3.1", "A", "Error Identification",
     "Input errors are identified and described in text."),
    ("3.3.2", "A", "Labels or Instructions",
     "Labels or instructions are provided when content requires user input."),
    ("4.1.1", "A", "Parsing",
     "Content implemented using markup languages has no major parsing errors."),
    ("4.1.2", "A", "Name, Role, Value",
     "For UI components, name and role can be programmatically determined; states, properties, and values can be set."),
]

WCAG_AA: List[Tuple[str, str, str, str]] = [
    ("1.2.4", "AA", "Captions (Live)",
     "Captions are provided for all live audio content in synchronized media."),
    ("1.2.5", "AA", "Audio Description (Prerecorded)",
     "Audio description is provided for all prerecorded video content in synchronized media."),
    ("1.3.4", "AA", "Orientation",
     "Content does not restrict its view and operation to a single display orientation."),
    ("1.3.5", "AA", "Identify Input Purpose",
     "The purpose of each input field collecting personal information can be programmatically determined."),
    ("1.4.3", "AA", "Contrast (Minimum)",
     "Text has a contrast ratio of at least 4.5:1 (3:1 for large text)."),
    ("1.4.4", "AA", "Resize Text",
     "Text can be resized up to 200 percent without loss of content or functionality."),
    ("1.4.5", "AA", "Images of Text",
     "Text is used to convey information rather than images of text."),
    ("1.4.10", "AA", "Reflow",
     "Content can reflow without loss of information or functionality, without scrolling in two dimensions."),
    ("1.4.11", "AA", "Non-text Contrast",
     "UI components and graphical objects have a contrast ratio of at least 3:1."),
    ("1.4.12", "AA", "Text Spacing",
     "No loss of content occurs when letter spacing, word spacing, line height, and paragraph spacing are modified."),
    ("1.4.13", "AA", "Content on Hover or Focus",
     "Where additional content appears on hover or focus, it is dismissible, hoverable, and persistent."),
    ("2.4.5", "AA", "Multiple Ways",
     "More than one way is available to locate a Web page within a set of Web pages."),
    ("2.4.6", "AA", "Headings and Labels",
     "Headings and labels describe topic or purpose."),
    ("2.4.7", "AA", "Focus Visible",
     "Any keyboard operable UI has a visible keyboard focus indicator."),
    ("3.1.2", "AA", "Language of Parts",
     "The human language of each passage or phrase can be programmatically determined."),
    ("3.2.3", "AA", "Consistent Navigation",
     "Navigational mechanisms repeated on multiple pages occur in the same relative order."),
    ("3.2.4", "AA", "Consistent Identification",
     "Components that have the same functionality are identified consistently."),
    ("3.3.3", "AA", "Error Suggestion",
     "If an error is detected, suggestions for correction are provided."),
    ("3.3.4", "AA", "Error Prevention (Legal, Financial, Data)",
     "Submissions can be checked, confirmed, or reversed."),
    ("4.1.3", "AA", "Status Messages",
     "Status messages can be programmatically determined through role or properties."),
]


# ---------------------------------------------------------------------------
# axe-core rule → WCAG criteria mapping
# Each entry: rule_id → list of criterion IDs it affects
# ---------------------------------------------------------------------------
AXE_TO_WCAG: Dict[str, List[str]] = {
    # Perceivable
    "image-alt":                     ["1.1.1"],
    "input-image-alt":               ["1.1.1"],
    "object-alt":                    ["1.1.1"],
    "role-img-alt":                  ["1.1.1"],
    "svg-img-alt":                   ["1.1.1"],
    "area-alt":                      ["1.1.1"],
    "video-caption":                 ["1.2.2"],
    "audio-caption":                 ["1.2.2"],
    "region":                        ["1.3.1"],
    "landmark-one-main":             ["1.3.1"],
    "landmark-main-is-top-level":    ["1.3.1"],
    "landmark-no-duplicate-main":    ["1.3.1"],
    "landmark-no-duplicate-banner":  ["1.3.1"],
    "landmark-no-duplicate-contentinfo": ["1.3.1"],
    "landmark-complementary-is-top-level": ["1.3.1"],
    "heading-order":                 ["1.3.1"],
    "label":                         ["1.3.1", "4.1.2"],
    "label-content-name-mismatch":   ["2.5.3"],
    "autocomplete-valid":            ["1.3.5"],
    "color-contrast":                ["1.4.3"],
    "color-contrast-enhanced":       ["1.4.6"],
    # Operable
    "keyboard":                      ["2.1.1"],
    "focusable-content":             ["2.1.1"],
    "scrollable-region-focusable":   ["2.1.1"],
    "focus-trap":                    ["2.1.2"],
    "bypass":                        ["2.4.1"],
    "skip-link":                     ["2.4.1"],
    "document-title":                ["2.4.2"],
    "frame-title":                   ["2.4.1", "4.1.2"],
    "frame-title-unique":            ["2.4.1", "4.1.2"],
    "link-name":                     ["2.4.4", "4.1.2"],
    "empty-heading":                 ["2.4.6"],
    "focus-visible":                 ["2.4.7"],
    # Understandable
    "html-has-lang":                 ["3.1.1"],
    "html-lang-valid":               ["3.1.1"],
    "html-xml-lang-mismatch":        ["3.1.1"],
    "valid-lang":                    ["3.1.2"],
    # Robust
    "duplicate-id":                  ["4.1.1"],
    "duplicate-id-active":           ["4.1.1"],
    "duplicate-id-aria":             ["4.1.1"],
    "aria-allowed-attr":             ["4.1.2"],
    "aria-allowed-role":             ["4.1.2"],
    "aria-command-name":             ["4.1.2"],
    "aria-hidden-body":              ["4.1.2"],
    "aria-hidden-focus":             ["4.1.2"],
    "aria-input-field-name":         ["4.1.2"],
    "aria-meter-name":               ["4.1.2"],
    "aria-progressbar-name":         ["4.1.2"],
    "aria-required-attr":            ["4.1.2"],
    "aria-required-children":        ["4.1.2"],
    "aria-required-parent":          ["4.1.2"],
    "aria-roles":                    ["4.1.2"],
    "aria-toggle-field-name":        ["4.1.2"],
    "aria-tooltip-name":             ["4.1.2"],
    "aria-valid-attr":               ["4.1.2"],
    "aria-valid-attr-value":         ["4.1.2"],
    "button-name":                   ["4.1.2"],
    "input-button-name":             ["4.1.2"],
    "combobox-name":                 ["4.1.2"],
    "select-name":                   ["4.1.2"],
    "td-headers-attr":               ["4.1.2"],
    "th-has-data-cells":             ["4.1.2"],
    "table-duplicate-name":          ["4.1.2"],
}

# Criteria that are Not Applicable for a standard web application
# (e.g., no audio/video content, no prerecorded media)
NOT_APPLICABLE_DEFAULT: List[str] = [
    "1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5",  # media
    "1.4.2",   # audio control (no auto-play audio expected)
    "2.1.4",   # character key shortcuts (no custom keyboard shortcuts declared)
    "2.2.1", "2.2.2",   # timing (no time limits in the app)
    "2.3.1",   # flashes
    "2.5.1", "2.5.2", "2.5.4",  # pointer/motion gestures (desktop web app)
]

# Criteria that require manual testing (cannot be fully automated)
MANUAL_REVIEW_DEFAULT: List[str] = [
    "1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5",
    "1.3.2",   # meaningful sequence
    "1.3.3",   # sensory characteristics
    "1.4.1",   # use of color
    "1.4.4",   # resize text
    "1.4.5",   # images of text
    "1.4.10",  # reflow
    "1.4.13",  # hover/focus content
    "2.1.1",   # keyboard (only partial coverage by axe)
    "2.1.2",   # no keyboard trap
    "2.2.1", "2.2.2",
    "2.4.1",   # bypass blocks
    "2.4.3",   # focus order
    "2.4.5",   # multiple ways
    "2.4.7",   # focus visible
    "2.5.3",   # label in name
    "3.2.1", "3.2.2",  # on focus / on input
    "3.2.3", "3.2.4",  # consistent navigation / identification
    "3.3.1", "3.3.2",  # error identification / instructions
    "3.3.3", "3.3.4",  # error suggestion / prevention
]


# ---------------------------------------------------------------------------
# Helper: extract JSON data blob from a detail HTML report
# ---------------------------------------------------------------------------
def _extract_report_data(html_path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(html_path, encoding="utf-8") as fh:
            html = fh.read()
        if "const D = " not in html:
            return None
        start = html.index("const D = ") + 10
        depth = 0
        i = start
        while i < len(html):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        return json.loads(html[start: i + 1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core: collect accessibility findings from a report folder
# ---------------------------------------------------------------------------
def collect_findings(report_folder: str) -> Dict[str, Any]:
    """
    Scan all per-page HTML reports in *report_folder* and aggregate the
    axe-core violations.  Returns a dict with:
        pages          – list of page dicts (url, score, violations)
        violations     – dict rule_id → aggregated violation info
        summary_data   – parsed summary JSON (or {})
        edm_version    – EDM version string
        timestamp      – report timestamp string
    """
    html_files = [
        f for f in glob.glob(os.path.join(report_folder, "**", "*.html"), recursive=True)
        if "summary" not in os.path.basename(f)
    ]

    # Load summary JSON if present
    json_files = glob.glob(os.path.join(report_folder, "*_data.json"))
    summary_data: Dict[str, Any] = {}
    if json_files:
        try:
            with open(json_files[0], encoding="utf-8") as fh:
                summary_data = json.load(fh)
        except Exception:
            pass

    all_violations: Dict[str, Dict[str, Any]] = {}
    pages: List[Dict[str, Any]] = []

    for path in html_files:
        data = _extract_report_data(path)
        if not data:
            continue
        a11y = data.get("accessibility", {})
        pages.append({
            "page_url": data.get("page_url", ""),
            "page_name": unquote(data.get("page_url", "").split("/")[-1] or "Home"),
            "browser": data.get("browser_display", ""),
            "score": a11y.get("score"),
            "total_violations": a11y.get("total_violations", 0),
            "violation_counts": a11y.get("violation_counts", {}),
        })
        for v in a11y.get("violations", []):
            vid = v["id"]
            if vid not in all_violations:
                all_violations[vid] = {
                    "impact": v["impact"],
                    "description": v["description"],
                    "help": v.get("help", ""),
                    "helpUrl": v.get("helpUrl", ""),
                    "tags": v.get("tags", []),
                    "pages": [],
                    "sample_nodes": [],
                }
            all_violations[vid]["pages"].append(data.get("page_url", ""))
            # Keep up to 3 sample nodes per violation
            existing = all_violations[vid]["sample_nodes"]
            for node in v.get("nodes", []):
                if len(existing) < 3:
                    existing.append({
                        "html": node.get("html", ""),
                        "failureSummary": node.get("failureSummary", ""),
                        "page_url": data.get("page_url", ""),
                    })

    return {
        "pages": pages,
        "violations": all_violations,
        "summary_data": summary_data,
        "edm_version": summary_data.get("edm_version", ""),
        "timestamp": summary_data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M")),
    }


# ---------------------------------------------------------------------------
# Core: build conformance table
# ---------------------------------------------------------------------------
def _conformance_for_criterion(
    criterion_id: str,
    violations: Dict[str, Any],
    page_count: int,
) -> Tuple[str, str]:
    """
    Returns (conformance_level, remarks) for a single WCAG criterion.
    """
    # Determine which violations map to this criterion
    matching: List[Dict[str, Any]] = []
    for rule_id, mapped_criteria in AXE_TO_WCAG.items():
        if criterion_id in mapped_criteria and rule_id in violations:
            matching.append({"rule_id": rule_id, **violations[rule_id]})

    if criterion_id in NOT_APPLICABLE_DEFAULT:
        return "Not Applicable", "This criterion does not apply to this web application."

    if not matching:
        # Criterion is in manual review list – mark accordingly
        if criterion_id in MANUAL_REVIEW_DEFAULT:
            return (
                "Not Evaluated",
                "Automated scanning could not fully verify this criterion. "
                "Manual testing is recommended.",
            )
        return "Supports", "No violations detected by automated accessibility scan."

    # Build remark
    pages_affected: set = set()
    max_impact = "minor"
    impact_order = {"critical": 4, "serious": 3, "moderate": 2, "minor": 1}
    rule_names: List[str] = []

    for m in matching:
        pages_affected.update(m["pages"])
        if impact_order.get(m["impact"], 0) > impact_order.get(max_impact, 0):
            max_impact = m["impact"]
        rule_names.append(m["rule_id"])

    pct = int(len(pages_affected) / page_count * 100) if page_count else 0
    is_best_practice = all("best-practice" in v.get("tags", []) for v in matching)

    if max_impact in ("critical", "serious") and not is_best_practice:
        level = "Does Not Support"
    elif max_impact in ("critical", "serious", "moderate") or (pct >= 50 and not is_best_practice):
        level = "Partially Supports"
    else:
        level = "Partially Supports"

    remarks = (
        f"Automated scan detected issue(s) on {len(pages_affected)} of {page_count} pages "
        f"({pct}%% of tested pages). "
        f"Rule(s): {', '.join(rule_names)}. "
    )
    if is_best_practice:
        remarks += (
            "This finding is classified as a best-practice recommendation "
            "rather than a strict WCAG violation. Manual review is advised."
        )
    return level, remarks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def generate_vpat_from_folder(report_folder: str, output_file: str) -> None:
    """
    Read all page-level HTML reports in *report_folder*, aggregate
    accessibility findings, and write a VPAT 2.4 (WCAG 2.1) HTML report
    to *output_file*.
    """
    findings = collect_findings(report_folder)
    generate_vpat(findings, output_file)


def generate_vpat(findings: Dict[str, Any], output_file: str) -> None:
    """
    Write the VPAT HTML report from pre-collected *findings*
    (as returned by :func:`collect_findings`).
    """
    violations = findings["violations"]
    pages = findings["pages"]
    page_count = len(pages)
    edm_version = findings.get("edm_version", "")
    timestamp = findings.get("timestamp", "")
    product_name = f"EDM{' v' + edm_version if edm_version else ''}"

    # Build conformance rows
    def _rows(criteria_list: List[Tuple]) -> List[Dict[str, str]]:
        rows = []
        for cid, level, name, desc in criteria_list:
            conf, remarks = _conformance_for_criterion(cid, violations, page_count)
            rows.append({
                "id": cid, "level": level, "name": name,
                "description": desc, "conformance": conf, "remarks": remarks,
            })
        return rows

    rows_a = _rows(WCAG_A)
    rows_aa = _rows(WCAG_AA)

    # Summary counts
    def _count(rows: List[Dict]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for r in rows:
            counts[r["conformance"]] = counts.get(r["conformance"], 0) + 1
        return counts

    counts_a = _count(rows_a)
    counts_aa = _count(rows_aa)

    html = _HTML_TEMPLATE % {
        "product_name": product_name,
        "timestamp": timestamp,
        "page_count": page_count,
        "rows_a_json": json.dumps(rows_a, ensure_ascii=False),
        "rows_aa_json": json.dumps(rows_aa, ensure_ascii=False),
        "violations_json": json.dumps(violations, ensure_ascii=False),
        "pages_json": json.dumps(pages, ensure_ascii=False),
        "counts_a_json": json.dumps(counts_a, ensure_ascii=False),
        "counts_aa_json": json.dumps(counts_aa, ensure_ascii=False),
    }

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>VPAT 2.4 – %(product_name)s</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PGNpcmNsZSBjeD0iMTYiIGN5PSIxNiIgcj0iMTYiIGZpbGw9IiMwZDExMTciLz48Y2lyY2xlIGN4PSIxNiIgY3k9IjE2IiByPSIxMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjNThhNmZmIiBzdHJva2Utd2lkdGg9IjEuNSIgb3BhY2l0eT0iMC4zNSIvPjxjaXJjbGUgY3g9IjE2IiBjeT0iMTYiIHI9IjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzU4YTZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIG9wYWNpdHk9IjAuNiIvPjxjaXJjbGUgY3g9IjE2IiBjeT0iMTYiIHI9IjIuMiIgZmlsbD0iIzU4YTZmZiIvPjxsaW5lIHgxPSIxNiIgeTE9IjE2IiB4Mj0iMjciIHkyPSI3IiBzdHJva2U9IiMzZmI5NTAiIHN0cm9rZS13aWR0aD0iMS44IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz48Y2lyY2xlIGN4PSIyMy41IiBjeT0iOS41IiByPSIyLjgiIGZpbGw9IiMzZmI5NTAiIG9wYWNpdHk9IjAuOSIvPjxjaXJjbGUgY3g9IjExIiBjeT0iMjIiIHI9IjEuOCIgZmlsbD0iIzU4YTZmZiIgb3BhY2l0eT0iMC40NSIvPjwvc3ZnPg==">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6}
a{color:#58a6ff;text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:1200px;margin:0 auto;padding:24px 32px}
/* Header */
.header{background:linear-gradient(135deg,#161b22 0%%,#0d1117 100%%);border:1px solid #30363d;border-radius:12px;padding:32px;margin-bottom:24px}
.header-top{display:flex;align-items:center;gap:16px;margin-bottom:20px}
.logo{font-size:28px;font-weight:800;color:#58a6ff;letter-spacing:-0.5px}
.logo span{color:#3fb950}
.header h1{font-size:22px;font-weight:700;color:#e6edf3;margin-bottom:4px}
.header .subtitle{color:#8b949e;font-size:13px}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:20px}
.meta-card{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 16px}
.meta-card .label{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.meta-card .value{font-size:15px;font-weight:600;color:#e6edf3}
/* Tabs */
.tabs{display:flex;gap:4px;margin-bottom:20px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:4px}
.tab-btn{flex:1;padding:8px 16px;background:transparent;border:none;border-radius:6px;color:#8b949e;font-size:13px;font-weight:500;cursor:pointer;transition:all .2s}
.tab-btn.active{background:#21262d;color:#e6edf3}
.tab-btn:hover:not(.active){background:#1c2128;color:#c9d1d9}
.tab-pane{display:none}
.tab-pane.active{display:block}
/* Conformance summary */
.conf-summary{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.conf-badge{display:flex;align-items:center;gap:8px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 16px}
.conf-badge .dot{width:10px;height:10px;border-radius:50%%;flex-shrink:0}
.conf-badge .cnt{font-size:20px;font-weight:700;color:#e6edf3}
.conf-badge .lbl{font-size:12px;color:#8b949e}
/* Table */
.wcag-table{width:100%%;border-collapse:collapse;font-size:13px}
.wcag-table th{background:#161b22;color:#8b949e;font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.5px;padding:10px 14px;border-bottom:2px solid #30363d;text-align:left;position:sticky;top:0;z-index:1}
.wcag-table td{padding:10px 14px;border-bottom:1px solid #21262d;vertical-align:top}
.wcag-table tr:hover td{background:rgba(88,166,255,.04)}
.crit-id{font-family:'SFMono-Regular',Consolas,monospace;font-weight:700;color:#58a6ff;white-space:nowrap}
.crit-name{font-weight:600;color:#e6edf3}
.crit-desc{color:#8b949e;font-size:12px;margin-top:2px}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.badge-supports{background:rgba(63,185,80,.15);color:#3fb950;border:1px solid rgba(63,185,80,.3)}
.badge-partial{background:rgba(210,153,34,.15);color:#d29922;border:1px solid rgba(210,153,34,.3)}
.badge-notsupport{background:rgba(248,81,73,.15);color:#f85149;border:1px solid rgba(248,81,73,.3)}
.badge-na{background:rgba(139,148,158,.1);color:#8b949e;border:1px solid rgba(139,148,158,.2)}
.badge-noteval{background:rgba(88,166,255,.1);color:#58a6ff;border:1px solid rgba(88,166,255,.2)}
.remarks{color:#8b949e;font-size:12px}
/* Violations */
.viol-card{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:12px;overflow:hidden}
.viol-header{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;user-select:none}
.viol-header:hover{background:#1c2128}
.viol-id{font-family:'SFMono-Regular',Consolas,monospace;font-size:13px;font-weight:700;color:#e6edf3}
.viol-desc{color:#8b949e;font-size:12px;flex:1}
.viol-body{padding:16px;border-top:1px solid #21262d}
.node-card{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 12px;margin-bottom:8px;font-size:12px}
.node-html{font-family:'SFMono-Regular',Consolas,monospace;color:#79c0ff;word-break:break-all;margin-bottom:6px}
.node-summary{color:#8b949e}
.node-page{color:#58a6ff;font-size:11px;margin-top:4px}
.pages-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.page-chip{background:#0d1117;border:1px solid #21262d;border-radius:4px;padding:2px 8px;font-size:11px;color:#8b949e}
/* Pages tested */
.pages-table{width:100%%;border-collapse:collapse}
.pages-table th{background:#161b22;color:#8b949e;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;padding:8px 12px;border-bottom:2px solid #30363d;text-align:left}
.pages-table td{padding:8px 12px;border-bottom:1px solid #21262d;font-size:13px}
.score-pill{display:inline-block;padding:1px 8px;border-radius:12px;font-size:11px;font-weight:700}
/* Impact badges */
.imp-critical{background:rgba(248,81,73,.15);color:#f85149;border:1px solid rgba(248,81,73,.3)}
.imp-serious{background:rgba(210,153,34,.15);color:#d29922;border:1px solid rgba(210,153,34,.3)}
.imp-moderate{background:rgba(130,80,223,.15);color:#a5a0f5;border:1px solid rgba(130,80,223,.3)}
.imp-minor{background:rgba(139,148,158,.1);color:#8b949e;border:1px solid rgba(139,148,158,.2)}
/* Print */
@media print{
  body{background:#fff;color:#000}
  .tabs{display:none}
  .tab-pane{display:block!important}
  .header{background:#fff;border:1px solid #ccc}
  .wcag-table th{background:#f5f5f5;color:#333}
  .viol-card{border:1px solid #ccc}
}
.chevron{transition:transform .2s;color:#8b949e;font-size:12px}
.collapsed .chevron{transform:rotate(-90deg)}
.viol-body.hidden{display:none}
.toggle-all{background:#21262d;border:1px solid #30363d;color:#8b949e;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;margin-bottom:12px}
.toggle-all:hover{background:#2d333b;color:#c9d1d9}
.print-btn{background:#21262d;border:1px solid #30363d;color:#8b949e;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;margin-bottom:12px;margin-left:8px}
.print-btn:hover{background:#2d333b;color:#c9d1d9}
.section-title{font-size:16px;font-weight:700;color:#e6edf3;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.section-title::after{content:'';flex:1;height:1px;background:#30363d}
.caveat{background:#161b22;border:1px solid #30363d;border-left:3px solid #58a6ff;border-radius:6px;padding:12px 16px;margin-bottom:20px;font-size:13px;color:#8b949e}
.caveat strong{color:#e6edf3}
</style>
</head>
<body>
<div class="container">

<!-- HEADER -->
<div class="header">
  <div class="header-top">
    <div class="logo">Rapid<span>Radar</span></div>
  </div>
  <h1>Voluntary Product Accessibility Template (VPAT&reg; 2.4) &mdash; WCAG 2.1 Edition</h1>
  <div class="subtitle">Automated Accessibility Conformance Report generated by RapidRadar</div>
  <div class="meta-grid" id="meta-grid"></div>
</div>

<!-- TABS -->
<div class="tabs">
  <button class="tab-btn active" onclick="showTab('overview')">Overview</button>
  <button class="tab-btn" onclick="showTab('level-a')">WCAG 2.1 Level A</button>
  <button class="tab-btn" onclick="showTab('level-aa')">WCAG 2.1 Level AA</button>
  <button class="tab-btn" onclick="showTab('violations')">Violations Detail</button>
  <button class="tab-btn" onclick="showTab('pages')">Pages Tested</button>
</div>

<!-- OVERVIEW -->
<div class="tab-pane active" id="tab-overview">
  <div class="caveat">
    <strong>Scope of Evaluation:</strong>
    This report is generated from automated axe-core scans performed by RapidRadar.
    Automated tools detect approximately 30&#8211;40%% of WCAG issues.
    Criteria marked <em>Not Evaluated</em> require manual expert testing.
    This report does not constitute a complete accessibility audit.
  </div>
  <div class="section-title">Level A Conformance Summary</div>
  <div class="conf-summary" id="conf-summary-a"></div>
  <div class="section-title">Level AA Conformance Summary</div>
  <div class="conf-summary" id="conf-summary-aa"></div>
</div>

<!-- LEVEL A -->
<div class="tab-pane" id="tab-level-a">
  <div class="section-title">WCAG 2.1 Level A Success Criteria</div>
  <div class="caveat">
    <strong>Note:</strong> Criteria marked <em>Not Evaluated</em> require manual testing.
    Criteria marked <em>Not Applicable</em> do not apply to this web application.
  </div>
  <table class="wcag-table" id="table-a">
    <thead><tr>
      <th style="width:100px">Criteria</th>
      <th style="width:200px">Name</th>
      <th style="width:130px">Conformance</th>
      <th>Remarks and Explanations</th>
    </tr></thead>
    <tbody id="tbody-a"></tbody>
  </table>
</div>

<!-- LEVEL AA -->
<div class="tab-pane" id="tab-level-aa">
  <div class="section-title">WCAG 2.1 Level AA Success Criteria</div>
  <div class="caveat">
    <strong>Note:</strong> Criteria marked <em>Not Evaluated</em> require manual testing.
    Criteria marked <em>Not Applicable</em> do not apply to this web application.
  </div>
  <table class="wcag-table" id="table-aa">
    <thead><tr>
      <th style="width:100px">Criteria</th>
      <th style="width:200px">Name</th>
      <th style="width:130px">Conformance</th>
      <th>Remarks and Explanations</th>
    </tr></thead>
    <tbody id="tbody-aa"></tbody>
  </table>
</div>

<!-- VIOLATIONS DETAIL -->
<div class="tab-pane" id="tab-violations">
  <div class="section-title">Accessibility Violations Detail</div>
  <button class="toggle-all" onclick="toggleAll()">Expand / Collapse All</button>
  <button class="print-btn" onclick="window.print()">Print / Save PDF</button>
  <div id="violations-container"></div>
</div>

<!-- PAGES TESTED -->
<div class="tab-pane" id="tab-pages">
  <div class="section-title">Pages Tested</div>
  <table class="pages-table">
    <thead><tr>
      <th>#</th><th>Page URL</th><th>Browser</th>
      <th>A11y Score</th><th>Violations</th>
    </tr></thead>
    <tbody id="tbody-pages"></tbody>
  </table>
</div>

</div><!-- /container -->

<script>
const ROWS_A    = %(rows_a_json)s;
const ROWS_AA   = %(rows_aa_json)s;
const VIOLATIONS = %(violations_json)s;
const PAGES     = %(pages_json)s;
const COUNTS_A  = %(counts_a_json)s;
const COUNTS_AA = %(counts_aa_json)s;
const META = {
  product: "%(product_name)s",
  timestamp: "%(timestamp)s",
  pages: %(page_count)d
};

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}

// ---------------------------------------------------------------------------
// Badge helpers
// ---------------------------------------------------------------------------
const CONF_CLASSES = {
  'Supports': 'badge-supports',
  'Partially Supports': 'badge-partial',
  'Does Not Support': 'badge-notsupport',
  'Not Applicable': 'badge-na',
  'Not Evaluated': 'badge-noteval'
};
const CONF_ICONS = {
  'Supports': '✓',
  'Partially Supports': '◑',
  'Does Not Support': '✗',
  'Not Applicable': '—',
  'Not Evaluated': '?'
};
function confBadge(c) {
  return `<span class="badge ${CONF_CLASSES[c]||'badge-na'}">${CONF_ICONS[c]||''} ${c}</span>`;
}
const IMP_CLASSES = {critical:'imp-critical',serious:'imp-serious',moderate:'imp-moderate',minor:'imp-minor'};
function impBadge(i) {
  return `<span class="badge ${IMP_CLASSES[i]||'imp-minor'}">${i}</span>`;
}
function scoreColor(s) {
  if (s >= 90) return '#3fb950'; if (s >= 70) return '#d29922'; return '#f85149';
}

// ---------------------------------------------------------------------------
// Meta cards
// ---------------------------------------------------------------------------
function renderMeta() {
  const cards = [
    {label:'Product', value: META.product},
    {label:'Report Date', value: META.timestamp},
    {label:'Pages Tested', value: META.pages},
    {label:'Standard', value: 'WCAG 2.1 AA'},
    {label:'Tool', value: 'RapidRadar + axe-core 4.10'},
    {label:'Report Type', value: 'VPAT 2.4'},
  ];
  document.getElementById('meta-grid').innerHTML = cards.map(c =>
    `<div class="meta-card"><div class="label">${c.label}</div><div class="value">${c.value}</div></div>`
  ).join('');
}

// ---------------------------------------------------------------------------
// Conformance summary badges
// ---------------------------------------------------------------------------
const CONF_ORDER = ['Supports','Partially Supports','Does Not Support','Not Evaluated','Not Applicable'];
const CONF_DOTS  = {
  'Supports':'#3fb950','Partially Supports':'#d29922',
  'Does Not Support':'#f85149','Not Evaluated':'#58a6ff','Not Applicable':'#8b949e'
};
function renderSummary(counts, containerId) {
  const el = document.getElementById(containerId);
  el.innerHTML = CONF_ORDER.filter(k => counts[k]).map(k =>
    `<div class="conf-badge">
       <div class="dot" style="background:${CONF_DOTS[k]}"></div>
       <div class="cnt">${counts[k]}</div>
       <div class="lbl">${k}</div>
     </div>`
  ).join('');
}

// ---------------------------------------------------------------------------
// WCAG criterion table rows
// ---------------------------------------------------------------------------
function renderCriterionTable(rows, tbodyId) {
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML = rows.map(r =>
    `<tr>
       <td><span class="crit-id">${r.id}</span></td>
       <td><div class="crit-name">${r.name}</div><div class="crit-desc">${r.description}</div></td>
       <td>${confBadge(r.conformance)}</td>
       <td><span class="remarks">${r.remarks}</span></td>
     </tr>`
  ).join('');
}

// ---------------------------------------------------------------------------
// Violations detail
// ---------------------------------------------------------------------------
function renderViolations() {
  const container = document.getElementById('violations-container');
  const keys = Object.keys(VIOLATIONS);
  if (!keys.length) {
    container.innerHTML = '<div class="caveat"><strong>No violations detected</strong> in the automated scan.</div>';
    return;
  }
  container.innerHTML = keys.map((vid, idx) => {
    const v = VIOLATIONS[vid];
    const wcagCriteria = getWcagForRule(vid);
    return `<div class="viol-card" id="vcard-${idx}">
      <div class="viol-header" onclick="toggleCard(${idx})">
        <span class="chevron">▾</span>
        <span class="viol-id">${vid}</span>
        ${impBadge(v.impact)}
        <span class="viol-desc">${v.help}</span>
        <span class="badge badge-na">${v.pages.length} page${v.pages.length!==1?'s':''}</span>
        ${wcagCriteria.map(c => `<span class="badge badge-noteval">${c}</span>`).join('')}
      </div>
      <div class="viol-body hidden" id="vbody-${idx}">
        <p style="color:#8b949e;font-size:13px;margin-bottom:12px">${v.description}</p>
        <p style="font-size:12px;margin-bottom:8px">
          <a href="${v.helpUrl}" target="_blank">Learn more at Deque University ↗</a>
        </p>
        ${v.sample_nodes.length ? `
          <div style="font-size:12px;font-weight:600;color:#8b949e;margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px">Sample Occurrences</div>
          ${v.sample_nodes.map(n => `
            <div class="node-card">
              <div class="node-html">${escHtml(n.html)}</div>
              <div class="node-summary">${escHtml(n.failureSummary)}</div>
              <div class="node-page">📄 ${n.page_url}</div>
            </div>`).join('')}` : ''}
        <div style="font-size:12px;font-weight:600;color:#8b949e;margin:12px 0 8px;text-transform:uppercase;letter-spacing:.5px">All Affected Pages</div>
        <div class="pages-list">${v.pages.map(p => `<span class="page-chip">${p}</span>`).join('')}</div>
      </div>
    </div>`;
  }).join('');
}

function getWcagForRule(ruleId) {
  const map = {
    'image-alt':['1.1.1'],'input-image-alt':['1.1.1'],'object-alt':['1.1.1'],
    'region':['1.3.1'],'landmark-one-main':['1.3.1'],'heading-order':['1.3.1'],
    'label':['1.3.1','4.1.2'],'color-contrast':['1.4.3'],'keyboard':['2.1.1'],
    'bypass':['2.4.1'],'document-title':['2.4.2'],'link-name':['2.4.4','4.1.2'],
    'empty-heading':['2.4.6'],'focus-visible':['2.4.7'],'html-has-lang':['3.1.1'],
    'duplicate-id':['4.1.1'],'aria-required-attr':['4.1.2'],'button-name':['4.1.2'],
    'aria-valid-attr':['4.1.2'],'aria-valid-attr-value':['4.1.2'],
  };
  return map[ruleId] || [];
}

function toggleCard(idx) {
  const card = document.getElementById('vcard-' + idx);
  const body = document.getElementById('vbody-' + idx);
  card.classList.toggle('collapsed');
  body.classList.toggle('hidden');
}

let allExpanded = false;
function toggleAll() {
  allExpanded = !allExpanded;
  document.querySelectorAll('.viol-body').forEach(b => b.classList.toggle('hidden', !allExpanded));
  document.querySelectorAll('.viol-card').forEach(c => c.classList.toggle('collapsed', !allExpanded));
}

// ---------------------------------------------------------------------------
// Pages tested table
// ---------------------------------------------------------------------------
function renderPages() {
  const tbody = document.getElementById('tbody-pages');
  tbody.innerHTML = PAGES.map((p, i) => {
    const score = p.score != null ? p.score : '—';
    const scoreStyle = p.score != null ? `color:${scoreColor(p.score)};font-weight:700` : '';
    return `<tr>
      <td style="color:#8b949e">${i+1}</td>
      <td><a href="${p.page_url}" target="_blank">${p.page_url}</a></td>
      <td>${p.browser}</td>
      <td><span class="score-pill" style="${scoreStyle}">${score}</span></td>
      <td>${p.total_violations}</td>
    </tr>`;
  }).join('');
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
renderMeta();
renderSummary(COUNTS_A, 'conf-summary-a');
renderSummary(COUNTS_AA, 'conf-summary-aa');
renderCriterionTable(ROWS_A, 'tbody-a');
renderCriterionTable(ROWS_AA, 'tbody-aa');
renderViolations();
renderPages();
</script>
</body>
</html>
"""
