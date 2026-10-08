"""Bounded streaming capture using explicit selected-document predicates."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import os
from contextlib import suppress
from pathlib import Path
import re
import time
import tempfile
from urllib.parse import urlsplit

from litbridge.errors import BridgeError, Code
from litbridge.storage import validate_content
from litbridge.transport import MAX_DOWNLOAD


class ReaderCapture:
    def __init__(self, identifier: str, inbox: Path, *, url_matcher=None, response_patterns=None):
        if not re.fullmatch(r'[A-Za-z0-9]{10,32}', identifier):
            raise BridgeError(Code.INVALID_INPUT, 'Invalid PDF article identifier')
        self.identifier, self.inbox = identifier, inbox
        self.url_matcher, self.response_patterns = url_matcher, response_patterns
        self.future = asyncio.get_running_loop().create_future()
        self.sessions, self.tasks, self.events, self.pages = [], set(), [], set()
        self.started = time.monotonic()
        self.closing = False

    def event(self, kind, **data):
        if len(self.events) < 100:
            self.events.append({'elapsed_seconds':round(time.monotonic()-self.started, 2), 'kind':kind, **data})

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def matches(self, url):
        return bool(self.url_matcher and self.url_matcher(url))

    async def attach(self, context, page):
        if page in self.pages or self.closing:
            return
        self.pages.add(page)
        session = None
        try:
            async with asyncio.timeout(5):
                session = await context.new_cdp_session(page)
                self.sessions.append(session)
                session.on('Fetch.requestPaused', lambda e: self.spawn(self.paused(session, e)))
                if not self.url_matcher or not self.response_patterns:
                    raise BridgeError(Code.INVALID_INPUT, 'Selected response predicate and patterns are required')
                patterns = [{'urlPattern':pattern, 'requestStage':'Response'} for pattern in self.response_patterns]
                await session.send('Fetch.enable', {'patterns':patterns})
        except Exception:
            self.event('reader_listener_unavailable')
            if session:
                with suppress(Exception):
                    await asyncio.wait_for(session.detach(), 2)

    async def paused(self, session, event):
        rid, stream, taken, resumed = event['requestId'], None, False, False
        headers = {h['name'].lower():h['value'] for h in event.get('responseHeaders', [])}
        try:
            if self.matches(event['request']['url']):
                self.event('selected_response', status=event.get('responseStatusCode'),
                           is_pdf='application/pdf' in headers.get('content-type', '').lower(),
                           is_range='content-range' in headers)
            # Reject partial PDF ranges and other articles; their normal browser flow continues.
            if (self.closing or not self.matches(event['request']['url']) or
                    event.get('responseStatusCode') != 200 or
                    'application/pdf' not in headers.get('content-type', '').lower() or
                    'content-range' in headers):
                await session.send('Fetch.continueRequest', {'requestId':rid})
                resumed = True
                return
            length = headers.get('content-length', '')
            self.event('selected_pdf_response', status=200, has_content_length=length.isdigit())
            if length.isdigit() and int(length) > MAX_DOWNLOAD:
                raise BridgeError(Code.TOO_LARGE, 'Reader PDF exceeds 32 MiB')
            stream = (await session.send('Fetch.takeResponseBodyAsStream', {'requestId':rid}))['stream']
            taken = True
            raw = bytearray()
            async with asyncio.timeout(60):
                while True:
                    part = await session.send('IO.read', {'handle':stream, 'size':65536})
                    raw.extend(base64.b64decode(part['data'], validate=True) if part.get('base64Encoded')
                               else part['data'].encode('utf-8'))
                    if len(raw) > MAX_DOWNLOAD:
                        raise BridgeError(Code.TOO_LARGE, 'Reader PDF exceeds 32 MiB')
                    if part['eof']:
                        break
            data = bytes(raw)
            validate_content(data, 'pdf')
            # Keep signed URL, cookies and headers exclusively in memory. Persist only PDF bytes.
            if self.inbox.is_symlink() or getattr(self.inbox, 'is_junction', lambda:False)():
                raise BridgeError(Code.STORAGE, 'Reader inbox must not be a link')
            path = self.inbox / (self.identifier + '-reader-' + hashlib.sha256(data).hexdigest()[:16] + '.pdf')
            if path.is_symlink():
                raise BridgeError(Code.STORAGE, 'Reader PDF target must not be a link')
            if self.future.done():
                # A duplicate user click must not overwrite an already delivered original.
                self.event('duplicate_pdf_response')
            else:
                if path.exists():
                    if not path.is_file() or path.stat().st_size != len(data) or path.read_bytes() != data:
                        raise BridgeError(Code.STORAGE, 'Existing reader receipt differs from captured PDF')
                else:
                    stage = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=self.inbox, suffix='.part', delete=False) as file:
                            stage = Path(file.name)
                            file.write(data)
                            file.flush()
                            os.fsync(file.fileno())
                        os.replace(stage, path)
                    except OSError as exc:
                        raise BridgeError(Code.STORAGE, 'Cannot save the reader PDF receipt') from exc
                    finally:
                        if stage:
                            stage.unlink(missing_ok=True)
                self.future.set_result(data)
                self.event('pdf_captured', bytes=len(data))
            response_headers = [h for h in event['responseHeaders'] if h['name'].lower() not in
                                ('content-length', 'content-encoding', 'transfer-encoding')]
            response_headers.append({'name':'Content-Length', 'value':str(len(data))})
            await session.send('Fetch.fulfillRequest', {'requestId':rid, 'responseCode':200,
                'responseHeaders':response_headers, 'body':base64.b64encode(data).decode('ascii')})
            resumed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Safe, classified evidence; no raw protocol exceptions containing signed URLs.
            self.event('capture_error', code=exc.info.code.value if isinstance(exc, BridgeError) else 'reader_stream_failed')
            if isinstance(exc, BridgeError) and exc.info.code in (Code.TOO_LARGE, Code.STORAGE, Code.INVALID_CONTENT):
                if not self.future.done():
                    self.future.set_exception(exc)
        finally:
            if not resumed:
                with suppress(Exception):
                    await asyncio.wait_for(session.send('Fetch.failRequest' if taken else 'Fetch.continueRequest',
                                       {'requestId':rid, **({'errorReason':'Aborted'} if taken else {})}), 2)
            if stream:
                with suppress(Exception):
                    await asyncio.wait_for(session.send('IO.close', {'handle':stream}), 2)

    async def close(self):
        self.closing = True
        # Finish short response restoration after bytes are received, then release paused requests.
        if self.tasks:
            grace = (15 if self.future.done() and not self.future.cancelled() and
                     self.future.exception() is None else 2)
            _, pending = await asyncio.wait(list(self.tasks), timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for session in self.sessions:
            with suppress(Exception):
                await asyncio.wait_for(session.send('Fetch.disable'), 2)
            with suppress(Exception):
                await asyncio.wait_for(session.detach(), 2)
        if self.future.done() and not self.future.cancelled():
            self.future.exception()
        else:
            self.future.cancel()
