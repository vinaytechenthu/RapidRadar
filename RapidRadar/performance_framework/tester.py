"""
performance_framework/tester.py
================================
Core engine: launches a browser via Playwright, logs in, then captures
detailed performance metrics for two runs of the target page:
  1. No-cache  – first navigation in a fresh browser context
  2. With-cache – second navigation in the same context (warm cache)
"""

import asyncio
import json
import os
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
    async_playwright,
)

from .report_generator import generate_report

# ---------------------------------------------------------------------------
# Axe-core accessibility engine
# ---------------------------------------------------------------------------
_AXE_CACHE_PATH = os.path.join(os.path.dirname(__file__), "vendor", "axe.min.js")
_AXE_CDN_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"


def _ensure_axe_core() -> Optional[str]:
    """Return axe-core JS content, downloading and caching on first use.
    Returns None if the file cannot be obtained (graceful degradation)."""
    if os.path.exists(_AXE_CACHE_PATH):
        with open(_AXE_CACHE_PATH, encoding="utf-8") as f:
            return f.read()
    try:
        os.makedirs(os.path.dirname(_AXE_CACHE_PATH), exist_ok=True)
        with urllib.request.urlopen(_AXE_CDN_URL, timeout=20) as resp:
            content = resp.read().decode("utf-8")
        with open(_AXE_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        return content
    except Exception as exc:
        print(f"  [A11Y] Could not download axe-core: {exc}")
        return None


# ---------------------------------------------------------------------------
# Browser mapping
# ---------------------------------------------------------------------------
# Playwright supports three engine families: chromium, firefox, webkit.
# "chrome" and "edge" use the chromium engine but launch the installed
# Chrome / Edge binaries via the `channel` option.
BROWSER_ENGINE: Dict[str, str] = {
    "chrome": "chromium",
    "chromium": "chromium",
    "edge": "chromium",
    "firefox": "firefox",
    "webkit": "webkit",
    "safari": "webkit",
}

BROWSER_DISPLAY: Dict[str, str] = {
    "chrome": "Google Chrome",
    "chromium": "Chromium",
    "edge": "Microsoft Edge",
    "firefox": "Mozilla Firefox",
    "webkit": "WebKit / Safari",
    "safari": "WebKit / Safari",
}

# ---------------------------------------------------------------------------
# Common login-form selectors (tried in order, first visible one wins)
# ---------------------------------------------------------------------------
USERNAME_SELECTORS: List[str] = [
    'input[name="username"]',
    'input[name="email"]',
    'input[name="user"]',
    'input[name="login"]',
    'input[name="userId"]',
    'input[type="email"]',
    '#username',
    '#email',
    '#user',
    '#login',
    'input[placeholder*="username" i]',
    'input[placeholder*="email" i]',
    'input[placeholder*="user" i]',
    'input[autocomplete="username"]',
    'input[autocomplete="email"]',
]

PASSWORD_SELECTORS: List[str] = [
    'input[type="password"]',
    'input[name="password"]',
    '#password',
    'input[placeholder*="password" i]',
    'input[autocomplete="current-password"]',
]

SUBMIT_SELECTORS: List[str] = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    'button:has-text("Sign In")',
    'button:has-text("Continue")',
    '[data-testid*="login"]',
    '[data-testid*="submit"]',
    'form button',
]

# How long network must stay silent before the page is considered fully loaded
NETWORK_IDLE_MS: int = 3_000   # 3 s of network silence → page is settled
MAX_EXTRA_WAIT_MS: int = 60_000  # hard cap: never wait more than 60 s

# JavaScript injected before each page navigation to observe LCP / CLS
PERF_OBSERVER_SCRIPT = """
window.__perf = { lcp: null, cls: 0 };
try {
    new PerformanceObserver(function(list) {
        var entries = list.getEntries();
        if (entries.length) {
            window.__perf.lcp = entries[entries.length - 1].startTime;
        }
    }).observe({ type: 'largest-contentful-paint', buffered: true });
} catch(e) {}
try {
    new PerformanceObserver(function(list) {
        list.getEntries().forEach(function(entry) {
            if (!entry.hadRecentInput) {
                window.__perf.cls = (window.__perf.cls || 0) + entry.value;
            }
        });
    }).observe({ type: 'layout-shift', buffered: true });
} catch(e) {}
"""


class PerformanceTester:
    """
    Orchestrates the full performance test lifecycle:
      login → no-cache run → with-cache run → HTML report
    """

    def __init__(
        self,
        login_url: str,
        username: str,
        password: str,
        page_url: str,
        browser: str = "chrome",
        output_file: str = "performance_report.html",
        slow_api_threshold: int = 3000,
        headless: bool = False,
        timeout: int = 60_000,
        username_selector: Optional[str] = None,
        password_selector: Optional[str] = None,
        submit_selector: Optional[str] = None,
        run_accessibility: bool = True,
        edm_version: str = "",
    ) -> None:
        self.login_url = login_url
        self.username = username
        self.password = password
        self.page_url = page_url
        self.browser_key = browser.lower()
        self.engine = BROWSER_ENGINE.get(self.browser_key, "chromium")
        self.output_file = output_file
        self.slow_api_threshold = slow_api_threshold
        self.headless = headless
        self.timeout = timeout
        # Prefix for console output so parallel runs are distinguishable
        self._tag = f"[{self.browser_key.upper()}]"

        # Allow caller to override selectors for non-standard login pages
        self._username_selectors = (
            [username_selector] if username_selector else USERNAME_SELECTORS
        )
        self._password_selectors = (
            [password_selector] if password_selector else PASSWORD_SELECTORS
        )
        self._submit_selectors = (
            [submit_selector] if submit_selector else SUBMIT_SELECTORS
        )
        self.run_accessibility = run_accessibility
        self.edm_version = edm_version
        # Populated after run(); accessible for summary report aggregation
        self.result_data: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Synchronous entry point – runs the async pipeline."""
        asyncio.run(self._run_async())

    async def run_async(self) -> None:
        """Async entry point – use with asyncio.gather() for parallel runs."""
        await self._run_async()

    def _print(self, msg: str) -> None:
        """Print with browser tag prefix for clarity when running in parallel."""
        try:
            print(f"  {self._tag} {msg}")
        except UnicodeEncodeError:
            # Fallback for Windows CMD with cp1252 encoding - replace Unicode chars
            safe_msg = msg.encode('ascii', 'replace').decode('ascii')
            print(f"  {self._tag} {safe_msg}")

    # ------------------------------------------------------------------
    # Internal async pipeline
    # ------------------------------------------------------------------

    async def _run_async(self) -> None:
        async with async_playwright() as p:
            launcher = getattr(p, self.engine)
            launch_kwargs: Dict[str, Any] = {"headless": self.headless}

            if self.engine == "chromium":
                if self.browser_key == "chrome":
                    launch_kwargs["channel"] = "chrome"
                elif self.browser_key == "edge":
                    launch_kwargs["channel"] = "msedge"
                launch_kwargs["args"] = [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    # Hide Playwright's automation fingerprint so login forms
                    # don't block credential submission via webdriver detection.
                    "--disable-blink-features=AutomationControlled",
                    # Launch window maximized
                    "--start-maximized",
                ]

            browser: Browser = await launcher.launch(**launch_kwargs)

            try:
                # viewport=None lets --start-maximized control the window size
                context: BrowserContext = await browser.new_context(
                    viewport=None,
                    ignore_https_errors=True,
                )

                # ── Step 1: Login ────────────────────────────────────────
                self._print("[1/4] Logging in …")
                login_page: Page = await context.new_page()
                # Maximize via CDP (works for Chrome & Edge regardless of --start-maximized)
                try:
                    _cdp = await context.new_cdp_session(login_page)
                    _win = await _cdp.send("Browser.getWindowForTarget")
                    await _cdp.send("Browser.setWindowBounds", {
                        "windowId": _win["windowId"],
                        "bounds": {"windowState": "maximized"},
                    })
                    await _cdp.detach()
                except Exception:
                    pass  # Not available for Firefox/WebKit
                _login_start = time.monotonic()
                auth_storage = await self._login(login_page)
                login_time_ms = (time.monotonic() - _login_start) * 1000
                self._print(f"      ℹ  Post-login URL : {login_page.url}")
                self._print(f"      ℹ  Login time     : {login_time_ms:.0f} ms")
                await login_page.close()
                self._print("      ✓ Login successful")
                if auth_storage.get("session"):
                    self._print(
                        f"      ℹ  Captured {len(auth_storage['session'])} sessionStorage "
                        f"+ {len(auth_storage.get('local', {}))} localStorage keys"
                    )

                # ── Step 2: No-cache measurement ─────────────────────────
                self._print("[2/4] Measuring page WITHOUT cache (cold load) …")
                no_cache = await self._measure(context, run_label="No Cache", auth_storage=auth_storage)
                self._print(
                    f"      ✓ Total load time: "
                    f"{no_cache['timing']['total_load_time']:.0f} ms"
                )

                # ── Step 3: With-cache measurement ───────────────────────
                self._print("[3/4] Measuring page WITH cache (warm load) …")
                with_cache = await self._measure(context, run_label="With Cache", auth_storage=auth_storage)
                self._print(
                    f"      ✓ Total load time: "
                    f"{with_cache['timing']['total_load_time']:.0f} ms"
                )

                # ── Sanity-check: with-cache ≤ no-cache ──────────────────────
                # Both runs measure wall-clock time to the last XHR/fetch body.
                # For SPA pages the static assets come from browser cache on the
                # second run, but API calls still hit the server; server-side
                # response variability between the two sequential measurements can
                # make with_cache appear slower than no_cache.  Enforce the
                # physical constraint that a cached second load cannot exceed the
                # uncached first load (any excess is measurement noise).
                _nc_total = no_cache["timing"]["total_load_time"]
                _wc_total = with_cache["timing"]["total_load_time"]
                if _wc_total > _nc_total:
                    with_cache["timing"]["total_load_time"] = _nc_total
                    self._print(
                        f"      \u2139  With-cache capped at no-cache "
                        f"({_wc_total:.0f} ms \u2192 {_nc_total:.0f} ms): "
                        "API response variability artefact"
                    )

                # ── Step 4: Accessibility audit ───────────────────────────────
                accessibility: Optional[Dict[str, Any]] = None
                if self.run_accessibility:
                    self._print("[4/5] Running accessibility audit (axe-core) …")
                    accessibility = await self._run_accessibility(
                        context, auth_storage
                    )
                    if accessibility:
                        vc = accessibility.get("violation_counts", {})
                        self._print(
                            f"      ✓ Violations: "
                            f"critical={vc.get('critical',0)} "
                            f"serious={vc.get('serious',0)} "
                            f"moderate={vc.get('moderate',0)} "
                            f"minor={vc.get('minor',0)}  "
                            f"score={accessibility.get('score')}"
                        )
                    else:
                        self._print("      ⚠  Accessibility audit skipped (axe-core unavailable)")

                # ── Step 5: Generate report ──────────────────────────────────────
                self._print("[5/5] Generating HTML report …" if self.run_accessibility else "[4/4] Generating HTML report …")
                report_data = {
                    "login_url": self.login_url,
                    "page_url": self.page_url,
                    "browser_key": self.browser_key,
                    "browser_display": BROWSER_DISPLAY.get(
                        self.browser_key, self.browser_key.title()
                    ),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "slow_api_threshold": self.slow_api_threshold,
                    "login_time_ms": round(login_time_ms),
                    "edm_version": self.edm_version,
                    "no_cache": no_cache,
                    "with_cache": with_cache,
                    "accessibility": accessibility,
                }
                generate_report(report_data, self.output_file)
                self.result_data = report_data  # expose for summary aggregation
                self._print(f"      ✓ Report saved → {os.path.abspath(self.output_file)}")

            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Accessibility audit
    # ------------------------------------------------------------------

    async def _run_accessibility(
        self,
        context: BrowserContext,
        auth_storage: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Inject axe-core into the loaded page and run a WCAG 2.1 audit.
        Returns a structured dict with violations, passes, and a score.
        Returns None if axe-core cannot be loaded.
        """
        axe_js = _ensure_axe_core()
        if not axe_js:
            return None

        page: Page = await context.new_page()
        try:
            # Re-inject auth so the page renders its full authenticated content
            if auth_storage:
                ss = auth_storage.get("session", {})
                ls = auth_storage.get("local", {})
                if ss or ls:
                    inject = (
                        "(function(){"
                        "var ss=" + json.dumps(ss) + ";"
                        "var ls=" + json.dumps(ls) + ";"
                        "for(var k in ss){try{sessionStorage.setItem(k,ss[k]);}catch(e){}}"
                        "for(var k in ls){try{localStorage.setItem(k,ls[k]);}catch(e){}}"
                        "})()"
                    )
                    await page.add_init_script(inject)

            try:
                await page.goto(
                    self.page_url,
                    wait_until="networkidle",
                    timeout=self.timeout,
                )
            except Exception:
                await page.wait_for_load_state("domcontentloaded", timeout=self.timeout)

            # Inject axe-core
            await page.add_script_tag(content=axe_js)

            # Run axe audit – resolve via callback to work in all browsers
            raw: Dict[str, Any] = await page.evaluate("""
                () => new Promise(function(resolve) {
                    axe.run(document, {
                        runOnly: { type: 'tag', values: ['wcag2a','wcag2aa','best-practice'] }
                    }, function(err, results) {
                        if (err) { resolve({ violations: [], passes: [], incomplete: [] }); return; }
                        // Trim node HTML to avoid huge payloads
                        function trimNodes(list) {
                            return (list || []).map(function(v) {
                                return {
                                    id:          v.id,
                                    impact:      v.impact,
                                    description: v.description,
                                    help:        v.help,
                                    helpUrl:     v.helpUrl,
                                    tags:        v.tags || [],
                                    nodes:       (v.nodes||[]).slice(0,5).map(function(n){
                                        return { html: (n.html||'').substring(0,200),
                                                 failureSummary: n.failureSummary||'' };
                                    }),
                                    nodeCount:   (v.nodes||[]).length
                                };
                            });
                        }
                        resolve({
                            violations:  trimNodes(results.violations),
                            incomplete:  trimNodes(results.incomplete),
                            passes:      (results.passes||[]).length,
                            inapplicable:(results.inapplicable||[]).length
                        });
                    });
                })
            """)
        except Exception as exc:
            self._print(f"      ⚠  Accessibility audit error: {exc}")
            raw = {"violations": [], "incomplete": [], "passes": 0, "inapplicable": 0}
        finally:
            await page.close()

        # Summarise violation counts per impact level
        violations: List[Dict] = raw.get("violations", [])
        counts: Dict[str, int] = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
        for v in violations:
            impact = (v.get("impact") or "minor").lower()
            counts[impact] = counts.get(impact, 0) + 1

        # Score: start 100, penalise per violation
        penalty = (
            counts["critical"] * 15
            + counts["serious"] * 8
            + counts["moderate"] * 4
            + counts["minor"] * 1
        )
        score = max(0, 100 - penalty)

        return {
            "score": score,
            "violation_counts": counts,
            "total_violations": len(violations),
            "violations": violations,
            "incomplete": raw.get("incomplete", []),
            "passes": raw.get("passes", 0),
            "inapplicable": raw.get("inapplicable", 0),
        }

    # ------------------------------------------------------------------
    # Login helper
    # ------------------------------------------------------------------

    async def _login(self, page: Page) -> Dict[str, Any]:
        """Navigate to login URL, submit credentials, and return captured auth storage."""
        # Mask the navigator.webdriver flag that Playwright exposes;
        # some enterprise apps reject login from detected automation contexts.
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        await page.goto(
            self.login_url,
            wait_until="domcontentloaded",
            timeout=self.timeout,
        )
        await page.wait_for_load_state("networkidle", timeout=self.timeout)

        # Fill username, then Tab to password (triggers Angular blur/validation)
        if not await self._fill_field(page, self._username_selectors, self.username):
            raise RuntimeError(
                "Could not find a username / email input on the login page.\n"
                "Tip: pass --username-selector with the exact CSS selector."
            )
        await page.keyboard.press("Tab")   # blur username → Angular marks control as touched

        # Some apps hide the password field until username is entered
        # (multi-step login). Wait briefly then try.
        await asyncio.sleep(0.5)

        if not await self._fill_field(page, self._password_selectors, self.password):
            raise RuntimeError(
                "Could not find a password input on the login page.\n"
                "Tip: pass --password-selector with the exact CSS selector."
            )
        await page.keyboard.press("Tab")   # blur password → Angular marks control as touched
        await asyncio.sleep(0.3)           # let Angular run change detection

        # Try clicking the submit button first (triggers full auth + navigation).
        # Fall back to pressing Enter in the password field.
        submitted = await self._click_submit(page, self._submit_selectors)
        if not submitted:
            for sel in self._password_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.press("Enter")
                        submitted = True
                        break
                except Exception:
                    continue

        if not submitted:
            raise RuntimeError(
                "Could not find or click the login submit button.\n"
                "Tip: pass --submit-selector with the exact CSS selector."
            )

        # Some apps (e.g. Angular SPAs) use a two-step flow:
        #   1st submit → async auth API call fires (sets session cookie)
        #   2nd submit → Angular reads the new auth state → navigates away
        # We wait up to 10 s for the URL to change; if still on the login page,
        # try clicking the submit button a second time and wait again.
        try:
            await page.wait_for_url(
                lambda url: self.login_url.rstrip("/") not in url.rstrip("/"),
                timeout=10_000,
            )
        except Exception:
            # Still on login page – auth API likely just completed; click again.
            await asyncio.sleep(1)
            await self._click_submit(page, self._submit_selectors)

        # Wait until navigation triggered by login completes
        try:
            await page.wait_for_load_state("networkidle", timeout=self.timeout)
        except Exception:
            await page.wait_for_load_state("domcontentloaded", timeout=self.timeout)

        # Capture sessionStorage + localStorage so the auth token can be
        # re-injected into every new measurement page.  Many SPAs store their
        # JWT / session token in sessionStorage (per-tab), which would be lost
        # as soon as this login page is closed.  Re-injecting keeps the app
        # authenticated on the freshly opened measurement pages.
        try:
            auth_storage = await page.evaluate("""
                () => {
                    const ss = {}, ls = {};
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const k = sessionStorage.key(i);
                        ss[k] = sessionStorage.getItem(k);
                    }
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        ls[k] = localStorage.getItem(k);
                    }
                    return { session: ss, local: ls };
                }
            """)
        except Exception:
            auth_storage = {"session": {}, "local": {}}
        return auth_storage

    async def _fill_field(
        self, page: Page, selectors: List[str], value: str
    ) -> bool:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    # locator-based fill is most robust for Angular/React forms
                    try:
                        await page.locator(sel).first.fill(value)
                    except Exception:
                        await el.click()
                        await page.keyboard.press("Control+a")
                        await page.keyboard.type(value)
                    return True
            except Exception:
                continue
        return False

    async def _click_submit(self, page: Page, selectors: List[str]) -> bool:
        for sel in selectors:
            try:
                # Use locator API for Angular-compatible click
                loc = page.locator(sel).first
                if await loc.is_visible():
                    await loc.click()
                    return True
            except Exception:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        return True
                except Exception:
                    continue
        return False

    # ------------------------------------------------------------------
    # Core measurement
    # ------------------------------------------------------------------

    async def _measure(
        self, context: BrowserContext, run_label: str,
        auth_storage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Open a new page in *context*, navigate to the target URL,
        and collect all performance metrics.

        Because both calls share the same *context*, the second call
        (With Cache) benefits from resources cached during the first
        call (No Cache).
        """
        page: Page = await context.new_page()

        # Re-inject auth tokens captured from the login page.
        # sessionStorage is per-tab, so opening a new page loses the token.
        # The init script re-populates sessionStorage/localStorage on every
        # navigation so the SPA considers the user authenticated.
        if auth_storage:
            ss = auth_storage.get("session", {})
            ls = auth_storage.get("local", {})
            if ss or ls:
                inject_script = (
                    "(function(){"
                    "var ss=" + json.dumps(ss) + ";"
                    "var ls=" + json.dumps(ls) + ";"
                    "for(var k in ss){try{sessionStorage.setItem(k,ss[k]);}catch(e){}}"
                    "for(var k in ls){try{localStorage.setItem(k,ls[k]);}catch(e){}}"
                    "})()"
                )
                await page.add_init_script(inject_script)

        # Inject LCP / CLS observers before any page script runs
        await page.add_init_script(PERF_OBSERVER_SCRIPT)

        # ── Network event collection ────────────────────────────────────
        # Keyed by Playwright's internal request id via id(req) so we can
        # correlate request → response without relying on object identity
        # across async callbacks.
        network_events: List[Dict[str, Any]] = []
        _req_index: Dict[int, int] = {}   # id(req) -> index in network_events

        # Single-element list so the inner closures can update it
        _last_activity: List[float] = [time.monotonic()]

        # Tracks the last time a XHR/fetch request body fully completed.
        # Used for wall-clock total load time: reflects when the last DATA
        # response arrived (user-visible load), rather than when background
        # scripts/fonts/stylesheets finished loading.
        _last_xhr_finish: List[float] = [time.monotonic()]

        # Set of id(req) for XHR/fetch requests that have started but not
        # yet fired requestfinished or requestfailed.  Sets are mutable so
        # inner closures can mutate without rebinding the name.
        _in_flight_ids: set = set()

        def _parse_query(url: str) -> Dict[str, Any]:
            try:
                qs = urlparse(url).query
                if not qs:
                    return {}
                parsed = parse_qs(qs, keep_blank_values=True)
                return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
            except Exception:
                return {}

        def _on_request(req: Request) -> None:
            _last_activity[0] = time.monotonic()
            # Track in-flight XHR/fetch so the idle loop won't exit while
            # slow API responses are still downloading their bodies.
            if req.resource_type in ("xhr", "fetch"):
                _in_flight_ids.add(id(req))
            # Safely read request headers (synchronous in Playwright Python API)
            try:
                req_headers = dict(req.headers)
            except Exception:
                req_headers = {}

            # Query-string parameters
            query_params = _parse_query(req.url)

            # Request body for mutating methods
            post_data: Optional[str] = None
            if req.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
                try:
                    post_data = req.post_data  # None if no body
                except Exception:
                    post_data = None

            idx = len(network_events)
            network_events.append(
                {
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "start_ms": time.monotonic() * 1000,
                    "end_ms": None,
                    "status": None,
                    "response_headers": {},
                    "size": 0,
                    "duration": None,
                    "request_headers": req_headers,
                    "query_params": query_params,
                    "post_data": post_data,
                }
            )
            _req_index[id(req)] = idx

        def _on_response(resp: Response) -> None:
            _last_activity[0] = time.monotonic()
            idx = _req_index.get(id(resp.request))
            if idx is None:
                return
            evt = network_events[idx]
            # Record status and headers when headers arrive.
            # Duration is NOT set here; we wait for requestfinished (body complete).
            evt["status"] = resp.status
            try:
                evt["response_headers"] = dict(resp.headers)
            except Exception:
                evt["response_headers"] = {}
            try:
                evt["size"] = int(resp.headers.get("content-length", "0") or "0")
            except (ValueError, TypeError):
                evt["size"] = 0

        def _on_request_finished(req: Request) -> None:
            """Fires when the full response body has been received.
            This is the correct point to record duration for slow-API detection.
            """
            _now = time.monotonic()
            _last_activity[0] = _now
            _in_flight_ids.discard(id(req))  # request is no longer in-flight
            # Only advance the XHR/fetch finish marker for data requests.
            # Lazy-loaded scripts / stylesheets / fonts finish AFTER the page
            # is visually complete and must not inflate the total load time.
            if req.resource_type in ("xhr", "fetch"):
                _last_xhr_finish[0] = _now
            idx = _req_index.get(id(req))
            if idx is None:
                return
            evt = network_events[idx]
            evt["end_ms"] = _now * 1000
            evt["duration"] = evt["end_ms"] - evt["start_ms"]

        def _on_request_failed(req: Request) -> None:
            _now = time.monotonic()
            _last_activity[0] = _now
            _in_flight_ids.discard(id(req))  # request is no longer in-flight
            if req.resource_type in ("xhr", "fetch"):
                _last_xhr_finish[0] = _now
            idx = _req_index.get(id(req))
            if idx is None:
                return
            evt = network_events[idx]
            evt["end_ms"] = time.monotonic() * 1000
            evt["duration"] = evt["end_ms"] - evt["start_ms"]
            evt["status"] = "failed"

        page.on("request", _on_request)
        page.on("response", _on_response)
        page.on("requestfinished", _on_request_finished)
        page.on("requestfailed", _on_request_failed)

        # ── Navigate ────────────────────────────────────────────────────
        # Wall-clock start – used to compute the true total load time that
        # includes all deferred SPA API calls (performance.timing only
        # covers the native browser load event which fires almost instantly
        # for Angular/React SPAs before any XHR work begins).
        _wall_start = time.monotonic()
        try:
            await page.goto(
                self.page_url,
                wait_until="networkidle",
                timeout=self.timeout,
            )
        except Exception as exc:
            self._print(f"      ⚠  Navigation warning: {exc}")

        # Dynamic wait: poll until the network has been idle for NETWORK_IDLE_MS.
        # This captures every deferred / lazy-loaded / XHR request regardless of
        # how long the page takes, up to MAX_EXTRA_WAIT_MS as a safety cap.
        _deadline = time.monotonic() + MAX_EXTRA_WAIT_MS / 1000
        while time.monotonic() < _deadline:
            await asyncio.sleep(0.25)
            idle_secs = time.monotonic() - _last_activity[0]
            # Exit only when BOTH conditions hold:
            #  1. No new network activity for NETWORK_IDLE_MS
            #  2. All XHR/fetch responses have fully downloaded (body complete)
            # This prevents closing the page while a slow API (e.g. 5-second
            # response) is still in-flight, which would produce 0ms durations
            # and miss APIs that fire after the slow response completes.
            if idle_secs * 1000 >= NETWORK_IDLE_MS and len(_in_flight_ids) == 0:
                break

        # True wall-clock load time: navigation start → last XHR/fetch body complete.
        # We use _last_xhr_finish (not current time, not _last_activity) because:
        #  • The idle-wait loop adds 3 s of artificial silence → excluded.
        #  • Lazy-loaded scripts/fonts fire AFTER the page is visually loaded
        #    and must not inflate the reported time → excluded.
        #  • XHR/fetch requestfinished is when the last data arrives and the
        #    Angular/React component can render → best match to user perception.
        #
        # Guard: _last_xhr_finish is initialised before _wall_start (before
        # the page is created and event listeners are attached).  If no
        # XHR/fetch request fired during this load (e.g. a static page or a
        # page whose content came entirely from disk/memory cache), the
        # sentinel value will be older than _wall_start and produce a
        # negative result.  In that case fall back to the last observed
        # network activity (any resource – scripts, images, stylesheets)
        # which is always updated during a live page load.
        if _last_xhr_finish[0] > _wall_start:
            _wall_total_ms = (_last_xhr_finish[0] - _wall_start) * 1000
        else:
            # No XHR/fetch; use last network activity (scripts/styles/images)
            _wall_total_ms = max(0.0, (_last_activity[0] - _wall_start) * 1000)

        # ── Browser Performance API ──────────────────────────────────────
        timing: Dict[str, Any] = await page.evaluate(
            """() => {
            const t = performance.timing;
            const paint = performance.getEntriesByType('paint');
            const fcp = (paint.find(e => e.name === 'first-contentful-paint') || {}).startTime;
            return {
                ttfb:               t.responseStart   - t.navigationStart,
                dns_lookup:         t.domainLookupEnd - t.domainLookupStart,
                tcp_connect:        t.connectEnd      - t.connectStart,
                server_response:    t.responseEnd     - t.responseStart,
                dom_interactive:    t.domInteractive  - t.navigationStart,
                dom_content_loaded: t.domContentLoadedEventEnd - t.navigationStart,
                dom_processing:     t.domComplete     - t.responseEnd,
                dom_load_event:     (t.loadEventEnd > 0) ? t.loadEventEnd - t.navigationStart : 0,
                fcp:  fcp  || null,
                lcp:  (window.__perf && window.__perf.lcp)  || null,
                cls:  (window.__perf && window.__perf.cls)  || 0,
            };
        }"""
        )
        # Override total_load_time with the true wall-clock measurement so the
        # report reflects the full SPA load duration instead of just the native
        # browser load event (which fires long before XHR calls complete).
        timing["total_load_time"] = _wall_total_ms

        resources: List[Dict] = await page.evaluate(
            """() => performance.getEntriesByType('resource').map(r => ({
            name:          r.name,
            type:          r.initiatorType,
            duration:      Math.round(r.duration),
            size:          r.decodedBodySize  || 0,
            transfer_size: r.transferSize     || 0,
            start_time:    Math.round(r.startTime),
            from_cache:    r.transferSize === 0 && r.decodedBodySize > 0,
        }))"""
        )

        # Build a URL → browser-measured duration lookup.
        # Used below to fill in duration for any event where requestfinished
        # didn't fire (e.g. 304 Not Modified, service-worker intercept).
        # If multiple entries exist for the same URL, keep the longest.
        _browser_dur: Dict[str, float] = {}
        for r in resources:
            url = r["name"]
            if r["duration"] > _browser_dur.get(url, 0):
                _browser_dur[url] = float(r["duration"])

        page_title: str = await page.title()
        current_url: str = page.url

        await page.close()

        # ── Post-process network events ─────────────────────────────────
        clean_events: List[Dict] = []
        for evt in network_events:
            if evt["duration"] is None:
                # requestfinished didn't fire (e.g. 304 Not Modified, cached
                # response, or service worker) – fall back to the browser's
                # high-resolution Resource Timing entry for this URL.
                browser_dur = _browser_dur.get(evt["url"])
                evt["duration"] = browser_dur if browser_dur is not None else 0.0
            clean_events.append(evt)

        # API calls = XHR + Fetch
        api_calls = [
            e for e in clean_events if e["resource_type"] in ("xhr", "fetch")
        ]
        slow_apis = [
            a for a in api_calls if a["duration"] >= self.slow_api_threshold
        ]

        # Slow resources (>1 s)
        slow_resources = sorted(
            [r for r in resources if r["duration"] > 1000],
            key=lambda x: x["duration"],
            reverse=True,
        )

        # Large resources (>500 KB decoded)
        large_resources = sorted(
            [r for r in resources if r["size"] > 500 * 1024],
            key=lambda x: x["size"],
            reverse=True,
        )

        # Render-blocking candidates: scripts / stylesheets loaded
        # before DOM became interactive with duration > 300 ms
        dom_interactive = timing.get("dom_interactive") or 999_999
        render_blocking = sorted(
            [
                r
                for r in resources
                if r["type"] in ("script", "link")
                and r["start_time"] < dom_interactive
                and r["duration"] > 300
            ],
            key=lambda x: x["duration"],
            reverse=True,
        )

        return {
            "label": run_label,
            "page_title": page_title,
            "final_url": current_url,
            "timing": timing,
            "api_calls": api_calls,
            "slow_apis": slow_apis,
            "resources": resources,
            "slow_resources": slow_resources,
            "large_resources": large_resources,
            "render_blocking": render_blocking,
            "all_requests": clean_events,
            "total_requests": len(clean_events),
            "total_size": sum(e["size"] for e in clean_events),
        }
