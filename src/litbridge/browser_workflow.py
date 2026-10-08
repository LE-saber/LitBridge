"""Shared visible-session workflows; no challenge solving or browser stealth."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import tempfile
import time
from html import escape
from urllib.parse import urljoin
from litbridge.browser import BrowserBridge
from litbridge.errors import BridgeError, Code
from litbridge.models import digest
from litbridge.transport import MAX_DOWNLOAD, URLPolicy, public_url

MANUAL_ERRORS = frozenset((Code.HUMAN_REQUIRED, Code.AUTH_REQUIRED, Code.ACCESS_DENIED,
                          Code.UPSTREAM_CHANGED, Code.UPSTREAM, Code.INVALID_CONTENT, Code.NOT_FOUND,
                          'full_text_unavailable'))


@dataclass(frozen=True)
class BrowserTarget:
    url: str
    policy: URLPolicy
    ready: str
    challenge: str
    download_selector: str
    resources: URLPolicy | None = None
    pdf_identity: Callable[[str], bool] | None = None
    pdf_patterns: tuple[str, ...] = ()


@dataclass
class HumanSession:
    page: object
    download: bytes | None = None


async def visible_challenge(page, selector):
    if not selector:
        return False
    for element in (await page.locator(selector).all())[:30]:
        if await element.is_visible():
            return True
    return False


async def usable_download_link(page, target):
    """Only auto-resume a real URL; a JS-only button must remain available to the human."""
    for node in (await page.locator(target.download_selector).all())[:20]:
        href = await node.get_attribute('href') or await node.get_attribute('content')
        if href and not href.strip().lower().startswith(('javascript:', '#')):
            target.policy.check(urljoin(page.url, href))
            return True
    return False


class Capture:
    """Observe only this workflow's page. Bytes are validated by Store, not by filename."""
    def __init__(self, page, policy, *, future=None, pdf_identity=None):
        self.page, self.policy = page, policy
        self.pdf_identity = pdf_identity
        self.stream = self.stream_tmp = self.stream_target = None
        self.owns_future = future is None
        self.future = future if future is not None else asyncio.get_running_loop().create_future()
        self.tasks, self.downloads = set(), []
        self.children = []
        self.on_download = lambda d: self._spawn(self._download(d))
        self.on_response = lambda r: self._spawn(self._response(r)) if 'application/pdf' in r.headers.get('content-type','').lower() else None
        page.on('download', self.on_download)
        page.on('response', self.on_response)
        def on_popup(child):
            capture = Capture(child, policy, future=self.future, pdf_identity=pdf_identity)
            self.children.append(capture)
            if self.stream_target:
                capture._spawn(capture._popup_stream(self.stream_target))
        self.on_popup = on_popup
        page.on('popup', self.on_popup)

    async def start_stream(self, target):
        if not target.pdf_patterns:
            return
        if target.pdf_identity is None:
            raise BridgeError(Code.INVALID_INPUT, 'Selected PDF response patterns require an article identity check')
        from litbridge.reader_capture import ReaderCapture
        self.stream_target = target
        self.stream_tmp = tempfile.TemporaryDirectory(prefix='litbridge-selected-pdf-')
        self.stream = ReaderCapture(digest(target.url)[:24], Path(self.stream_tmp.name),
            url_matcher=target.pdf_identity, response_patterns=target.pdf_patterns)
        await self.stream.attach(self.page.context, self.page)
        if any(e['kind'] == 'reader_listener_unavailable' for e in self.stream.events):
            raise BridgeError(Code.NOT_CONFIGURED, 'Selected PDF response listener is unavailable in this browser')
        async def receive():
            try:
                self._deliver(await self.stream.future)
            except BridgeError as exc:
                self._error(exc)
        self._spawn(receive())

    async def _popup_stream(self, target):
        try:
            await self.start_stream(target)
        except BridgeError as exc:
            self._error(exc)
        except Exception:
            self._error(BridgeError(Code.NETWORK, 'Cannot attach selected PDF listener to the article popup', retryable=True))

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _deliver(self, value):
        if not self.future.done():
            self.future.set_result(value)

    def _error(self, exc):
        if not self.future.done():
            self.future.set_exception(exc)

    def _bound(self, size):
        if size > MAX_DOWNLOAD:
            raise BridgeError(Code.TOO_LARGE, 'Browser PDF exceeds 32 MiB; original was not accepted')

    async def _download(self, download):
        self.downloads.append(download)
        if self.stream:
            return  # The selected complete response stream owns the receipt.
        try:
            self.policy.check(download.url)
            if self.pdf_identity and not self.pdf_identity(download.url):
                raise BridgeError(Code.UPSTREAM_CHANGED, 'Browser download does not match the selected article')
            with tempfile.TemporaryDirectory(prefix='litbridge-download-') as tmp:
                path = Path(tmp) / 'download.pdf'  # Never use an upstream suggested filename.
                save = asyncio.create_task(download.save_as(str(path)))
                try:
                    async with asyncio.timeout(45):
                        while not save.done():
                            if path.exists():
                                self._bound(path.stat().st_size)
                            await asyncio.sleep(0.1)
                        await save
                    self._bound(path.stat().st_size)
                    self._deliver(path.read_bytes())
                finally:
                    if not save.done():
                        save.cancel()
                        with suppress(asyncio.CancelledError):
                            await save
        except asyncio.CancelledError:
            await download.cancel()
            raise
        except BridgeError as exc:
            await download.cancel()
            self._error(exc)
        except Exception:
            await download.cancel()
            self._error(BridgeError(Code.NETWORK, 'Browser download failed or timed out', retryable=True))
        finally:
            with suppress(Exception):
                await download.delete()

    async def _response(self, response):
        if self.stream:
            return  # Browser PDF viewers may expose HTML here, not original bytes.
        try:
            if not response.request.is_navigation_request():
                return
            if self.pdf_identity:
                # A provider must bind an iframe PDF URL to the selected article.
                # Legacy recipes still accept only the main-frame response.
                if not self.pdf_identity(response.url):
                    return
            elif response.frame != self.page.main_frame:
                return
            self.policy.check(response.url)
            if self.pdf_identity and (response.status == 206 or response.headers.get('content-range')):
                raise BridgeError(Code.INVALID_CONTENT, 'Selected browser PDF response is partial; original was not accepted')
            length = response.headers.get('content-length', '')
            if length.isdigit():
                self._bound(int(length))
            if not 200 <= response.status < 300:
                return
            data = await response.body()
            self._bound(len(data))
            self._deliver(data)
        except BridgeError as exc:
            self._error(exc)
        except Exception:
            pass  # Attachment responses may have no body; the download event owns that case.

    async def close(self):
        self.page.remove_listener('download', self.on_download)
        self.page.remove_listener('response', self.on_response)
        self.page.remove_listener('popup', self.on_popup)
        if self.stream:
            await self.stream.close()
        if self.stream_tmp:
            self.stream_tmp.cleanup()
        for child in self.children:
            await child.close()
            if not child.page.is_closed():
                with suppress(Exception):
                    await child.page.close()
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if not self.owns_future:
            return
        if self.future.done() and not self.future.cancelled():
            self.future.exception()  # Mark a discarded error observed on cleanup.
        else:
            self.future.cancel()


class WorkflowBrowser(BrowserBridge):
    def __init__(self, endpoint, *, context_index=0, verification_mode='selectors', human_wait_seconds=300):
        super().__init__(endpoint, context_index=context_index, verification_mode=verification_mode,
                         human_wait_seconds=human_wait_seconds)
        self._setup()

    def _setup(self):
        self.human_lock = asyncio.Lock()
        self.connection_lock = asyncio.Lock()
        self.session_context = ContextVar('litbridge-human-session', default=None)
        self.active = self.active_target = self.active_owner = self.active_capture = None

    async def _context(self):
        async with self.connection_lock:
            return await super()._context()

    async def _new_page(self):
        context = await self._context()
        try:
            return await context.new_page()
        except Exception as exc:
            await asyncio.sleep(0)  # Let the close event invalidate a managed context.
            if 'has been closed' in str(exc) and getattr(self, 'context', context) is not context:
                try:
                    return await (await self._context()).new_page()
                except BridgeError:
                    raise
                except Exception:
                    pass
            raise BridgeError(Code.NETWORK, 'Cannot open an article tab; retry after reopening the dedicated browser', retryable=True) from exc

    async def _manual_pdf(self, page, target, capture):
        try:
            await page.bring_to_front()
            deadline = time.monotonic() + self.human_wait_seconds
            while not capture.future.done():
                if page.is_closed() or time.monotonic() >= deadline:
                    raise BridgeError(Code.HUMAN_REQUIRED, 'Manual download deadline reached or tab closed; retry with human-run',
                                      action='Complete verification and click PDF download in the retained article tab')
                target.policy.navigation_url(page.url)
                await asyncio.sleep(.25)
            return capture.future.result()
        except Exception as exc:
            if page.is_closed():
                raise BridgeError(Code.HUMAN_REQUIRED, 'Manual article tab/browser was closed; item remains pending') from exc
            raise

    async def _guard(self, page, target):
        async def guard(route):
            try:
                # Context routing also covers a popup's very first request. Only
                # descendants of this workflow page belong to this guard.
                try:
                    frame = route.request.frame
                except Exception:
                    frame = None  # Popup first navigation can precede creation of its frame.
                if frame is not None:
                    owner = frame.page
                    root = owner
                    while root is not None and root != page:
                        root = await root.opener()
                    if root is None:
                        await route.fallback()
                        return
                # Frameless requests cannot be attributed yet: conservatively apply
                # this provider allowlist, rather than letting a popup bypass it.
                is_main = route.request.is_navigation_request() and (frame is None or frame == owner.main_frame)
                policy = target.policy if is_main else target.resources or target.policy
                checked = (policy.navigation_url(route.request.url) if is_main and route.request.method == 'GET'
                           else policy.check(route.request.url))
                if checked != route.request.url:
                    await route.fulfill(status=200, content_type='text/html', body=
                        '<meta http-equiv="refresh" content="0;url=' + escape(checked, quote=True) + '">')
                    return
            except (BridgeError, ValueError):
                await route.abort()
            else:
                await route.fallback()
        context = page.context
        await context.route('**/*', guard)
        return guard

    async def _navigate(self, page, url, capture):
        try:
            return await page.goto(url, wait_until='domcontentloaded', timeout=18000,
                                   referer=page.url if page.url.startswith('https://') else None)
        except Exception as exc:
            if page.is_closed():
                raise BridgeError(Code.HUMAN_REQUIRED, 'Article tab/browser was closed; retry with human-run') from exc
            if capture.downloads or capture.future.done() or 'Download is starting' in str(exc):
                return None
            if 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc):
                raise BridgeError(Code.ACCESS_DENIED, 'Local browser policy blocked navigation') from exc
            if 'ERR_ABORTED' in str(exc):
                return None  # Download event may be queued immediately after an aborted document navigation.
            raise BridgeError(Code.NETWORK, 'Browser navigation failed', retryable=True) from exc

    def _same_active(self, url):
        return (self.active is not None and self.session_context.get() is self.active
                and self.active_target and public_url(url) == public_url(self.active_target.url))

    async def snapshot(self, url, policy, *, ready, challenge, resources=None):
        if self._same_active(url):
            policy.check(self.active.url)
            if await visible_challenge(self.active, challenge):
                raise BridgeError(Code.HUMAN_REQUIRED, 'Manual verification is still required')
            if not await self.active.locator(ready).count():
                raise BridgeError(Code.UPSTREAM_CHANGED, 'Expected article elements were not found after verification')
            html = await self.active.content()
            if len(html.encode('utf-8')) > 4 * 1024 * 1024:
                raise BridgeError(Code.TOO_LARGE, 'Browser document is too large')
            return html, self.active.url
        return await super().snapshot(url, policy, ready=ready, challenge=challenge, resources=resources)

    async def article_pdf(self, target):
        target.policy.check(target.url)
        async with self.lock:
            await self._pace()
            reuse = self._same_active(target.url)
            page = self.active if reuse else await self._new_page()
            capture = self.active_capture if reuse else Capture(page, target.policy, pdf_identity=target.pdf_identity)
            guard = None
            try:
                if not reuse:
                    await capture.start_stream(target)
                    guard = await self._guard(page, target)
                    response = await self._navigate(page, target.url, capture)
                else:
                    response = None
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not capture.future.done():
                    if await visible_challenge(page, target.challenge):
                        raise BridgeError(Code.HUMAN_REQUIRED, 'Article requires manual browser verification',
                                          action='Run human-run for this job; complete verification manually')
                    nodes = await page.locator(target.download_selector).all()
                    if nodes:
                        break
                    if response is not None and response.status in (401,403):
                        raise BridgeError(Code.ACCESS_DENIED, 'Article browser returned an access denial')
                    await asyncio.sleep(0.25)
                if capture.future.done():
                    return capture.future.result()
                target.policy.check(page.url)
                nodes = await page.locator(target.download_selector).all()
                for node in nodes[:20]:
                    href = await node.get_attribute('href') or await node.get_attribute('content')
                    if not href or href.startswith(('javascript:', '#')):
                        continue
                    url = urljoin(page.url, href)
                    target.policy.check(url)
                    await self._navigate(page, url, capture)
                    try:
                        async with asyncio.timeout(45):
                            while not capture.future.done():
                                if await visible_challenge(page, target.challenge):
                                    raise BridgeError(Code.HUMAN_REQUIRED, 'PDF request requires manual browser verification')
                                await asyncio.sleep(0.25)
                            return capture.future.result()
                    except TimeoutError as exc:
                        raise BridgeError(Code.HUMAN_REQUIRED, 'PDF was not delivered; use the visible article page manually') from exc
                raise BridgeError(Code.HUMAN_REQUIRED, 'No recognized PDF link; use the article download control manually',
                                  action='Run human-run; manual download is observed without solving CAPTCHA')
            except BridgeError as exc:
                if reuse or self.verification_mode != 'stable' or exc.info.code not in MANUAL_ERRORS:
                    raise
                # Keep the exact failed tab and its cookies/navigation history for the human.
                return await self._manual_pdf(page, target, capture)
            except Exception as exc:
                if page.is_closed():
                    raise BridgeError(Code.HUMAN_REQUIRED, 'Article tab/browser was closed; item remains pending') from exc
                raise
            finally:
                if not reuse:
                    await capture.close()
                    if guard:
                        with suppress(Exception):
                            await page.context.unroute('**/*', guard)
                    if not page.is_closed():
                        with suppress(Exception):
                            await page.close()

    async def native_pdf(self, url, policy):
        return await self.article_pdf(BrowserTarget(url, policy, 'body', '#captcha, #challenge-form, input[type="password"]',
                                                    'a[href*=".pdf"], meta[name="citation_pdf_url"]'))

    @asynccontextmanager
    async def human_session(self, target, wait_seconds):
        target.policy.check(target.url)
        async with self.human_lock:
            page = await self._new_page()
            context = page.context
            capture = Capture(page, target.policy, pdf_identity=target.pdf_identity)
            guard = None
            try:
                await capture.start_stream(target)
                guard = await self._guard(page, target)
                await self._navigate(page, target.url, capture)
                await page.bring_to_front()
                deadline = time.monotonic() + wait_seconds
                while True:
                    if capture.future.done():
                        session = HumanSession(page, capture.future.result())
                        break
                    if page.is_closed():
                        raise BridgeError(Code.HUMAN_REQUIRED, 'Manual article tab was closed; item remains pending')
                    target.policy.navigation_url(page.url)
                    challenged = await visible_challenge(page, target.challenge)
                    # Readiness requires a real link, not just metadata visible behind a challenge.
                    if self.verification_mode != 'stable' and not challenged and await usable_download_link(page, target):
                        session = HumanSession(page)
                        break
                    if time.monotonic() >= deadline:
                        raise BridgeError(Code.HUMAN_REQUIRED, 'Manual verification/download deadline reached; item remains pending')
                    await asyncio.sleep(0.5)
                self.active, self.active_target, self.active_owner = page, target, asyncio.current_task()
                self.active_capture = capture
                context_token = self.session_context.set(page)
                try:
                    yield session
                finally:
                    self.session_context.reset(context_token)
            except Exception as exc:
                if page.is_closed():
                    raise BridgeError(Code.HUMAN_REQUIRED, 'Manual article tab/browser was closed; item remains pending') from exc
                raise
            finally:
                self.active = self.active_target = self.active_owner = self.active_capture = None
                await capture.close()
                if guard:
                    with suppress(Exception):
                        await context.unroute('**/*', guard)
                if not page.is_closed():
                    with suppress(Exception):
                        await page.close()


class ManagedBrowser(WorkflowBrowser):
    """Lazily launched, dedicated persistent profile; never the user's default browser profile."""
    def __init__(self, profile: Path, channel='chrome', *, headless=False, verification_mode='selectors', human_wait_seconds=300):
        self.profile, self.channel, self.headless = profile, channel, headless
        self.endpoint, self.context_index = None, 0
        self.playwright = self.browser = self.context = None
        self.lock, self.launch_lock = asyncio.Lock(), asyncio.Lock()
        self.next_request = 0.0
        self.verification_mode, self.human_wait_seconds = verification_mode, human_wait_seconds
        self._setup()

    async def _context(self):
        async with self.launch_lock:
            if self.context is not None:
                return self.context
            if self.profile.is_symlink():
                raise BridgeError(Code.STORAGE, 'Managed browser profile must not be a symlink')
            self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                from playwright.async_api import async_playwright
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                options = {'headless':self.headless, 'accept_downloads':True, 'no_viewport':True}
                if self.channel != 'chromium':
                    options['channel'] = self.channel
                self.context = await self.playwright.chromium.launch_persistent_context(str(self.profile), **options)
                launched = self.context
                def closed():
                    if self.context is launched:
                        self.context = None
                launched.on('close', closed)
                return self.context
            except Exception as exc:
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
                raise BridgeError(Code.NOT_CONFIGURED, 'Cannot launch dedicated visible browser',
                    action='Install selected Chrome/Edge or Playwright Chromium; close another LitBridge using this profile') from exc

    async def health(self):
        await self._context()
        return {'state':'connected','check':'managed_visible_browser' if not self.headless else 'test_headless_browser',
                'live_checked':True,'publisher_session_verified':False}

    async def close(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
