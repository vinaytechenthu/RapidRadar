"""
performance_framework/crawler.py
==================================
Logs in to a web application and discovers all navigable internal pages
via BFS link-crawling using Playwright.

Designed to work with Angular / React SPAs that:
 - Use pushState HTML5 routing (not hash routing)
 - Have WebSocket / long-polling connections (no networkidle)
 - Render navigation items dynamically (sidebar loaded from API)
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .tester import (
    BROWSER_ENGINE,
    PASSWORD_SELECTORS,
    SUBMIT_SELECTORS,
    USERNAME_SELECTORS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# File extensions that indicate non-page resources (skip these)
SKIP_EXTENSIONS: Set[str] = {
    ".css", ".js", ".json", ".xml", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar",
    ".mp4", ".mp3", ".avi", ".mov", ".wav",
    ".woff", ".woff2", ".ttf", ".eot",
}

# URL prefixes that are never navigable pages
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")

MAX_PAGES_DEFAULT = 100
MAX_DEPTH_DEFAULT = 3

# Seconds to wait for Angular/React to finish rendering after domcontentloaded
SPA_SETTLE_SECS = 2

# Selectors that identify navigation / sidebar links in common UI frameworks
NAV_LINK_SELECTORS = [
    # Generic semantic
    "nav a[href]",
    "[role='navigation'] a[href]",
    "[role='menubar'] a[href]",
    "[role='menu'] a[href]",
    # Common CSS class patterns
    ".sidebar a[href]",
    ".sidenav a[href]",
    ".nav a[href]",
    ".menu a[href]",
    ".navbar a[href]",
    ".navigation a[href]",
    "#sidebar a[href]",
    "#sidenav a[href]",
    "#nav a[href]",
    # Angular Material
    "mat-nav-list a[href]",
    "mat-list-item a[href]",
    "mat-tree-node a[href]",
    "mat-expansion-panel a[href]",
    # Angular routerLink (rendered as attribute on any element)
    "[routerlink]",
    "[ng-reflect-router-link]",
]


# ---------------------------------------------------------------------------
# PageDiscoverer
# ---------------------------------------------------------------------------

class PageDiscoverer:
    """
    Logs in to a web application and discovers all navigable pages via BFS
    link crawling starting from the post-login landing page.

    Designed for Angular/React SPAs where:
    - networkidle never fires due to WebSockets / long-polling
    - Navigation menus are rendered dynamically after API calls
    """

    def __init__(
        self,
        login_url: str,
        username: str,
        password: str,
        browser: str = "chrome",
        max_pages: int = MAX_PAGES_DEFAULT,
        max_depth: int = MAX_DEPTH_DEFAULT,
        headless: bool = False,
        timeout: int = 60_000,
        extra_urls: Optional[List[str]] = None,
    ) -> None:
        self.login_url = login_url
        self.username = username
        self.password = password
        self.browser_key = browser.lower()
        self.engine = BROWSER_ENGINE.get(self.browser_key, "chromium")
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.headless = headless
        self.timeout = timeout
        self.extra_urls: List[str] = list(extra_urls or [])
        parsed = urlparse(login_url)
        self._base_origin = f"{parsed.scheme}://{parsed.netloc}"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def discover(self) -> List[str]:
        """Synchronous entry point – returns a list of discovered page URLs."""
        return asyncio.run(self._discover_async())

    # ------------------------------------------------------------------
    # Async pipeline
    # ------------------------------------------------------------------

    async def _discover_async(self) -> List[str]:
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
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ]

            browser: Browser = await launcher.launch(**launch_kwargs)
            try:
                # viewport=None lets --start-maximized determine the window size
                context: BrowserContext = await browser.new_context(
                    viewport=None,
                    ignore_https_errors=True,
                )

                print("  [DISCOVER] Logging in …")
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
                auth_storage = await self._login(login_page)
                start_url = self._normalize_url(login_page.url)
                login_normalized = self._normalize_url(self.login_url)

                # If the URL didn't navigate away from the login page, the SPA
                # may use internal routing without changing the address bar, or
                # the auth redirect is still pending. Fall back to the app root.
                if start_url == login_normalized:
                    start_url = self._base_origin
                    print(f"  [DISCOVER] Post-login URL unchanged; "
                          f"starting crawl from app root: {start_url}")
                else:
                    print(f"  [DISCOVER] Post-login landing page: {start_url}")
                await login_page.close()

                discovered = await self._crawl(context, start_url, auth_storage)

                # Append any user-supplied extra URLs that weren't already found
                discovered_set = set(discovered)
                for extra in self.extra_urls:
                    try:
                        norm = self._normalize_url(extra)
                        if norm and self._is_crawlable(norm) and norm not in discovered_set:
                            discovered.append(norm)
                            discovered_set.add(norm)
                            print(f"  [DISCOVER] Added extra URL: {norm}")
                    except Exception:
                        pass

                print(f"  [DISCOVER] Found {len(discovered)} page(s) to test")
                return discovered
            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # SPA-safe page navigation
    # ------------------------------------------------------------------

    async def _goto_spa(self, page: Page, url: str) -> bool:
        """
        Navigate to *url* in a way that works for Angular/React SPAs.

        Strategy:
        1. goto with wait_until="domcontentloaded" (capped at 30 s)
        2. Wait up to 5 s for the Angular Router to finish any pushState redirect
           (e.g. visiting /login while authenticated redirects to /dashboard)
        3. Try a brief networkidle settle (max 8 s) – fine to ignore timeout
        4. Sleep SPA_SETTLE_SECS to let the framework render the navigation

        Returns True if the page loaded (even partially), False on hard error.
        """
        GOTO_TIMEOUT = min(self.timeout, 30_000)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
        except Exception as exc:
            err = str(exc)
            # Hard navigation errors – give up immediately
            if any(k in err for k in ("net::", "ERR_CONNECTION", "ERR_NAME", "SSL", "ECONNREFUSED")):
                return False
            # Soft timeout / other – page may still have partial content, continue

        # Wait a few seconds for Angular Router to execute any client-side redirect
        # that fires after domcontentloaded (e.g. auth guard → /dashboard)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        await asyncio.sleep(2)   # let Angular zone stabilise

        # Extra settle so the sidebar / navigation menus fully render
        await asyncio.sleep(SPA_SETTLE_SECS)
        return True

    # ------------------------------------------------------------------
    # Login  (mirrors PerformanceTester._login logic)
    # ------------------------------------------------------------------

    async def _login(self, page: Page) -> Dict[str, Any]:
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        await page.goto(
            self.login_url, wait_until="domcontentloaded", timeout=self.timeout
        )
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        if not await self._fill_field(page, USERNAME_SELECTORS, self.username):
            raise RuntimeError(
                "Could not find a username/email input on the login page.\n"
                "Tip: verify the login URL is correct."
            )
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.8)   # let Angular mark username control as touched

        if not await self._fill_field(page, PASSWORD_SELECTORS, self.password):
            raise RuntimeError(
                "Could not find a password input on the login page.\n"
                "Tip: verify the login URL is correct."
            )
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.5)   # let Angular run change detection

        # Try submit button first, fall back to Enter key in password field
        submitted = await self._click_submit(page, SUBMIT_SELECTORS)
        if not submitted:
            for sel in PASSWORD_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.press("Enter")
                        submitted = True
                        break
                except Exception:
                    continue

        if not submitted:
            raise RuntimeError("Could not submit the login form.")

        login_base_normalized = self._normalize_url(self.login_url)

        # Wait up to 30 s for the SPA to navigate away from the login page.
        # Do NOT double-click submit – a second click can cancel Angular's
        # in-flight auth API request.
        try:
            await page.wait_for_url(
                lambda u: self._normalize_url(u) != login_base_normalized,
                timeout=30_000,
            )
        except Exception:
            # URL didn't change in 30 s – try pressing Enter as a last resort
            for sel in PASSWORD_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.press("Enter")
                        break
                except Exception:
                    continue
            # Give it another 15 s
            try:
                await page.wait_for_url(
                    lambda u: self._normalize_url(u) != login_base_normalized,
                    timeout=15_000,
                )
            except Exception:
                print(f"  [DISCOVER] WARNING: URL still at login page after submit. "
                      f"Current URL: {page.url}")
                print(f"  [DISCOVER]   Page title: {await page.title()}")

        # Wait for the SPA to finish loading the post-login page
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass   # SPA with WebSockets – networkidle may never fire
        await asyncio.sleep(SPA_SETTLE_SECS)

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

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    async def _fill_field(
        self, page: Page, selectors: List[str], value: str
    ) -> bool:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
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

    async def _click_submit(
        self, page: Page, selectors: List[str]
    ) -> bool:
        for sel in selectors:
            try:
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
    # URL helpers
    # ------------------------------------------------------------------

    def _normalize_url(self, url: str) -> str:
        """Remove fragment and trailing slash for consistent deduplication."""
        parsed = urlparse(url)
        clean = parsed._replace(fragment="").geturl()
        return clean.rstrip("/")

    def _is_crawlable(self, url: str) -> bool:
        """Return True if the URL is an internal page worth crawling."""
        if not url:
            return False
        for scheme in SKIP_SCHEMES:
            if url.startswith(scheme):
                return False
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        # Must be same origin
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin != self._base_origin:
            return False
        # Skip static resource files
        path = parsed.path.lower()
        last_segment = path.rsplit("/", 1)[-1]
        if "." in last_segment:
            ext = "." + last_segment.rsplit(".", 1)[-1]
            if ext in SKIP_EXTENSIONS:
                return False
        return True

    # ------------------------------------------------------------------
    # Link extraction
    # ------------------------------------------------------------------

    async def _extract_links(self, page: Page) -> List[str]:
        """
        Extract every internal navigable link from the rendered SPA page.

        Sources checked:
        - All <a href> tags (anywhere in the DOM)
        - Angular [routerLink] and [ng-reflect-router-link] attributes
          (handles both absolute and relative paths; resolves relative paths
          against the current page's directory, matching Angular Router semantics)
        - Any element with data-href / data-url attributes
        """
        base_url = page.url
        try:
            raw_links: List[str] = await page.evaluate("""
                () => {
                    const links = new Set();

                    // Current page's directory path – used to resolve relative routerLinks.
                    // e.g. on /workflow  → currentDir = '/workflow/'
                    // Angular resolves relative routerLink="Fund Matcher" to /workflow/Fund Matcher
                    const p = window.location.pathname;
                    const currentDir = p.endsWith('/') ? p : p.replace(/\\/[^\\/]*$/, '/');

                    // Helper: parse Angular routerLink / ng-reflect-router-link value.
                    // Handles both string ("/workflow") and array notation
                    // ("/workflow,Fund Matcher" → "/workflow/Fund Matcher").
                    // Returns the resolved absolute path string.
                    function parseRouterLink(v) {
                        if (!v) return null;
                        v = v.trim().replace(/^\\[|\\]$/g, '').replace(/^['"]|['"]$/g, '');
                        let path;
                        if (v.includes(',')) {
                            const parts = v.split(',').map(s => s.trim().replace(/^['"]|['"]$/g, ''));
                            // First part may be absolute ('/workflow') or relative ('Fund Matcher')
                            if (parts[0].startsWith('/') || parts[0].startsWith('http')) {
                                path = parts.join('/');
                            } else {
                                path = currentDir + parts.join('/');
                            }
                        } else if (v.startsWith('/') || v.startsWith('http')) {
                            path = v;
                        } else if (v && !v.startsWith('#') && !v.startsWith('?')) {
                            // Relative segment: resolve against current directory
                            path = currentDir + v;
                        } else {
                            return null;
                        }
                        return path;
                    }

                    // 1. All standard anchor hrefs
                    document.querySelectorAll('a[href]').forEach(a => {
                        const h = a.getAttribute('href');
                        if (h) links.add(h);
                    });

                    // 2. Angular routerLink attribute (handles absolute + relative paths)
                    document.querySelectorAll('[routerlink]').forEach(el => {
                        const v = el.getAttribute('routerlink');
                        const parsed = parseRouterLink(v);
                        if (parsed) links.add(parsed);
                    });

                    // 3. Angular dev-mode reflection (ng-reflect-router-link)
                    document.querySelectorAll('[ng-reflect-router-link]').forEach(el => {
                        const v = el.getAttribute('ng-reflect-router-link');
                        const parsed = parseRouterLink(v);
                        if (parsed) links.add(parsed);
                    });

                    // 4. Any element with a data-href or data-url attribute
                    document.querySelectorAll('[data-href], [data-url]').forEach(el => {
                        const v = el.getAttribute('data-href') || el.getAttribute('data-url');
                        if (v) links.add(v);
                    });

                    return Array.from(links);
                }
            """)
        except Exception:
            raw_links = []

        links: List[str] = []
        for href in raw_links:
            try:
                # For bare relative paths (no leading / or scheme), urljoin treats
                # the last path segment of base_url as a "file" and replaces it.
                # e.g. urljoin('http://host/workflow', 'Fund Matcher') → /Fund Matcher (WRONG)
                # Fix: ensure base ends with '/' so urljoin resolves correctly:
                # urljoin('http://host/workflow/', 'Fund Matcher') → /workflow/Fund Matcher
                if href and not href.startswith(("/", "http", "#", "?",
                                                 "mailto:", "javascript:", "data:")):
                    join_base = base_url if base_url.endswith("/") else base_url + "/"
                else:
                    join_base = base_url
                absolute = urljoin(join_base, href)
                normalized = self._normalize_url(absolute)
                if self._is_crawlable(normalized):
                    links.append(normalized)
            except Exception:
                continue
        return list(set(links))

    # ------------------------------------------------------------------
    # Nav-click discovery: expand sidebar items to reveal sub-routes
    # ------------------------------------------------------------------

    async def _harvest_submenu(
        self,
        page: Page,
        original_url: str,
        visited: Set[str],
    ) -> Set[str]:
        """
        Extract all navigable URLs from the currently-visible .mmMenuSubs element.

        Phase A  — fast DOM-only read: collects href / routerLink / ng-reflect-router-link
                   values without any page navigation.
        Phase B  — click-and-follow: for items that have no discoverable href in Phase A,
                   click each one, record the resulting URL, then navigate back.
        """
        found: Set[str] = set()

        # ── Phase A: DOM extraction (zero navigation) ──────────────────────
        try:
            raw_links: List[str] = await page.evaluate("""
                () => {
                    const links = new Set();
                    const p = window.location.pathname;
                    const currentDir = p.endsWith('/') ? p : p.replace(/\\/[^\\/]*$/, '/');

                    function parseRL(v) {
                        if (!v) return null;
                        v = v.trim().replace(/^\\[|\\]$/g, '').replace(/^['"]|['"]$/g, '');
                        if (v.includes(',')) {
                            const parts = v.split(',').map(s => s.trim().replace(/^['"]|['"]$/g, ''));
                            return (parts[0].startsWith('/') || parts[0].startsWith('http'))
                                ? parts.join('/') : currentDir + parts.join('/');
                        }
                        if (v.startsWith('/') || v.startsWith('http')) return v;
                        if (v && !v.startsWith('#') && !v.startsWith('?')) return currentDir + v;
                        return null;
                    }

                    // Scan ALL descendants of every .mmMenuSubs element —
                    // anchors, routerLink, ng-reflect — regardless of nesting depth.
                    document.querySelectorAll('.mmMenuSubs, div.mmMenuSubs').forEach(function(container) {
                        container.querySelectorAll('a[href]').forEach(function(a) {
                            var h = a.getAttribute('href');
                            if (h) links.add(h);
                        });
                        container.querySelectorAll('[routerlink],[ng-reflect-router-link]').forEach(function(el) {
                            var v = el.getAttribute('routerlink') || el.getAttribute('ng-reflect-router-link');
                            var parsed = parseRL(v);
                            if (parsed) links.add(parsed);
                        });
                        // Also capture text-content of any clickable leaf element
                        // that may carry a router directive without an attribute.
                        container.querySelectorAll('li,span,div').forEach(function(el) {
                            // Only leaves (no child elements with text)
                            if (el.children.length === 0) return;
                            var v = el.getAttribute('routerlink') || el.getAttribute('ng-reflect-router-link');
                            if (v) { var parsed = parseRL(v); if (parsed) links.add(parsed); }
                        });
                    });
                    return Array.from(links);
                }
            """)
            base_url = page.url
            for href in raw_links:
                try:
                    join_base = base_url if base_url.endswith("/") else base_url + "/"
                    absolute = (
                        urljoin(join_base, href)
                        if not href.startswith(("/", "http"))
                        else urljoin(base_url, href)
                    )
                    normalized = self._normalize_url(absolute)
                    if self._is_crawlable(normalized):
                        found.add(normalized)
                except Exception:
                    continue
        except Exception:
            pass

        # ── Phase B: Click-and-follow for items with no extractable href ───
        # Use index-based Playwright locators so DOM elements are re-evaluated
        # lazily after each navigation (avoids stale-handle errors).
        sub_sel: str = ""
        sub_count: int = 0
        for sel, min_cnt in [
            (".mmMenuSubs > * > *",   2),   # grandchildren  (real items)
            ("div.mmMenuSubs > * > *", 2),
            (".mmMenuSubs li",         2),
            (".mmMenuSubs a",          2),
            (".mmMenuSubs > * > *",   1),   # grandchildren fallback (1 item)
            ("div.mmMenuSubs > * > *", 1),
            (".mmMenuSubs li",         1),
            (".mmMenuSubs > *",        1),   # direct child last resort
            ("div.mmMenuSubs > *",     1),
        ]:
            try:
                candidates = await page.query_selector_all(sel)
                visible = [c for c in candidates if await c.is_visible()]
                if len(visible) >= min_cnt:
                    sub_sel = sel
                    sub_count = len(visible)
                    break
            except Exception:
                continue

        if sub_sel:
            print(f"  [DISCOVER] mmMenuSubs: {sub_count} item(s) to harvest")

        clicked_labels: Set[str] = set()
        item_idx = 0
        while item_idx < sub_count:
            try:
                # Re-evaluate fresh element at this index each iteration
                locator = page.locator(sub_sel).nth(item_idx)
                item_idx += 1
                try:
                    visible_check = await locator.is_visible(timeout=2000)
                except Exception:
                    continue
                if not visible_check:
                    continue
                try:
                    label = ((await locator.text_content(timeout=2000)) or "").strip()[:80]
                except Exception:
                    label = ""
                if label and label in clicked_labels:
                    continue
                if label:
                    clicked_labels.add(label)

                pre_url = self._normalize_url(page.url)
                try:
                    await locator.click(timeout=3000)
                except Exception:
                    continue
                await asyncio.sleep(1.2)

                new_norm = self._normalize_url(page.url)
                if new_norm != pre_url and self._is_crawlable(new_norm):
                    found.add(new_norm)

                # Always extract links from the re-rendered page content —
                # even when the URL didn't change.  Category-filter items
                # (e.g. Security/Party/Fund) stay at the same URL but
                # re-render the main content with workflow card links that
                # lead to sub-pages like /workflow/Fund Matcher.
                try:
                    bonus = await self._extract_links(page)
                    for lnk in bonus:
                        if self._is_crawlable(lnk):
                            found.add(lnk)
                except Exception:
                    pass

                if self._normalize_url(page.url) != self._normalize_url(original_url):
                    try:
                        await page.goto(
                            original_url,
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                        await asyncio.sleep(1.5)
                        # Refresh item count after navigation (DOM may change)
                        try:
                            sub_count = await page.locator(sub_sel).count()
                        except Exception:
                            pass
                    except Exception:
                        return found
            except Exception:
                continue

        return found

    async def _discover_via_nav_clicks(
        self,
        page: Page,
        visited: Set[str],
    ) -> List[str]:
        """
        Aggressively expand all navigation and discover all reachable routes.

        Phase 0  — App-specific: click every icon in div.navMenus, then harvest
                   all links from div.mmMenuSubs after each click.  Also harvest
                   mmMenuSubs in its current state (before any icon click) in case
                   the menu is already open on arrival.
        Phase 1  — Scroll sidebar to trigger virtualised rendering.
        Phase 2  — Multi-pass accordion expansion (Angular Material + ARIA).
        Phase 3  — DOM link extraction after full expansion.
        Phase 4  — routerLink / ng-reflect-router-link DOM scan.
        Phase 5  — Click-and-follow non-anchor routerLink nav items.
        Phase 6  — Angular router introspection via ng.getInjector().
        """
        found: Set[str] = set()
        original_url = self._normalize_url(page.url)

        # ── Phase 0: navMenus icons  →  mmMenuSubs harvest ────────────────
        # Harvest mmMenuSubs in its current state first (menu may already be open)
        initial_sub = await self._harvest_submenu(page, original_url, visited)
        found.update(initial_sub)

        # Try to find navMenus icons and click each one.
        # Ordered from most-specific (grandchildren = individual icons) to
        # least-specific (direct child = outer container).
        nav_icon_selectors = [
            ("div.navMenus > * > *", 1),    # grandchildren (individual icons)
            (".navMenus > * > *",   1),
            (".navMenus li",        1),
            (".navMenus a",         1),
            (".navMenus button",    1),
            (".navMenus span",      1),
            ("div.navMenus > *",    1),    # direct child (fallback container)
            (".navMenus > *",       1),
            (".navMenus [class]",   1),
        ]

        async def _query_nav_icons() -> List:
            for sel, min_count in nav_icon_selectors:
                try:
                    candidates = await page.query_selector_all(sel)
                    visible = [c for c in candidates
                               if await c.is_visible()  # type: ignore[misc]
                               ]
                except Exception:
                    visible = []
                if len(visible) >= min_count:
                    print(
                        f"  [DISCOVER] navMenus: {len(visible)} icon(s) "
                        f"found via '{sel}'"
                    )
                    return visible
            return []

        # Use a while loop so we can re-query fresh DOM elements after every
        # icon click that causes navigation.  Python for-loops capture the
        # iterable at start; reassigning the list inside the loop has no effect.
        clicked_icon_labels: Set[str] = set()
        nav_icons = await _query_nav_icons()
        icon_index = 0

        while icon_index < len(nav_icons):
            icon = nav_icons[icon_index]
            icon_index += 1
            try:
                label = ((await icon.text_content()) or "").strip()[:60]
                if label and label in clicked_icon_labels:
                    continue
                if label:
                    clicked_icon_labels.add(label)
                if not await icon.is_visible():
                    continue

                pre_url = page.url
                await icon.click()
                await asyncio.sleep(1.0)

                # Icon navigated to a new page → harvest its mmMenuSubs, then return
                if page.url != pre_url:
                    new_norm = self._normalize_url(page.url)
                    if self._is_crawlable(new_norm) and new_norm not in visited:
                        found.add(new_norm)
                    # mmMenuSubs on the navigated page lists the sub-pages for this icon
                    nav_sub = await self._harvest_submenu(
                        page, new_norm or original_url, visited
                    )
                    found.update(nav_sub)
                    try:
                        await page.goto(
                            original_url,
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                        await asyncio.sleep(1.5)
                        # Re-query fresh DOM elements after returning; continue
                        # at icon_index (already incremented) so already-processed
                        # icons are not repeated.
                        fresh = await _query_nav_icons()
                        if fresh:
                            nav_icons = fresh
                    except Exception:
                        break
                    continue

                # Icon did NOT navigate → mmMenuSubs may be visible alongside it
                sub_urls = await self._harvest_submenu(page, original_url, visited)
                found.update(sub_urls)

            except Exception:
                continue

        # ── Phase 1: Scroll sidebar to trigger virtual rendering ──────────
        sidebar_scroll_js = """
            () => {
                const candidates = [
                    'mat-sidenav', 'mat-sidenav-content', '.sidenav', '.sidebar',
                    '.mat-sidenav', '[role="navigation"]', 'nav', '#sidebar',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el && el.scrollHeight > el.clientHeight) {
                        el.scrollTop = el.scrollHeight;
                        break;
                    }
                }
            }
        """
        try:
            await page.evaluate(sidebar_scroll_js)
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # ── Phase 2: Multi-pass accordion expansion ────────────────────────
        # Selectors ordered from most specific to most generic.
        # Two selectors cover the collapsed state: aria-expanded=false AND
        # the absence of the Angular Material .mat-expanded CSS class, so that
        # panels which start without any aria attribute are also caught.
        expand_selectors = [
            # Angular Material – aria attribute present and false
            "mat-expansion-panel-header[aria-expanded='false']",
            ".mat-expansion-panel-header[aria-expanded='false']",
            # Angular Material – no aria-expanded=true AND no .mat-expanded class
            # (catches panels that start without the attribute)
            "mat-expansion-panel-header:not([aria-expanded='true']):not(.mat-expanded)",
            # Angular Material tree toggle buttons
            "mat-tree-node button[aria-label='Toggle']",
            "mat-nested-tree-node button[aria-label='Toggle']",
            # Generic ARIA collapsed buttons / tree items
            "[role='treeitem'][aria-expanded='false']",
            "[role='button'][aria-expanded='false']",
        ]

        for _pass in range(5):   # up to 5 expansion passes for deep nesting
            expanded_any = False
            for sel in expand_selectors:
                try:
                    items = await page.query_selector_all(sel)
                    i = 0
                    while i < len(items):
                        item = items[i]
                        i += 1
                        try:
                            if not await item.is_visible():
                                continue
                            pre_url = page.url
                            await item.click()
                            await asyncio.sleep(0.5)
                            # If click accidentally navigated away, record + go back
                            if page.url != pre_url:
                                if self._is_crawlable(self._normalize_url(page.url)):
                                    found.add(self._normalize_url(page.url))
                                try:
                                    await page.goto(original_url,
                                                    wait_until="domcontentloaded",
                                                    timeout=15_000)
                                    await asyncio.sleep(1.5)
                                except Exception:
                                    return list(found)
                                # Re-query so we continue from a fresh DOM
                                items = await page.query_selector_all(sel)
                                i = 0   # restart from the beginning of this selector
                                continue
                            expanded_any = True
                        except Exception:
                            continue
                except Exception:
                    continue

            if not expanded_any:
                break   # nothing new expanded in this pass
            await asyncio.sleep(0.3)

        # ── Phase 3: Collect all links after full expansion (DOM-only) ────
        all_links = await self._extract_links(page)
        for lnk in all_links:
            if lnk not in visited:
                found.add(lnk)

        # ── Phase 4: Read routerLink attributes directly from the DOM ─────
        # Redundant with Phase 3 but kept as a safety net using the same
        # relative-path resolution logic.
        try:
            extra_hrefs: List[str] = await page.evaluate("""
                () => {
                    const links = new Set();
                    const p = window.location.pathname;
                    const currentDir = p.endsWith('/') ? p : p.replace(/\\/[^\\/]*$/, '/');
                    function parseRL(v) {
                        if (!v) return null;
                        v = v.trim().replace(/^\\[|\\]$/g, '').replace(/^['"]|['"]$/g, '');
                        if (v.includes(',')) {
                            const parts = v.split(',').map(s => s.trim().replace(/^['"]|['"]$/g, ''));
                            return (parts[0].startsWith('/') || parts[0].startsWith('http'))
                                ? parts.join('/') : currentDir + parts.join('/');
                        }
                        if (v.startsWith('/') || v.startsWith('http')) return v;
                        if (v && !v.startsWith('#') && !v.startsWith('?')) return currentDir + v;
                        return null;
                    }
                    document.querySelectorAll('[routerlink],[ng-reflect-router-link]').forEach(el => {
                        const v = el.getAttribute('routerlink') || el.getAttribute('ng-reflect-router-link');
                        const parsed = parseRL(v);
                        if (parsed) links.add(parsed);
                    });
                    return Array.from(links);
                }
            """)
            base_url = page.url
            for href in extra_hrefs:
                try:
                    join_base = base_url if base_url.endswith("/") else base_url + "/"
                    absolute = urljoin(join_base, href) if not href.startswith(("/", "http")) else urljoin(base_url, href)
                    normalized = self._normalize_url(absolute)
                    if self._is_crawlable(normalized) and normalized not in visited:
                        found.add(normalized)
                except Exception:
                    continue
        except Exception:
            pass

        # ── Phase 5: Click-and-follow non-anchor routerLink nav items ─────
        # mat-list-item and similar elements with routerLink don't get an href
        # attribute, so their target URL can only be found by clicking them.
        click_follow_selectors = [
            "mat-list-item[routerlink]:not([href])",
            "mat-list-item[ng-reflect-router-link]:not([href])",
            "mat-nav-list [routerlink]:not(a):not(mat-expansion-panel-header)",
            "[routerlink]:not(a):not([href]):not(mat-expansion-panel-header)",
        ]
        clicked_texts: Set[str] = set()
        for sel in click_follow_selectors:
            try:
                items = await page.query_selector_all(sel)
                for item in items:
                    try:
                        if not await item.is_visible():
                            continue
                        # Deduplicate by text content
                        text = ((await item.text_content()) or "").strip()[:80]
                        if text in clicked_texts:
                            continue
                        clicked_texts.add(text)

                        pre_url = self._normalize_url(page.url)
                        await item.click()
                        await asyncio.sleep(1.0)

                        new_norm = self._normalize_url(page.url)
                        if new_norm != pre_url and self._is_crawlable(new_norm) and new_norm not in visited:
                            found.add(new_norm)
                            # Grab links from the newly navigated page as a bonus
                            bonus = await self._extract_links(page)
                            for lnk in bonus:
                                if lnk not in visited:
                                    found.add(lnk)

                        # Navigate back if we left the original page
                        if self._normalize_url(page.url) != original_url:
                            try:
                                await page.goto(original_url,
                                                wait_until="domcontentloaded",
                                                timeout=15_000)
                                await asyncio.sleep(1.5)
                                # Quick re-expansion so next items are visible
                                for exp_sel in [
                                    "mat-expansion-panel-header[aria-expanded='false']",
                                    "mat-expansion-panel-header:not([aria-expanded='true']):not(.mat-expanded)",
                                ]:
                                    for ei in await page.query_selector_all(exp_sel):
                                        try:
                                            if await ei.is_visible():
                                                await ei.click()
                                                await asyncio.sleep(0.3)
                                        except Exception:
                                            pass
                                await asyncio.sleep(0.5)
                            except Exception:
                                return list(found)
                    except Exception:
                        continue
            except Exception:
                continue

        # ── Phase 6: Angular router introspection ─────────────────────────
        angular_routes = await self._extract_angular_routes(page)
        for route in angular_routes:
            if self._is_crawlable(route) and route not in visited:
                found.add(route)

        return list(found)

    async def _extract_angular_routes(self, page: Page) -> List[str]:
        """
        Try to read all registered routes directly from the Angular Router service.
        Works when Angular's ng.getInjector() is available (dev mode and some
        production builds).  Returns absolute URLs; empty list on failure.
        """
        try:
            raw_paths: List[str] = await page.evaluate("""
                () => {
                    function flattenRoutes(routes, prefix) {
                        const out = [];
                        if (!routes || !Array.isArray(routes)) return out;
                        for (const r of routes) {
                            if (r.path === undefined || r.path === null) continue;
                            // Skip parametric, wildcard and redirect-only routes
                            if (r.path.includes(':') || r.path === '**') continue;
                            if (r.redirectTo !== undefined) continue;
                            const full = r.path === ''
                                ? prefix
                                : (prefix ? prefix + '/' + r.path : '/' + r.path);
                            if (r.path !== '') out.push(full);
                            if (r.children && r.children.length)
                                out.push(...flattenRoutes(r.children, full));
                            if (r._loadedRoutes && r._loadedRoutes.length)
                                out.push(...flattenRoutes(r._loadedRoutes, full));
                        }
                        return out;
                    }
                    try {
                        const roots = (window.getAllAngularRootElements
                            ? window.getAllAngularRootElements()
                            : []).concat(
                                [document.querySelector('[ng-version]'),
                                 document.querySelector('app-root')]
                            ).filter(Boolean);
                        for (const root of roots) {
                            const inj = window.ng && window.ng.getInjector && window.ng.getInjector(root);
                            if (!inj) continue;
                            const recs = inj._records;
                            if (!(recs instanceof Map)) continue;
                            for (const [, rec] of recs) {
                                const v = rec && rec.value;
                                if (v && v.config && Array.isArray(v.config)
                                        && typeof v.navigate === 'function') {
                                    return flattenRoutes(v.config, '');
                                }
                            }
                        }
                    } catch(e) {}
                    return [];
                }
            """)
            if raw_paths and isinstance(raw_paths, list):
                result = []
                for r in raw_paths:
                    if r and isinstance(r, str):
                        path = r if r.startswith("/") else "/" + r
                        result.append(self._normalize_url(self._base_origin + path))
                return result
        except Exception:
            pass
        return []

    # ------------------------------------------------------------------
    # BFS crawl
    # ------------------------------------------------------------------

    async def _inject_auth(
        self, page: Page, auth_storage: Dict[str, Any]
    ) -> None:
        """Re-inject auth tokens from login into a newly opened page."""
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

    async def _crawl(
        self,
        context: BrowserContext,
        start_url: str,
        auth_storage: Dict[str, Any],
    ) -> List[str]:
        """BFS from *start_url*; returns a list of reachable internal pages."""
        visited: Set[str] = set()
        # queue items: (url, depth)
        queue: List[Tuple[str, int]] = [(start_url, 0)]
        discovered: List[str] = []

        while queue and len(discovered) < self.max_pages:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            print(
                f"  [DISCOVER] [{len(discovered) + 1}/{self.max_pages}] "
                f"Scanning (depth {depth}): {url}"
            )

            page: Page = await context.new_page()
            await self._inject_auth(page, auth_storage)

            try:
                ok = await self._goto_spa(page, url)
                if not ok:
                    print(f"  [DISCOVER] Could not load: {url}")
                    continue

                current_url = self._normalize_url(page.url)
                login_normalized = self._normalize_url(self.login_url)

                # If the page (after settling) is the login page, the session
                # token didn't carry over for this URL — skip it.
                if current_url == login_normalized:
                    print(f"  [DISCOVER] Skipping (redirected to login): {url}")
                    continue

                discovered.append(url)

                if depth < self.max_depth:
                    # Standard link extraction (fast, always runs)
                    links = await self._extract_links(page)

                    # Full traversal (navMenus icon clicks + mmMenuSubs harvest +
                    # accordion expansion) runs on depth-0 (landing page) and
                    # depth-1 pages.  Depth-0 seeds all top-level routes; depth-1
                    # catches pages whose sub-menu only appears after navigating
                    # to a parent page (e.g. Fund Matcher is only shown in mmMenuSubs
                    # when the Matchers icon is active on /workflow/Process Management).
                    # Depth >= 2 uses fast DOM extraction only to keep overall run time
                    # reasonable.
                    if depth <= 1:
                        nav_links = await self._discover_via_nav_clicks(page, visited)
                        all_links = list(set(links + nav_links))
                    else:
                        all_links = links

                    for link in all_links:
                        if link not in visited:
                            queue.append((link, depth + 1))

            except Exception as exc:
                print(f"  [DISCOVER] Unexpected error on {url}: {exc}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        return discovered

