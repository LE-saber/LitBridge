"""Bounded, allowlisted HTTP. Authentication never crosses an origin redirect."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import httpx
from litbridge.errors import BridgeError, Code

MAX_METADATA = 4 * 1024 * 1024
MAX_DOWNLOAD = 32 * 1024 * 1024


_IDENTITY_QUERY_ROUTES = {}

def register_identity_route(host: str, path: str, keys: set[str]) -> None:
    """Trusted opt-in providers may declare exact HTTPS identity-only query routes."""
    if not re.fullmatch(r'[a-z0-9.-]+',host) or not path.startswith('/') or any(
            not re.fullmatch(r'[a-z][a-z0-9_]{0,40}',key) for key in keys):
        raise BridgeError(Code.INVALID_INPUT, 'Invalid identity query route')
    _IDENTITY_QUERY_ROUTES[('https',host,path)] = frozenset(keys)


def public_url(url: str) -> str:
    """Persist only stable identifying query parameters, not session/signature material."""
    try:
        p = urlsplit(url)
        allowed = {'dbname', 'filename', 'dbcode', 'doi', 'article_number'}
        allowed.update(_IDENTITY_QUERY_ROUTES.get((p.scheme, p.hostname, p.path), ()))
        query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() in allowed])
        return urlunsplit((p.scheme, p.hostname or '', p.path, query, ''))
    except ValueError:
        return ''


class URLPolicy:
    def __init__(self, hosts: set[str], suffixes: tuple[str, ...] = (), *, navigation_upgrades=()):
        self.hosts, self.suffixes = hosts, suffixes
        self.navigation_upgrades = frozenset(navigation_upgrades)

    def navigation_url(self, url: str) -> str:
        """Upgrade only explicitly approved legacy document routes; never fetch HTTP."""
        try:
            p = urlsplit(url)
            if (p.scheme == 'http' and p.port in (None, 80) and not p.username and not p.password
                    and (p.hostname, p.path) in self.navigation_upgrades):
                upgraded = urlunsplit(('https', p.hostname, p.path, p.query, p.fragment))
                # Validate original text too, before normalization can discard unsafe characters.
                if re.search(r'[\x00-\x20\\]', url):
                    raise ValueError()
                return self.check(upgraded)
        except ValueError as exc:
            raise BridgeError(Code.UNSAFE_URL, 'Invalid browser navigation URL') from exc
        return self.check(url)

    def check(self, url: str) -> str:
        try:
            if re.search(r'[\x00-\x20\\]', url):
                raise ValueError()
            p = urlsplit(url)
            host = (p.hostname or '').lower()
            if p.scheme != 'https' or p.port not in (None, 443) or p.username or p.password:
                raise ValueError()
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError()
            if not (host in self.hosts or any(host.endswith('.' + s) for s in self.suffixes)):
                raise ValueError()
        except ValueError as exc:
            raise BridgeError(Code.UNSAFE_URL, 'URL is outside this provider HTTPS allowlist') from exc
        return url


def retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


class HTTP:
    def __init__(self, policy: URLPolicy, *, headers: dict | None = None,
                 params: dict | None = None, transport=None, interval: float = 0.6,
                 timeout: float = 20, retries: int = 1, error_classifier=None):
        self.policy = policy
        self.auth_headers, self.auth_params = headers or {}, params or {}
        self.client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False,
                                       headers={'User-Agent': 'LitBridge/0.1 (local research gateway)'},
                                       limits=httpx.Limits(max_connections=4))
        self.interval, self.retries = interval, retries
        self.error_classifier = error_classifier
        self.lock, self.next_request = asyncio.Lock(), 0.0

    async def close(self):
        await self.client.aclose()

    async def _pace(self):
        async with self.lock:
            await asyncio.sleep(max(0.0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval

    async def request(self, method: str, url: str, *, params=None, json_body=None,
                      accept='application/json', max_bytes=MAX_METADATA) -> bytes:
        self.policy.check(url)
        origin = urlsplit(url).netloc
        current = url
        query = dict(params or {}) | self.auth_params
        for redirect in range(5):
            self.policy.check(current)
            if urlsplit(current).netloc != origin:
                # Conservative: no cross-origin redirect, even without credentials.
                raise BridgeError(Code.UNSAFE_URL, 'Cross-origin redirect refused')
            for attempt in range(self.retries + 1):
                await self._pace()
                try:
                    target = str(httpx.URL(current).copy_merge_params(query))
                    async with self.client.stream(method, target,
                            json=json_body, headers=self.auth_headers | {'Accept': accept}) as response:
                        status = response.status_code
                        if status in (301, 302, 303, 307, 308):
                            location = response.headers.get('location')
                            if not location:
                                raise BridgeError(Code.UPSTREAM_CHANGED, 'Redirect is missing a location')
                            current = urljoin(str(response.url), location)
                            query = self.auth_params.copy()
                            if status == 303:
                                method, json_body = 'GET', None
                            break
                        if status == 429 or status in (502, 503, 504):
                            wait = max(retry_after(response.headers.get('retry-after')), 0.5 * 2**attempt)
                            if attempt < self.retries and wait <= 3:
                                await asyncio.sleep(wait)
                                continue
                            code = Code.RATE_LIMITED if status == 429 else Code.UPSTREAM
                            raise BridgeError(code, 'Upstream rate limit or temporary outage', retryable=True,
                                              action='Retry later; respect the provider quota')
                        codes = {400: Code.INVALID_INPUT, 401: Code.AUTH_REQUIRED,
                                 403: Code.ACCESS_DENIED, 404: Code.NOT_FOUND}
                        if status >= 400:
                            if self.error_classifier is not None:
                                # Classifiers see a bounded body in memory; raw provider text never crosses the boundary.
                                sample = bytearray()
                                async for chunk in response.aiter_bytes():
                                    sample.extend(chunk[:4096-len(sample)])
                                    if len(sample) >= 4096:
                                        break
                                classified = self.error_classifier(status, bytes(sample))
                                if classified is not None:
                                    raise classified
                            raise BridgeError(codes.get(status, Code.UPSTREAM), f'Upstream returned HTTP {status}',
                                              retryable=status >= 500)
                        if not 200 <= status < 300:
                            raise BridgeError(Code.UPSTREAM, 'Unexpected upstream HTTP response')
                        length = response.headers.get('content-length', '')
                        if length.isdigit() and int(length) > max_bytes:
                            raise BridgeError(Code.TOO_LARGE, 'Response exceeds the configured size bound')
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise BridgeError(Code.TOO_LARGE, 'Response exceeds the configured size bound')
                            chunks.append(chunk)
                        return b''.join(chunks)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                    code = Code.TIMEOUT if isinstance(exc, httpx.TimeoutException) else Code.NETWORK
                    raise BridgeError(code, 'Upstream connection failed', retryable=True) from exc
            else:
                raise BridgeError(Code.UPSTREAM, 'Request could not be completed')
        raise BridgeError(Code.UPSTREAM, 'Too many redirects')

    async def json(self, method: str, url: str, **kwargs) -> dict:
        raw = await self.request(method, url, **kwargs)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError) as exc:
            raise BridgeError(Code.UPSTREAM_CHANGED, 'Expected a JSON object from this API') from exc
