"""Shared bridge to a dedicated, locally authorized Chromium profile via CDP."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import time
from html import escape
from urllib.parse import urlsplit
from litbridge.errors import BridgeError, Code
from litbridge.transport import HTTP, MAX_DOWNLOAD, URLPolicy


class BrowserBridge:
    def __init__(self, endpoint: str, *, context_index=0, verification_mode='selectors', human_wait_seconds=300):
        p = urlsplit(endpoint)
        if (p.scheme not in ('http', 'ws') or p.hostname not in ('127.0.0.1', '::1', 'localhost')
                or not p.port or p.username or p.password or p.query or p.fragment):
            raise BridgeError(Code.UNSAFE_URL, 'CDP must be an explicit loopback endpoint without credentials')
        if context_index < 0:
            raise BridgeError(Code.INVALID_INPUT, 'Browser context index must be nonnegative')
        self.endpoint, self.context_index = endpoint, context_index
        self.playwright = self.browser = None
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.verification_mode, self.human_wait_seconds = verification_mode, human_wait_seconds

    async def _pace(self):
        await asyncio.sleep(max(0.0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + 1.0

    async def _context(self):
        if self.browser is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise BridgeError(Code.NOT_CONFIGURED, 'Install litbridge[browser] for the browser provider') from exc
            self.playwright = await async_playwright().start()
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(self.endpoint, timeout=10000)
            except Exception as exc:
                await self.playwright.stop()
                self.playwright = None
                raise BridgeError(Code.NOT_CONFIGURED, 'Cannot connect to the dedicated local browser',
                                  action='Start Chrome/Edge with a dedicated profile and loopback CDP port') from exc
        if self.context_index >= len(self.browser.contexts):
            raise BridgeError(Code.NOT_CONFIGURED, 'Configured browser context is unavailable')
        return self.browser.contexts[self.context_index]

    async def _new_page(self):
        try:
            return await (await self._context()).new_page()
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(Code.NETWORK, 'Cannot open a verification tab; retry after reopening the dedicated browser', retryable=True) from exc

    async def snapshot(self, url: str, policy: URLPolicy, *, ready: str, challenge: str,
                       resources: URLPolicy | None = None) -> tuple[str, str]:
        policy.check(url)
        async with self.lock:
            await self._pace()
            page = await self._new_page()
            try:
                async def route_guard(route):
                    try:
                        request = route.request
                        main = request.is_navigation_request() and request.frame == page.main_frame
                        navigation = main and request.method == 'GET'
                        checked = (policy.navigation_url(request.url) if navigation else
                                   (policy if main else resources or policy).check(request.url))
                        if checked != request.url:
                            # A fresh document navigation stays subject to Playwright routing;
                            # HTTP redirect chains otherwise bypass subsequent route handlers.
                            await route.fulfill(status=200, content_type='text/html', body=
                                '<meta http-equiv="refresh" content="0;url=' + escape(checked, quote=True) + '">')
                            return
                    except BridgeError:
                        await route.abort()
                    else:
                        await route.fallback()
                await page.route('**/*', route_guard)
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=18000)
                except Exception as exc:
                    # Classify only; never return the raw browser exception (it includes full URLs).
                    message = str(exc)
                    if page.is_closed():
                        raise BridgeError(Code.HUMAN_REQUIRED, 'Verification tab/browser was closed; retry manually') from exc
                    if 'ERR_BLOCKED_BY_ADMINISTRATOR' in message:
                        raise BridgeError(Code.ACCESS_DENIED, 'Local browser policy blocked navigation',
                                          action='Use an administrator-approved browser environment') from exc
                    if 'Timeout' in type(exc).__name__:
                        raise BridgeError(Code.TIMEOUT, 'Browser navigation timed out', retryable=True) from exc
                    raise BridgeError(Code.NETWORK, 'Browser navigation failed', retryable=True) from exc
                policy.check(page.url)
                # Poll known result/challenge elements, not networkidle or arbitrary long sleeps.
                stable = self.verification_mode == 'stable'
                deadline = time.monotonic() + (self.human_wait_seconds if stable else 6)
                if stable:
                    await page.bring_to_front()
                while time.monotonic() < deadline:
                    if page.is_closed():
                        raise BridgeError(Code.HUMAN_REQUIRED, 'Manual verification tab was closed')
                    if policy.navigation_url(page.url) != page.url:
                        await asyncio.sleep(0.1)
                        continue
                    challenged = False
                    if challenge and await page.locator(challenge).count():
                        for element in await page.locator(challenge).all():
                            if await element.is_visible():
                                challenged = True
                                if not stable:
                                    raise BridgeError(Code.HUMAN_REQUIRED, 'Provider requires human login or verification',
                                        action='Complete it manually in the dedicated browser, then retry')
                    if not challenged and await page.locator(ready).count():
                        html = await page.content()
                        if len(html.encode('utf-8')) > 4 * 1024 * 1024:
                            raise BridgeError(Code.TOO_LARGE, 'Browser document is too large')
                        return html, page.url
                    await asyncio.sleep(0.25)
                raise BridgeError(Code.HUMAN_REQUIRED if stable else Code.UPSTREAM_CHANGED, 'Expected browser page elements were not found before the verification deadline',
                                  action='Inspect the provider recipe; no empty-success result was fabricated')
            except Exception as exc:
                if page.is_closed():
                    raise BridgeError(Code.HUMAN_REQUIRED, 'Verification tab/browser was closed; retry manually') from exc
                raise
            finally:
                # Never close the user browser or existing tabs.
                with suppress(Exception):
                    await page.close()

    async def retrieve(self, url: str, policy: URLPolicy) -> bytes:
        policy.check(url)
        async with self.lock:
            await self._pace()
            context = await self._context()
            # Cookies are selected by the browser for this exact URL, kept in memory, never returned.
            cookies = await context.cookies([url])
            headers = {'Cookie': '; '.join(c['name'] + '=' + c['value'] for c in cookies)}
            client = HTTP(policy, headers=headers)
            try:
                return await client.request('GET', url, accept='application/pdf', max_bytes=MAX_DOWNLOAD)
            finally:
                await client.close()

    async def health(self):
        async with self.lock:
            await self._context()
        return {'state': 'connected', 'check': 'local_cdp', 'live_checked': True,
                'publisher_session_verified': False}

    async def close(self):
        # Playwright transport shutdown disconnects CDP; browser.close would close the user's browser.
        if self.playwright:
            await self.playwright.stop()
        self.browser = self.playwright = None
