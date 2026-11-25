#!/usr/bin/env python3
"""
RapidRadar Web Interface
========================
One web UI for both RapidRadar utilities:

  * Page Load Analysis  — crawls / tests web pages (performance + accessibility)
  * DB Scan             — scans the database for slow queries, missing / fragmented /
                          unused indexes, I/O, CPU and connections
                          (SQL Server, PostgreSQL, Oracle, MySQL / MariaDB)

Run modes
---------
  pageload  Page Load Analysis only (original RapidRadar behaviour)
  dbscan    DB Scan only — continuous scanning until the user presses Stop
  both      Page Load + DB Scan — the DB is scanned *while* the browser test
            drives the UI; only DB activity inside that window is reported and
            the DB report is written into the same run folder, together with a
            combined landing page.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from queue import Queue, Empty

from flask import Flask, render_template, request, jsonify, Response, send_file

from performance_framework.vpat_generator import generate_vpat_from_folder
from db_scan import ConnectionConfig, create_scanner, driver_status
from db_scan.report import ENGINE_COLOURS
from combined_report import build_combined_report
from db_scan.pages import PageTracker
from db_scan.service import DBScanService

# ── Frozen-EXE path resolution ────────────────────────────────────────────────
# When bundled by PyInstaller:
#   sys.frozen  = True
#   sys.executable = path to RapidRadar.exe
#   sys._MEIPASS   = temp extraction folder (contains bundled data files)
#   _APP_DIR       = folder next to the EXE (reports/, etc. go here)
# In dev mode, _APP_DIR is the project folder containing app.py.
if getattr(sys, "frozen", False):
    _APP_DIR       = os.path.dirname(sys.executable)
    _BUNDLE_DIR    = sys._MEIPASS          # type: ignore[attr-defined]
    _TEMPLATE_DIR  = os.path.join(_BUNDLE_DIR, "templates")
else:
    _APP_DIR       = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE_DIR    = _APP_DIR
    _TEMPLATE_DIR  = os.path.join(_APP_DIR, "templates")

_REPORTS_DIR = os.path.join(_APP_DIR, "reports")

app = Flask(__name__, template_folder=_TEMPLATE_DIR)

# Global queue for log messages (shared by page-load runs and DB scans)
log_queue = Queue()
current_process = None

# DB scan engine (used by the standalone DB Scan mode and the combined mode)
db_service = DBScanService(_REPORTS_DIR, log=log_queue.put)

# State of the current page-load / combined job
_job_lock = threading.Lock()
_job = {"running": False, "mode": None, "run_folder": None, "combined_report_url": None}

VALID_MODES = ("pageload", "dbscan", "both")


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

PAGE_EVENT_PREFIX = b"@@RR_PAGE@@ "


def enqueue_output(stream, queue, prefix="", on_page_event=None):
    """Read lines from a stream and put them in a queue.

    Lines starting with PAGE_EVENT_PREFIX are page-boundary events from
    run_test.py --page-events; they go to *on_page_event* instead of the log.
    """
    for line in iter(stream.readline, b''):
        if not line:
            continue
        if on_page_event is not None and PAGE_EVENT_PREFIX in line:
            before, _, payload = line.partition(PAGE_EVENT_PREFIX)
            if before.strip():
                queue.put(prefix + before.decode('utf-8', errors='replace') + "\n")
            on_page_event(payload)
            continue
        queue.put(prefix + line.decode('utf-8', errors='replace'))
    stream.close()


def _clear_log_queue():
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except Empty:
            break


def _make_run_suffix(edm_version):
    """Same naming scheme run_test.py uses, decided here so the web app knows the folder."""
    now = datetime.now()
    clean = re.sub(r'[^A-Za-z0-9._-]', '', (edm_version or "").strip())
    stamp = f"{now.strftime('%d-%m-%Y')}_{now.strftime('%H-%M-%S')}"
    return f"EDM_Version_{clean}_{stamp}" if clean else f"Run_{stamp}"


def _abs_output(output_folder):
    folder = output_folder or "reports"
    return folder if os.path.isabs(folder) else os.path.join(_APP_DIR, folder)


def _report_url(path):
    """Map a file path to a /reports/... URL when it lives under reports/."""
    try:
        rel = os.path.relpath(path, _REPORTS_DIR)
    except ValueError:
        return None
    return None if rel.startswith("..") else "/reports/" + rel.replace("\\", "/")


def _build_test_cmd(login_url, username, password, browsers, output_folder, run_suffix,
                    extra_urls=None, csv_urls=None, app_server_ip=None, db_server_ip=None,
                    edm_version=None):
    """Build the run_test.py command line (frozen EXE re-invokes itself)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--internal-run-test"]
    else:
        cmd = [sys.executable, os.path.join(_APP_DIR, "run_test.py")]
    cmd += [
        "--login-url", login_url,
        "--username", username,
        "--password", password,
        "--browser", ",".join(browsers),
        "--output", output_folder,
        "--run-suffix", run_suffix,
    ]
    for url in (extra_urls or []):
        if url.strip():
            cmd += ["--extra-urls", url.strip()]
    for url in (csv_urls or []):
        if url.strip():
            cmd += ["--csv-urls", url.strip()]
    if app_server_ip:
        cmd += ["--app-server-ip", app_server_ip]
    if db_server_ip:
        cmd += ["--db-server-ip", db_server_ip]
    if edm_version:
        cmd += ["--edm-version", edm_version]
    return cmd


def _masked(cmd):
    """Command line for logging, with the password hidden."""
    out = list(cmd)
    for i, part in enumerate(out[:-1]):
        if part == "--password":
            out[i + 1] = "********"
    return " ".join(out)


def _run_subprocess(cmd, title, page_tracker=None):
    """Run the page-load test subprocess, streaming its output. Returns the exit code.

    With *page_tracker* (combined mode) the runner is started with --page-events:
    at every page boundary a DB query snapshot is taken and the runner is then
    acknowledged through its stdin.
    """
    global current_process
    if page_tracker is not None:
        cmd = list(cmd) + ["--page-events"]

    log_queue.put(f"\n{'='*65}\n")
    log_queue.put(f"[INFO] Starting {title} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    log_queue.put(f"[INFO] Command: {_masked(cmd)}\n")
    log_queue.put(f"{'='*65}\n\n")

    return_code = -1
    try:
        # Set up environment with UTF-8 encoding to handle Unicode characters
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'

        current_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if page_tracker is not None else None,
            cwd=_APP_DIR, bufsize=1, env=env,
        )
        proc = current_process

        def _on_page_event(payload):
            try:
                page_tracker.handle_event(json.loads(payload.decode('utf-8', errors='replace')))
            except Exception as exc:
                log_queue.put(f"[WARN] [DB] Page event ignored: {exc}\n")
            finally:
                try:                                   # always release the runner
                    proc.stdin.write(b"ok\n")
                    proc.stdin.flush()
                except Exception:
                    pass

        stdout_thread = threading.Thread(
            target=enqueue_output,
            args=(current_process.stdout, log_queue, "",
                  _on_page_event if page_tracker is not None else None),
            daemon=True)
        stderr_thread = threading.Thread(target=enqueue_output,
                                         args=(current_process.stderr, log_queue, "[ERROR] "), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        return_code = current_process.wait()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

        log_queue.put(f"\n{'='*65}\n")
        if return_code == 0:
            log_queue.put(f"[SUCCESS] {title} completed successfully!\n")
        else:
            log_queue.put(f"[ERROR] {title} failed with return code {return_code}\n")
        log_queue.put(f"{'='*65}\n")
    except Exception as e:
        log_queue.put(f"\n[ERROR] Exception occurred: {str(e)}\n")
    finally:
        current_process = None
    return return_code


def _parse_db_config(data):
    """Return (ConnectionConfig, interval, error)."""
    db = data.get("db") or {}
    cfg = ConnectionConfig.from_dict(db)
    ok, msg = cfg.validate()
    if not ok:
        return None, None, msg
    try:
        interval = max(10, min(int(db.get("interval", 30)), 3600))
    except (TypeError, ValueError):
        interval = 30
    return cfg, interval, None


# ═════════════════════════════════════════════════════════════════════════════
#  Job orchestration (page load / combined)
# ═════════════════════════════════════════════════════════════════════════════

def _run_job(mode, cmd, title, run_dir, db_cfg=None, db_interval=30, meta=None):
    """Background thread: optional DB scan around the page-load subprocess."""
    tracker, tracking_error = None, None
    try:
        if mode == "both":
            log_queue.put(f"\n{'='*65}\n[INFO] Combined run: Page Load Analysis + DB Scan "
                          f"({db_cfg.label})\n{'='*65}\n")
            log_queue.put(f"[DB] Testing connection to {db_cfg.describe()} …\n")
            probe = create_scanner(db_cfg).test_connection()
            if not probe.get("success"):
                log_queue.put(f"[ERROR] [DB] Connection failed: {probe.get('message')}\n")
                log_queue.put("[ERROR] Combined run aborted — fix the DB connection or choose "
                              "'Page Load Analysis' mode.\n")
                return
            log_queue.put(f"[DB] ✓ Connected — {probe.get('server_version')}\n")

            ok, msg = db_service.start(db_cfg, interval=db_interval, scope="window",
                                       output_dir=run_dir, mode="combined",
                                       context={"mode": "combined"})
            if not ok:
                log_queue.put(f"[ERROR] [DB] {msg}\n")
                return
            # Make sure the analysis window is open before the browser starts.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                st = db_service.status()
                if st["window_start"] or not st["running"]:
                    break
                time.sleep(0.5)
            if not db_service.status()["running"]:
                log_queue.put("[ERROR] [DB] DB scan could not start — combined run aborted.\n")
                return

            tracker = PageTracker(db_cfg, log=log_queue.put)
            if tracker.begin():
                log_queue.put("[DB] ✓ Per-page query tracking on — DB activity will be linked to "
                              "each page tested.\n")
            else:
                tracking_error, tracker = tracker.error, None

        rc = _run_subprocess(cmd, title, page_tracker=tracker)

        if mode == "both":
            pages = None
            if tracker is not None:
                tracker.finish(run_dir)
                pages, tracking_error = tracker.pages, tracker.error
            log_queue.put(f"\n[DB] Page Load Analysis finished (exit code {rc}) — stopping DB scan, "
                          "taking the final scan and writing the report …\n")
            db_service.stop()
            db_service.wait(timeout=1800)
            path = _write_combined_report(run_dir, meta or {}, db_service.status(), rc, pages,
                                          tracking_error)
            if path:
                url = _report_url(path)
                with _job_lock:
                    _job["combined_report_url"] = url
                log_queue.put(f"\n{'='*65}\n[SUCCESS] ✓ Combined report → {path}\n{'='*65}\n")
    except Exception as exc:
        log_queue.put(f"\n[ERROR] {exc}\n")
        if db_service.status()["running"]:
            db_service.stop()
    finally:
        if tracker is not None:
            tracker.close()
        with _job_lock:
            _job["running"] = False


def _start_job(mode, cmd_kwargs, title, data):
    """Validate + launch a page-load (or combined) job. Returns a Flask response."""
    db_cfg, interval = None, 30
    if mode == "both":
        db_cfg, interval, err = _parse_db_config(data)
        if err:
            return jsonify({'error': f'DB connection: {err}'}), 400
        if db_service.is_busy():
            return jsonify({'error': 'A DB scan is already running. Stop it first.'}), 409

    with _job_lock:
        if _job["running"] or (current_process is not None and current_process.poll() is None):
            return jsonify({'error': 'A test is already running.'}), 409
        run_suffix = _make_run_suffix(cmd_kwargs.get("edm_version"))
        run_dir = os.path.join(_abs_output(cmd_kwargs["output_folder"]), run_suffix)
        _job.update({"running": True, "mode": mode, "run_folder": run_dir,
                     "combined_report_url": None})

    _clear_log_queue()
    cmd = _build_test_cmd(run_suffix=run_suffix, **cmd_kwargs)
    meta = {
        "login_url": cmd_kwargs["login_url"], "browsers": cmd_kwargs["browsers"],
        "edm_version": cmd_kwargs.get("edm_version"), "run_suffix": run_suffix,
        "csv_pages": len(cmd_kwargs.get("csv_urls") or []),
    }
    threading.Thread(target=_run_job, daemon=True,
                     args=(mode, cmd, title, run_dir, db_cfg, interval, meta)).start()
    return jsonify({'message': f'{title} started successfully', 'run_folder': run_suffix})


def _common_form(data):
    return dict(
        login_url=data.get('loginUrl', '').strip(),
        username=data.get('username', '').strip(),
        password=data.get('password', ''),
        browsers=data.get('browsers', []),
        output_folder=(data.get('outputFolder') or 'reports').strip(),
        app_server_ip=(data.get('appServerIp') or '').strip() or None,
        db_server_ip=(data.get('dbServerIp') or '').strip() or None,
        edm_version=(data.get('edmVersion') or '').strip() or None,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Combined landing page
# ═════════════════════════════════════════════════════════════════════════════

def _write_combined_report(run_dir, meta, db_state, page_rc, pages=None, tracking_error=None):
    """Write RapidRadar_Combined_<run>.html linking page-load + DB reports."""
    try:
        return build_combined_report(run_dir, meta, db_state, page_rc, pages=pages,
                                     engine_colours=ENGINE_COLOURS, tracking_error=tracking_error)
    except Exception as exc:
        log_queue.put(f"[WARN] Could not write combined report: {exc}\n")
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Routes — UI + page load
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Serve the main web interface."""
    return render_template('index.html')


@app.route('/sample-csv')
def sample_csv():
    """Download the sample CSV template."""
    # In dev mode it's next to app.py; when frozen it's bundled in _BUNDLE_DIR
    csv_path = os.path.join(_BUNDLE_DIR, 'sample_pages.csv')
    return send_file(csv_path, as_attachment=True, download_name='sample_pages.csv', mimetype='text/csv')


@app.route('/run-test', methods=['POST'])
def run_test_endpoint():
    """Start Page Load Analysis (mode=pageload) or Page Load + DB Scan (mode=both)."""
    data = request.json or {}
    mode = data.get('mode', 'pageload')
    if mode not in ("pageload", "both"):
        return jsonify({'error': f'Invalid mode for /run-test: {mode}'}), 400
    form = _common_form(data)
    if not all([form['login_url'], form['username'], form['password'], form['browsers']]):
        return jsonify({'error': 'Login URL, username, password, and at least one browser are required'}), 400
    form['extra_urls'] = data.get('extraUrls', [])
    title = "Page Load + DB Scan" if mode == "both" else "performance test"
    return _start_job(mode, form, title, data)


@app.route('/run-csv-pages', methods=['POST'])
def run_csv_pages_endpoint():
    """Start a CSV-mode test for the supplied page URLs (optionally with DB Scan)."""
    data = request.json or {}
    mode = data.get('mode', 'pageload')
    if mode not in ("pageload", "both"):
        return jsonify({'error': f'Invalid mode for /run-csv-pages: {mode}'}), 400
    form = _common_form(data)
    if not all([form['login_url'], form['username'], form['password'], form['browsers']]):
        return jsonify({'error': 'Login URL, username, password, and at least one browser are required'}), 400
    csv_urls = data.get('csvUrls', [])
    if not csv_urls:
        return jsonify({'error': 'No page URLs were provided'}), 400
    form['csv_urls'] = csv_urls
    title = "CSV page test + DB Scan" if mode == "both" else "CSV page test"
    return _start_job(mode, form, title, data)


@app.route('/stream-logs')
def stream_logs():
    """Stream logs in real-time using Server-Sent Events."""
    def generate():
        while True:
            try:
                message = log_queue.get(timeout=1)
                yield f"data: {message}\n\n"
            except Empty:
                yield ": heartbeat\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/status')
def status():
    """Current job + DB scan state (polled by the UI)."""
    proc_running = current_process is not None and current_process.poll() is None
    with _job_lock:
        job = dict(_job)
    db_state = db_service.status()
    db_state.pop("report_path", None)
    return jsonify({
        'running': job["running"] or proc_running,
        'mode': job["mode"],
        'combined_report_url': job["combined_report_url"],
        'db': db_state,
    })


@app.route('/list-reports')
def list_reports():
    """Return a list of available report run folders."""
    if not os.path.isdir(_REPORTS_DIR):
        return jsonify({'reports': []})
    folders = sorted([d for d in os.listdir(_REPORTS_DIR)
                      if os.path.isdir(os.path.join(_REPORTS_DIR, d))], reverse=True)
    result = []
    for folder in folders:
        files = os.listdir(os.path.join(_REPORTS_DIR, folder))
        first = lambda pred: next((f for f in files if pred(f)), None)   # noqa: E731
        result.append({
            'folder': folder,
            'has_data': any(f.endswith('_data.json') for f in files),
            'summary_file': first(lambda f: f.startswith('summary_') and f.endswith('.html')),
            'vpat_file': first(lambda f: f.startswith('vpat_')),
            'db_report_file': first(lambda f: f.startswith('DBScan_') and f.endswith('.html')),
            'combined_file': first(lambda f: f.startswith('RapidRadar_Combined_')),
        })
    return jsonify({'reports': result})


@app.route('/generate-vpat', methods=['POST'])
def generate_vpat_endpoint():
    """Generate a VPAT report from an existing report folder."""
    data = request.json or {}
    folder_name = data.get('folder', '').strip()
    if not folder_name:
        return jsonify({'error': 'folder name is required'}), 400

    report_folder = os.path.normpath(os.path.join(_REPORTS_DIR, folder_name))
    if not report_folder.startswith(os.path.normpath(_REPORTS_DIR) + os.sep):
        return jsonify({'error': 'Access denied'}), 403
    if not os.path.isdir(report_folder):
        return jsonify({'error': f'Report folder not found: {folder_name}'}), 404

    vpat_file = os.path.join(report_folder, f'vpat_{folder_name}.html')
    try:
        generate_vpat_from_folder(report_folder, vpat_file)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

    return jsonify({'vpat_file': f'reports/{folder_name}/vpat_{folder_name}.html'})


@app.route('/reports/<path:filepath>')
def serve_report(filepath):
    """Serve static report files (HTML, JSON) from the reports directory."""
    full_path = os.path.normpath(os.path.join(_REPORTS_DIR, filepath))
    # Security: ensure the resolved path stays inside the reports directory
    if not full_path.startswith(os.path.normpath(_REPORTS_DIR) + os.sep):
        return jsonify({'error': 'Access denied'}), 403
    if not os.path.isfile(full_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(full_path)


# ═════════════════════════════════════════════════════════════════════════════
#  Routes — DB Scan API
# ═════════════════════════════════════════════════════════════════════════════

@app.route('/api/db/drivers')
def db_drivers():
    """Which database engines are supported and whether their driver is installed."""
    return jsonify(driver_status())


@app.route('/api/db/test-connection', methods=['POST'])
def db_test_connection():
    data = request.get_json(silent=True) or {}
    cfg, _, err = _parse_db_config({"db": data})
    if err:
        return jsonify({"success": False, "message": err})
    try:
        return jsonify(create_scanner(cfg).test_connection())
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


_SELF_CHECKS = [
    ("db_info", "Database information"),
    ("slow_queries", "Slow queries"),
    ("object_performance", "Procedures / functions"),
    ("missing_indexes", "Missing indexes"),
    ("index_fragmentation", "Index fragmentation"),
    ("unused_indexes", "Unused indexes"),
    ("iops", "I/O statistics"),
    ("cpu_utilization", "CPU utilisation"),
    ("throughput", "Throughput"),
]


def _check_row(name, res):
    res = res or {}
    if res.get("success"):
        n = len(res.get("rows") or [])
        return {"name": name, "ok": True, "status": "ok", "message": f"{n} row(s)"}
    if res.get("unsupported"):
        return {"name": name, "ok": False, "status": "unsupported",
                "message": res.get("error") or "Not available for this database"}
    return {"name": name, "ok": False, "status": "error", "message": res.get("error") or "Failed"}


def run_self_check(cfg):
    """Run every DB Scan check once and report which ones work for this login."""
    scanner = create_scanner(cfg)
    try:
        results = scanner.run_full_scan()
    except Exception as exc:
        return {"success": False, "db_type_label": cfg.label,
                "message": f"Could not connect: {exc}", "checks": []}
    checks = [_check_row(name, results.get(key)) for key, name in _SELF_CHECKS]
    conn = results.get("connections") or {}
    checks.append(_check_row("Connections", conn.get("summary")))
    for extra in results.get("extra_sections") or []:
        checks.append(_check_row(extra.get("title") or extra.get("id") or "Extra", extra.get("result")))
    # Per-page query tracking (combined mode)
    try:
        snap = scanner.query_snapshot()
        checks.append({"name": "Per-page query tracking", "ok": True, "status": "ok",
                       "message": f"{len(snap)} statement(s) visible"})
    except Exception as exc:
        checks.append({"name": "Per-page query tracking", "ok": False, "status": "error",
                       "message": str(exc)[:300]})
    finally:
        scanner.close_snapshots()
    failed = sum(1 for c in checks if c["status"] == "error")
    return {
        "success": failed == 0,
        "db_type": cfg.db_type,
        "db_type_label": cfg.label,
        "server_version": str(results.get("server_version") or "")[:200],
        "message": ("All checks work for this login." if not failed
                    else f"{failed} of {len(checks)} checks failed — see details."),
        "findings": [f.get("message") for f in results.get("findings") or []],
        "checks": checks,
    }


@app.route('/api/db/self-check', methods=['POST'])
def db_self_check():
    """Run every check once (used to validate a new database / login before a real run)."""
    data = request.get_json(silent=True) or {}
    cfg, _, err = _parse_db_config({"db": data})
    if err:
        return jsonify({"success": False, "message": err, "checks": []})
    try:
        return jsonify(run_self_check(cfg))
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc), "checks": []})


@app.route('/api/db/start', methods=['POST'])
def db_start():
    """Standalone DB Scan: scan every <interval> seconds until /api/db/stop."""
    data = request.get_json(silent=True) or {}
    cfg, interval, err = _parse_db_config({"db": data})
    if err:
        return jsonify({"success": False, "message": err})
    with _job_lock:
        if _job["running"]:
            return jsonify({"success": False, "message": "A page-load test is running — wait for it "
                                                         "to finish or use the combined mode."})
    scope = "window" if data.get("scope") == "window" else "cumulative"
    _clear_log_queue()
    log_queue.put(f"\n{'='*65}\n[INFO] Starting DB Scan ({cfg.label}) at "
                  f"{datetime.now():%Y-%m-%d %H:%M:%S} — every {interval}s, scope: {scope}\n{'='*65}\n")
    out_root = _abs_output((data.get("outputFolder") or "reports").strip())
    stamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
    out_dir = os.path.join(out_root, f"DBScan_{cfg.db_type}_{re.sub(r'[^A-Za-z0-9._-]+', '_', cfg.database)}_{stamp}")
    ok, msg = db_service.start(cfg, interval=interval, scope=scope, output_dir=out_dir,
                               mode="standalone")
    return jsonify({"success": ok, "message": msg})


@app.route('/api/db/stop', methods=['POST'])
def db_stop():
    with _job_lock:
        if _job["running"] and _job["mode"] == "both":
            return jsonify({"success": False, "message": "The DB scan stops automatically when the "
                                                         "page-load test of the combined run finishes."})
    ok, msg = db_service.stop()
    return jsonify({"success": ok, "message": msg})


@app.route('/api/db/status')
def db_status():
    st = db_service.status()
    st.pop("report_path", None)
    return jsonify(st)


if __name__ == '__main__':
    # Emoji in the banner/logs must not crash when stdout is redirected (cp1252).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("\n" + "="*65)
    print("🚀 Rapid Radar - Page Load Analysis + DB Scan")
    print("   Created by OPUS EDM PERF TEAM")
    print("="*65)
    print("\n   Server starting on http://127.0.0.1:5000")
    print("   Press Ctrl+C to stop the server\n")
    print("="*65 + "\n")

    # use_reloader=False: the reloader would start a second process and the
    # background DB-scan thread state would be split between them.
    app.run(debug=True, use_reloader=False, host='127.0.0.1', port=5000, threaded=True)
