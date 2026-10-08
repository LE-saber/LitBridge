"""Versioned Core document layer; original artifacts are never replaced by extracted text."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Literal
from pydantic import Field
from litbridge.errors import BridgeError, Code, safe_error
from litbridge.models import Artifact, Model

VERSION = '1.0-litbridge-0.1.1-normalize.3'
MAX_DOCUMENT = 16 * 1024 * 1024


class Locator(Model):
    artifact_id: str
    format: Literal['pdf', 'xml', 'html']
    page: int | None = None
    path: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    method: str | None = None
    confidence: float | None = None
    model: str | None = None
    model_profile: str | None = None
    crop_sha256: str | None = None


class Block(Model):
    id: str
    kind: Literal['heading', 'paragraph', 'table', 'figure', 'formula', 'list_item']
    text: str
    parent_id: str | None = None
    level: int | None = None
    rows: list[list[str]] = Field(default_factory=list)
    source: Locator
    start: int = 0
    end: int = 0


class Document(Model):
    id: str
    schema_version: str = '1.0'
    parser_version: str = VERSION
    artifact_id: str
    original_sha256: str
    original: Artifact | None = None
    title: str
    status: Literal['ready', 'partial', 'needs_ocr'] = 'ready'
    warnings: list[str] = Field(default_factory=list)
    blocks: list[Block]
    markdown: str
    untrusted_content: bool = True


def original(store, artifact_id: str) -> tuple[Artifact, Path]:
    row = store.db.execute('SELECT data FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if not row:
        raise BridgeError(Code.NOT_FOUND, 'Unknown original artifact ID')
    a = Artifact.model_validate_json(row[0])
    path = Path(a.path)
    if path.is_symlink() or store.downloads.is_symlink() or path.resolve().parent != store.downloads.resolve():
        raise BridgeError(Code.STORAGE, 'Original artifact escaped the downloads directory')
    try:
        if path.stat().st_size != a.size or a.size > 32 * 1024 * 1024:
            raise BridgeError(Code.INVALID_CONTENT, 'Original artifact size changed')
        if hashlib.sha256(path.read_bytes()).hexdigest() != a.sha256:
            raise BridgeError(Code.INVALID_CONTENT, 'Original artifact checksum mismatch')
    except OSError as exc:
        raise BridgeError(Code.STORAGE, 'Original artifact is missing or unreadable') from exc
    return a, path


class Documents:
    def __init__(self, store, *, structured_python=None, structured_models=None, cloud_formula_ocr=False,
                 model_services=None):
        self.store = store
        self.structured_python, self.structured_models = structured_python, structured_models
        self.cloud_formula_ocr = cloud_formula_ocr
        self.model_services = model_services
        self.root = store.home / 'documents'
        if self.root.is_symlink():
            raise BridgeError(Code.STORAGE, 'Documents directory must not be a symlink')
        self.root.mkdir(mode=0o700, exist_ok=True)
        with store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS documents_v1
                (id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, version TEXT NOT NULL,
                 json_sha TEXT NOT NULL, md_sha TEXT NOT NULL)''')
        self.locks: dict[str, asyncio.Lock] = {}

    def _path(self, name):
        if self.root.is_symlink() or self.root.resolve().parent != self.store.home:
            raise BridgeError(Code.STORAGE, 'Documents directory changed outside configured home')
        path = self.root / name
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise BridgeError(Code.STORAGE, 'Unsafe canonical document path')
        return path

    def _load(self, identifier: str, version=VERSION) -> Document | None:
        row = self.store.db.execute('''SELECT id,artifact_id,json_sha,md_sha FROM documents_v1
            WHERE id=? OR (artifact_id=? AND version=?) ORDER BY id LIMIT 1''',
            (identifier, identifier, version)).fetchone()
        if not row:
            return None
        original(self.store, row[1])
        try:
            data = []
            for suffix, expected in (('.json', row[2]), ('.md', row[3])):
                path = self._path(row[0] + suffix)
                if path.stat().st_size > MAX_DOCUMENT:
                    raise BridgeError(Code.TOO_LARGE, 'Canonical document exceeds its bound')
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise BridgeError(Code.INVALID_CONTENT, 'Canonical document checksum mismatch')
                data.append(raw)
            doc = Document.model_validate_json(data[0])
            if doc.markdown.encode('utf-8') != data[1]:
                raise BridgeError(Code.INVALID_CONTENT, 'Canonical Markdown and AST disagree')
            return doc
        except OSError as exc:
            raise BridgeError(Code.STORAGE, 'Canonical document files are missing or unreadable') from exc

    def summary(self, doc, reused=False):
        return {'status': doc.status, 'document_id': doc.id, 'artifact_id': doc.artifact_id,
                'schema_version': doc.schema_version, 'parser_version': doc.parser_version,
                'markdown_path': str(self._path(doc.id + '.md')), 'json_path': str(self._path(doc.id + '.json')),
                'block_count': len(doc.blocks), 'total_chars': len(doc.markdown),
                'warnings': doc.warnings, 'reused': reused, 'untrusted_content': True}

    def _save(self, doc, artifact, tmp):
        if doc.artifact_id != artifact.id or doc.original_sha256 != artifact.sha256:
            raise BridgeError(Code.INVALID_CONTENT, 'Parser returned mismatched source identity')
        doc.original = artifact
        paper = self.store.get(artifact.paper_id)
        if paper and doc.title in ('Full text', 'Saved article'):
            doc.title = paper.title
        raw, md = doc.model_dump_json().encode('utf-8'), doc.markdown.encode('utf-8')
        if len(raw) > MAX_DOCUMENT:
            raise BridgeError(Code.TOO_LARGE, 'Canonical document exceeds 16 MiB; original retained')
        for suffix, content in (('.json', raw), ('.md', md)):
            stage = Path(tmp) / ('document' + suffix)
            with stage.open('wb') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(stage, self._path(doc.id + suffix))
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO documents_v1 VALUES (?,?,?,?,?)',
                (doc.id, artifact.id, doc.parser_version, hashlib.sha256(raw).hexdigest(), hashlib.sha256(md).hexdigest()))
        return self.summary(doc)

    async def normalize(self, artifact_id: str, *, force=False, engine='basic', page_limit=3, formulas=False,
                        cloud_limit=3, retry_cloud=False, model_profile=None, compare_profiles=None):
        if engine not in ('basic', 'docling', 'mathpix', 'model', 'compare'):
            raise BridgeError(Code.INVALID_INPUT, 'Normalization engine must be basic, docling, mathpix, model or compare')
        if (model_profile is not None and engine != 'model') or (compare_profiles is not None and engine != 'compare'):
            raise BridgeError(Code.INVALID_INPUT, 'Model selections must match the requested normalization engine')
        async with self.locks.setdefault(artifact_id, asyncio.Lock()):
            if engine in ('model','compare'):
                if not self.cloud_formula_ocr:
                    raise BridgeError(Code.NOT_CONFIGURED,'Cloud formula OCR is disabled; user opt-in is required')
                if engine == 'compare':
                    from litbridge.model_comparison import compare
                    return await compare(self,artifact_id,profiles=compare_profiles,force=force,page_limit=page_limit,
                        cloud_limit=cloud_limit,retry_cloud=retry_cloud)
                from litbridge.model_services import create
                from litbridge.cloud_formula import normalize
                client=create(self.model_services,model_profile)
                return await normalize(self,artifact_id,force=force,page_limit=page_limit,
                    cloud_limit=cloud_limit,retry_cloud=retry_cloud,client=client)
            if engine == 'mathpix':
                from litbridge.cloud_formula import normalize
                return await normalize(self,artifact_id,force=force,page_limit=page_limit,
                    cloud_limit=cloud_limit,retry_cloud=retry_cloud)
            if engine == 'docling':
                from litbridge.structured import normalize
                return await normalize(self, artifact_id, force=force, page_limit=page_limit, formulas=formulas)
            if not force:
                cached = self._load(artifact_id)
                if cached:
                    return self.summary(cached, True)
            artifact, path = original(self.store, artifact_id)
            self._path('probe')  # Revalidate root before creating a temporary directory.
            with tempfile.TemporaryDirectory(prefix='normalize-', dir=self.root) as tmp:
                output = Path(tmp) / 'result.json'
                env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
                for key in list(env):
                    if key in ('MATHPIX_APP_ID','MATHPIX_APP_KEY') or key.startswith('LITBRIDGE_MODEL_'):
                        env.pop(key,None)
                process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'litbridge.normalize_worker',
                    str(path), str(output), artifact.id, artifact.sha256, artifact.format,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, env=env)
                try:
                    await asyncio.wait_for(process.wait(), timeout=60)
                except (TimeoutError, asyncio.CancelledError):
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    if asyncio.current_task().cancelling():
                        raise
                    raise BridgeError(Code.TIMEOUT, 'Normalization deadline exceeded; original retained', retryable=True)
                if process.returncode or not output.exists():
                    raise BridgeError(Code.INVALID_CONTENT, 'Parser could not normalize original; original retained')
                if output.stat().st_size > MAX_DOCUMENT:
                    raise BridgeError(Code.TOO_LARGE, 'Canonical document exceeds 16 MiB; original retained')
                payload = json.loads(output.read_text(encoding='utf-8'))
                if 'error' in payload:
                    raise BridgeError(Code(payload['error']), payload['message'], action=payload.get('action'))
                doc = Document.model_validate(payload)
                return self._save(doc, artifact, tmp)

    async def read(self, identifier, offset=0, max_chars=8000):
        if offset < 0 or not 1 <= max_chars <= 20000:
            raise BridgeError(Code.INVALID_INPUT, 'offset must be nonnegative; max_chars must be 1..20000')
        doc = self._load(identifier)
        if doc is None:
            await self.normalize(identifier)
            doc = self._load(identifier)
        end = min(len(doc.markdown), offset + max_chars)
        locations = [{'block_id': b.id, 'kind': b.kind, 'start': b.start, 'end': b.end,
                      'source': b.source.model_dump()} for b in doc.blocks if b.end > offset and b.start < end]
        return {'status': 'ok', 'document': self.summary(doc, True), 'text': doc.markdown[offset:end],
                'offset': offset, 'total_chars': len(doc.markdown), 'next_offset': end if end < len(doc.markdown) else None,
                'locations': locations[:256], 'locations_truncated': len(locations)>256,
                'artifact': original(self.store, doc.artifact_id)[0].model_dump(), 'untrusted_content': True}

    async def outcome(self, artifact_id):
        try:
            return await self.normalize(artifact_id)
        except Exception as exc:
            error = safe_error(exc)
            return {'status': 'error', 'artifact_id': artifact_id, 'original_retained': True,
                    'error': error.model_dump(),
                    'action': error.action or 'Retry normalize; do not download the original again'}
