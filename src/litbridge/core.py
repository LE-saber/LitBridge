"""One service layer for CLI/MCP: isolation, identity, cache and explicit retrieval."""
from __future__ import annotations
import asyncio
import json
import time
from litbridge.errors import BridgeError, Code, safe_error
from litbridge.models import Candidate, Paper, Query, Reference, SearchPage, digest, merge_papers, normalize_doi, same_work
from litbridge.storage import Store
from litbridge.transport import MAX_DOWNLOAD, public_url


class Gateway:
    def __init__(self, providers, store: Store, *, timeout=45.0, cache_ttl=21600, browser=None, defaults=None,
                 structured_python=None, structured_models=None, cloud_formula_ocr=False,model_services=None):
        from litbridge.documents import Documents
        from litbridge.ledger import Ledger
        self.providers = {p.info.id: p for p in providers}
        self.store, self.timeout, self.cache_ttl = store, timeout, cache_ttl
        self.browser, self.defaults = browser, defaults
        self.documents = Documents(store, structured_python=structured_python, structured_models=structured_models,
            cloud_formula_ocr=cloud_formula_ocr,model_services=model_services)
        self.ledger = Ledger(self)
        self.breakers = {name: [0, 0.0] for name in self.providers}
        self.locks = {name: asyncio.Semaphore(2) for name in self.providers}

    def _select(self, capability, names=None):
        if names is not None:
            if not names or len(names) != len(set(names)) or len(names) > 16:
                raise BridgeError(Code.INVALID_INPUT, 'Specify 1..16 unique provider IDs')
            if any(name not in self.providers for name in names):
                raise BridgeError(Code.INVALID_INPUT, 'Unknown provider ID; use providers to list available IDs')
            return names
        candidates = self.defaults if self.defaults is not None else list(self.providers)
        return [name for name in candidates if name in self.providers
                and capability in self.providers[name].info.capabilities
                and self.providers[name].info.state in ('ready', 'experimental')]

    async def _call(self, name, method, argument=None, *, use_cache=True):
        p = self.providers[name]
        if method != 'health':
            p.require(method)
        breaker = self.breakers[name]
        if breaker[1] > time.monotonic():
            raise BridgeError(Code.CIRCUIT_OPEN, 'Provider temporarily isolated after repeated failures', retryable=True)
        key = None
        if method in ('search', 'resolve') and p.cache_scope != 'browser-uncached':
            payload = argument.model_dump() if hasattr(argument, 'model_dump') else argument
            key = digest(json.dumps([name, p.info.protocol, p.info.version, p.cache_scope, method, payload], sort_keys=True))
            hit = self.store.cache_get(key) if use_cache else None
            if hit is not None:
                return SearchPage.model_validate(hit) if method == 'search' else Paper.model_validate(hit)
        try:
            timeout = self.timeout
            bridge = getattr(p, 'bridge', None)
            if bridge is not None and getattr(bridge, 'verification_mode', 'selectors') == 'stable':
                timeout += bridge.human_wait_seconds
            async with asyncio.timeout(timeout):
                async with self.locks[name]:
                    result = await getattr(p, method)(argument)
            # Validate plugin output inside the isolation boundary, not during federation.
            try:
                if method == 'search':
                    result = SearchPage.model_validate(result)
                    if result.provider != name or len(result.papers) > argument.limit:
                        raise ValueError()
                elif method == 'resolve' and result is not None:
                    result = Paper.model_validate(result)
                    expected_doi = normalize_doi(argument)
                    if expected_doi and result.doi != expected_doi:
                        raise ValueError()
                elif method == 'access':
                    if not isinstance(result, list) or len(result) > 20:
                        raise ValueError()
                    result = [Candidate.model_validate(c) for c in result]
                    if any(c.provider != name for c in result):
                        raise ValueError()
                elif method == 'references':
                    if not isinstance(result, list):
                        raise ValueError()
                    result = [Reference.model_validate(r).model_dump() for r in result[:200]]
                elif method == 'retrieve':
                    if not isinstance(result, bytes):
                        raise ValueError()
                    if len(result) > MAX_DOWNLOAD:
                        raise BridgeError(Code.TOO_LARGE, 'Provider returned more than 32 MiB')
            except (ValueError, TypeError, AttributeError) as exc:
                raise BridgeError(Code.UPSTREAM_CHANGED, 'Provider output violates protocol 1.0') from exc
            if result is not None and key:
                self.store.cache_put(key, result.model_dump(), self.cache_ttl)
            breaker[:] = [0, 0.0]
            return result
        except Exception as exc:
            info = safe_error(exc, name)
            if info.retryable or info.code in (Code.INTERNAL, Code.UPSTREAM_CHANGED):
                breaker[0] += 1
                if breaker[0] >= 3:
                    breaker[1] = time.monotonic() + 30
            raise

    async def _fanout(self, names, method, arguments, *, use_cache=True):
        async def one(name):
            try:
                return name, await self._call(name, method, arguments[name], use_cache=use_cache), None
            except Exception as exc:
                return name, None, safe_error(exc, name).model_dump()
        return await asyncio.gather(*(one(name) for name in names))

    def _merge(self, papers):
        groups = []
        for p in papers:
            for i, old in enumerate(groups):
                if same_work(old, p):
                    groups[i] = merge_papers(old, p)
                    break
            else:
                groups.append(p)
        return [self.store.put(p) for p in groups]

    async def search(self, text: str, *, providers=None, limit=10, mode='simple', cursors=None):
        names = self._select('search', providers)
        if mode == 'native' and (providers is None or len(names) != 1):
            raise BridgeError(Code.INVALID_INPUT, 'Native query syntax requires exactly one explicit provider')
        cursors = cursors or {}
        if set(cursors) - set(names):
            raise BridgeError(Code.INVALID_INPUT, 'Cursor keys must be among selected provider IDs')
        queries = {n: Query(text=text, limit=limit, mode=mode, cursor=cursors.get(n)) for n in names}
        Query(text=text, limit=limit, mode=mode)  # Validate even when no providers are ready.
        results = await self._fanout(names, 'search', queries)
        errors, pages, all_papers, ranked = [], [], [], []
        for name, page, error in results:
            if error:
                errors.append(error)
                continue
            pages.append(page)
            all_papers.extend(page.papers)
            ranked.extend((p, 1.0 / (60 + rank)) for rank, p in enumerate(page.papers, 1))
        papers = self._merge(all_papers)
        scores = {p.id: sum(score for q, score in ranked if same_work(p, q)) for p in papers}
        papers.sort(key=lambda p: (-scores[p.id], p.id))
        return {'status': 'ok' if pages else 'error', 'partial': bool(errors),
                'papers': [p.model_dump() for p in papers],
                'providers': [{'id': page.provider, 'returned': len(page.papers), 'total': page.total,
                               'query_sent': page.query_sent, 'semantics': page.semantics,
                               'warnings': page.warnings} for page in pages],
                'next_cursors': {page.provider: page.next_cursor for page in pages if page.next_cursor},
                'errors': errors, 'limit_semantics': 'Per-provider; all fetched records returned after merge',
                'ranking': 'Reciprocal rank fusion; scores are not comparable relevance probabilities',
                'untrusted_content': True}

    def _stored(self, identifier):
        doi = normalize_doi(identifier)
        return self.store.get(identifier) or (self.store.get('doi:' + doi) if doi else None)

    async def resolve(self, identifier: str, *, providers=None, refresh=False):
        if not identifier or len(identifier) > 2048:
            raise BridgeError(Code.INVALID_INPUT, 'Identifier must have 1..2048 characters')
        old = self._stored(identifier)
        # Explicit provider selection requests enrichment, even if a local record exists.
        if old and not refresh and providers is None:
            return {'status': 'ok', 'paper': old.model_dump(), 'errors': [], 'partial': False, 'from_cache': True}
        names = self._select('resolve', providers)
        query = (old.doi if old else None) or identifier
        results = await self._fanout(names, 'resolve', {n: query for n in names}, use_cache=not refresh)
        found = [p for _, p, _ in results if p]
        if old:
            found.insert(0, old)
        papers = self._merge(found)
        errors = [e for _, _, e in results if e]
        if not papers:
            raise BridgeError(Code.NOT_FOUND if not errors else Code.UPSTREAM,
                              'No resolvable record returned by selected providers',
                              action='Check identifier and provider health; no title-match identity was assumed')
        if len(papers) > 1:
            raise BridgeError(Code.UPSTREAM_CHANGED, 'Providers returned incompatible identities for this identifier')
        return {'status': 'ok', 'paper': papers[0].model_dump(), 'errors': errors,
                'partial': bool(errors), 'from_cache': False, 'untrusted_content': True}

    async def _load(self, identifier):
        result = self._stored(identifier)
        if result is None:
            result = Paper.model_validate((await self.resolve(identifier))['paper'])
        return result

    async def _discover(self, identifier, names=None):
        p = await self._load(identifier)
        selected = self._select('access', names)
        results = await self._fanout(selected, 'access', {n: p for n in selected})
        candidates = [c for _, batch, _ in results if batch for c in batch]
        errors = [e for _, _, e in results if e]
        candidates.sort(key=lambda c: (c.access != 'open_access', c.format != 'xml', c.provider, c.url))
        return p, candidates, errors

    async def access(self, identifier, *, providers=None):
        p, candidates, errors = await self._discover(identifier, providers)
        return {'status': 'ok', 'paper_id': p.id,
                'candidates': [c.model_copy(update={'url': public_url(c.url)}).model_dump() for c in candidates],
                'errors': errors, 'partial': bool(errors),
                'note': 'Candidates are not entitlement proofs. Retrieve performs the actual request; tokens are hidden.'}

    async def retrieve(self, identifier, *, provider=None, format=None):
        if format not in (None, 'xml', 'pdf'):
            raise BridgeError(Code.INVALID_INPUT, 'Format must be xml or pdf')
        p, candidates, errors = await self._discover(identifier, [provider] if provider else None)
        candidate_entries = [{'provider': c.provider, 'url': public_url(c.url)} for c in candidates]
        for candidate in candidates:
            if format and candidate.format != format:
                continue
            try:
                data = await self._call(candidate.provider, 'retrieve', candidate)
                bridge = getattr(self.providers[candidate.provider], 'bridge', None)
                manual = candidate.format == 'pdf' and getattr(bridge, 'manual_import', False)
                origin = getattr(bridge, 'source_url', 'local:manual-download') if manual else public_url(candidate.url)
                artifact = self.store.save(p.id, candidate.provider, candidate.format, origin, data)
                return {'status': 'ok', 'artifact': artifact.model_dump(), 'attempt_errors': errors,
                        'normalization': await self.documents.outcome(artifact.id),
                        'candidate_entries': candidate_entries,
                        'verified': True, 'verification': 'Successful retrieval, format validation and SHA-256',
                        'identity_evidence': getattr(bridge, 'identity_evidence', 'Human-supplied PDF; document identity not independently verified') if manual else None,
                        'untrusted_content': True}
            except Exception as exc:
                error = safe_error(exc, candidate.provider)
                errors.append(error.model_dump())
                if error.code == Code.STORAGE:
                    break  # A disk problem is not a reason to download again from another publisher.
        return {'status': 'error', 'code': 'full_text_unavailable', 'paper_id': p.id,
                'errors': errors, 'candidate_entries': candidate_entries, 'note': 'No accessible validated full text; no access-control bypass attempted'}

    async def normalize(self, artifact_id, *, force=False, engine='basic', page_limit=3, formulas=False,
                        cloud_limit=3, retry_cloud=False,model_profile=None,compare_profiles=None):
        return {'status': 'ok', 'normalization': await self.documents.normalize(artifact_id, force=force,
            engine=engine, page_limit=page_limit, formulas=formulas,cloud_limit=cloud_limit,retry_cloud=retry_cloud,
            model_profile=model_profile,compare_profiles=compare_profiles)}

    async def read(self, artifact_id, offset=0, max_chars=8000):
        return await self.documents.read(artifact_id, offset, max_chars)

    async def batch(self, identifiers, *, provider=None, format=None):
        job = self.ledger.create(identifiers, provider, format)
        return await self.ledger.run(job['job_id'])

    async def job_create(self, identifiers, *, provider=None, format=None):
        return self.ledger.create(identifiers, provider, format)

    async def job_run(self, job_id, *, retry_failed=False, limit=100):
        return await self.ledger.run(job_id, retry_failed=retry_failed, limit=limit)

    async def job_status(self, job_id=None, offset=0, limit=50):
        return self.ledger.status(job_id, offset, limit)

    async def job_history(self, item_id, offset=0, limit=20):
        return self.ledger.history(item_id, offset, limit)

    async def human_run(self, job_id, *, wait_seconds=300, limit=10):
        return await self.ledger.human_run(job_id, wait_seconds=wait_seconds, limit=limit)

    async def ingest(self, path, *, identifier):
        # CLI-only explicit local import. Not a remotely exposed arbitrary-file reader.
        from pathlib import Path
        source = Path(path).expanduser()
        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_DOWNLOAD:
            raise BridgeError(Code.INVALID_INPUT, 'Choose a regular local PDF/XML/HTML file up to 32 MiB')
        fmt = source.suffix.lower().lstrip('.')
        if fmt == 'htm':
            fmt = 'html'
        if fmt not in ('pdf', 'xml', 'html'):
            raise BridgeError(Code.INVALID_INPUT, 'Only local PDF/XML/HTML originals can be ingested')
        p = await self._load(identifier)
        with source.open('rb') as f:
            data = f.read(MAX_DOWNLOAD + 1)
        artifact = self.store.save(p.id, 'local', fmt, 'local:explicit-import', data)
        return {'status': 'ok', 'artifact': artifact.model_dump(),
                'normalization': await self.documents.outcome(artifact.id),
                'identity_evidence': 'User explicitly associated this local file with the paper; not independently verified'}

    async def references(self, identifier, *, providers=None):
        p = await self._load(identifier)
        names = self._select('references', providers)
        results = await self._fanout(names, 'references', {n: p for n in names})
        items, errors = [], []
        for name, refs, error in results:
            if error:
                errors.append(error)
            else:
                items.extend({'provider': name, **ref} for ref in refs[:200])
        return {'status': 'ok', 'paper_id': p.id, 'references': items, 'errors': errors,
                'partial': bool(errors), 'limit_per_provider': 200,
                'note': 'Outgoing references, not citing articles; lists may be truncated or unavailable'}

    async def import_url(self, url, *, provider):
        self._select('import_url', [provider])
        p = await self._call(provider, 'import_url', url)
        return {'status': 'ok', 'paper': self.store.put(p).model_dump(), 'untrusted_content': True}

    def providers_info(self):
        return {'status': 'ok', 'providers': [p.info.model_dump() for p in self.providers.values()],
                'note': 'ready means configuration-ready, not campus-tested or entitled'}

    def models_info(self):
        from litbridge.model_services import info
        return {'status':'ok','cloud_formula_ocr_enabled':self.documents.cloud_formula_ocr,
                **info(self.documents.model_services)}

    async def doctor(self, *, live=False):
        results = await self._fanout(list(self.providers), 'health', {n: live for n in self.providers})
        return {'status': 'ok', 'checks': [{'provider': n, 'result': data, 'error': error}
                                          for n, data, error in results],
                'live_requested': live, 'data_directory': str(self.store.home), 'models':self.models_info()}

    async def close(self):
        await asyncio.gather(*(p.close() for p in self.providers.values()), return_exceptions=True)
        try:
            if self.browser:
                await self.browser.close()
        finally:
            self.store.close()
