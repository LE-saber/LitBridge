"""Opt-in, resumable local PDF layout/OCR. Never replaces an original artifact."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from litbridge.documents import MAX_DOCUMENT, original
from litbridge.errors import BridgeError, Code
from litbridge.models import digest

VERSION = '1.0-litbridge-0.1.1-structured.2'
MAX_PAGE = 4 * 1024 * 1024


class Item(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str
    text: str = ''
    rows: list[list[str]] = Field(default_factory=list)
    bbox: tuple[float, float, float, float]
    level: int | None = None
    method: str | None = None


class Page(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job: str
    source_sha: str
    page: int
    width: float
    height: float
    method: str
    items: list[Item]
    warnings: list[str] = Field(default_factory=list)


def checked_path(root, name):
    if root.is_symlink() or not root.is_dir():
        raise BridgeError(Code.STORAGE, 'Unsafe normalization cache directory')
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise BridgeError(Code.STORAGE, 'Unsafe normalization cache file')
    return path


@contextmanager
def job_lock(root):
    path=checked_path(root, '.lock')
    with path.open('a+b') as stream:
        if not stream.tell():
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BridgeError(Code.STORAGE, 'Normalization job is already running', retryable=True) from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def config(docs, formulas):
    if not docs.structured_python or not docs.structured_models:
        raise BridgeError(Code.NOT_CONFIGURED, 'Local structured normalization runtime/models are not configured',
            action='Install the structured extra and prefetch models, then set normalization_python/normalization_models')
    runtime=Path(docs.structured_python).expanduser().resolve()
    models=Path(docs.structured_models).expanduser().resolve()
    if not runtime.is_file() or not models.is_dir():
        raise BridgeError(Code.NOT_CONFIGURED, 'Local structured normalization runtime/models are unavailable')
    manifest=checked_path(models, 'litbridge-models.json')
    try:
        if manifest.stat().st_size > 1024*1024:
            raise ValueError()
        inventory=json.loads(manifest.read_text(encoding='utf-8'))
        if inventory['docling'] != '2.132.0' or not inventory['files'] or len(inventory['files']) > 2000:
            raise ValueError()
        if formulas and not inventory.get('formulas'):
            raise BridgeError(Code.NOT_CONFIGURED, 'Formula models have not been prefetched')
        for entry in inventory['files']:
            path=models/entry['path']
            if path.is_symlink() or not path.resolve().is_relative_to(models) or path.stat().st_size != entry['size']:
                raise ValueError()
            hasher=hashlib.sha256()
            with path.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):
                    hasher.update(chunk)
            if hasher.hexdigest() != entry['sha256']:
                raise ValueError()
    except BridgeError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BridgeError(Code.NOT_CONFIGURED, 'Prefetched model inventory is missing, invalid or changed') from exc
    version=VERSION+'-'+digest(json.dumps({'inventory':inventory,'formulas':formulas},sort_keys=True))[:16]
    return runtime, models, version


def parse_page(raw, job, sha, number):
    try:
        page=Page.model_validate_json(raw)
    except ValidationError as exc:
        raise BridgeError(Code.INVALID_CONTENT, 'Invalid structured page record') from exc
    if (page.job,page.source_sha,page.page)!=(job,sha,number) or len(page.items)>20000:
        raise BridgeError(Code.INVALID_CONTENT, 'Structured page source identity mismatch')
    if page.method not in ('layout','ocr') or not all(math.isfinite(x) and 0<x<=20000 for x in (page.width,page.height)):
        raise BridgeError(Code.INVALID_CONTENT, 'Invalid structured page geometry')
    for item in page.items:
        if item.kind not in ('heading','paragraph','table','figure','formula','list_item'):
            raise BridgeError(Code.INVALID_CONTENT, 'Invalid structured block kind')
        l,t,r,b=item.bbox
        if not all(math.isfinite(x) for x in item.bbox) or not (0<=l<=r<=page.width+1 and 0<=t<=b<=page.height+1):
            raise BridgeError(Code.INVALID_CONTENT, 'Invalid structured block locator')
        if len(item.rows)>1000 or any(len(row)>1000 for row in item.rows):
            raise BridgeError(Code.TOO_LARGE, 'Structured table exceeds cell bounds')
    return page


async def run_worker(runtime, models, source, sha, job, pages, output, formulas):
    env={**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parents[1]),
        'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1',
        'DO_NOT_TRACK':'1','OMP_NUM_THREADS':'2','TOKENIZERS_PARALLELISM':'false'}
    for key in list(env):
        if key in ('MATHPIX_APP_ID','MATHPIX_APP_KEY') or key.startswith('LITBRIDGE_MODEL_'):
            env.pop(key,None)
    process=await asyncio.create_subprocess_exec(str(runtime),'-m','litbridge.structured_worker',
        str(source),sha,job,json.dumps(pages),str(output),str(models),'1' if formulas else '0',
        env=env,stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(),timeout=min(840,120+len(pages)*240))
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        if asyncio.current_task().cancelling():
            raise
        return 'timeout'
    return None if process.returncode==0 else 'failed'


async def normalize(docs, artifact_id, *, force, page_limit, formulas):
    if not 1<=page_limit<=5:
        raise BridgeError(Code.INVALID_INPUT, 'Structured page_limit must be 1..5')
    artifact, source=original(docs.store,artifact_id)
    if artifact.format!='pdf':
        raise BridgeError(Code.UNSUPPORTED, 'Structured normalization currently accepts retained PDF originals')
    runtime, models, version=config(docs,formulas)
    cached=docs._load(artifact_id,version=version)
    if cached and not force:
        return docs.summary(cached,True)
    # The inexpensive page count check does not import any heavy model.
    from pypdf import PdfReader
    reader=PdfReader(source,strict=False)
    if reader.is_encrypted and not reader.decrypt(''):
        raise BridgeError(Code.UNSUPPORTED, 'PDF requires an opening password; normalization skipped',
            action='Skip this password-protected original; no password guessing or repeated download')
    count=len(reader.pages)
    if not 1<=count<=100:
        raise BridgeError(Code.TOO_LARGE, 'Structured PDF must contain 1..100 pages')
    job='doc_'+digest(artifact.id+artifact.sha256+version)[:24]
    root=docs.store.home/'normalization'
    if root.is_symlink():
        raise BridgeError(Code.STORAGE,'Unsafe normalization cache directory')
    root.mkdir(exist_ok=True)
    folder=checked_path(root,job)
    folder.mkdir(exist_ok=True)
    with job_lock(folder):
        with docs.store.db:
            docs.store.db.execute('CREATE TABLE IF NOT EXISTS normalization_pages_v1 (job TEXT, page INTEGER, sha TEXT, PRIMARY KEY(job,page))')
            if force:
                docs.store.db.execute('DELETE FROM normalization_pages_v1 WHERE job=?',(job,))
        complete={}; total=0
        for number, expected in docs.store.db.execute('SELECT page,sha FROM normalization_pages_v1 WHERE job=?',(job,)):
            path=checked_path(folder,f'{number}.json')
            if not 1<=number<=count or path.stat().st_size>MAX_PAGE:
                raise BridgeError(Code.INVALID_CONTENT,'Invalid structured page cache')
            raw=path.read_bytes(); total+=len(raw)
            if total>MAX_DOCUMENT:
                raise BridgeError(Code.TOO_LARGE,'Structured page cache exceeds document bound')
            if hashlib.sha256(raw).hexdigest()!=expected:
                raise BridgeError(Code.INVALID_CONTENT,'Structured page cache checksum mismatch')
            complete[number]=parse_page(raw,job,artifact.sha256,number)
        reused=len(complete)
        pending=[n for n in range(1,count+1) if n not in complete]
        failed=[]
        with tempfile.TemporaryDirectory(prefix='structured-',dir=folder) as tmp:
            target=Path(tmp)
            if pending:
                selected=pending[:page_limit]
                cancelled=False
                try:
                    outcome=await run_worker(runtime,models,source,artifact.sha256,job,selected,target,formulas)
                except asyncio.CancelledError:
                    # The child has stopped. Commit only complete, validated page
                    # files before propagating cancellation to the caller.
                    cancelled=True
                    outcome='cancelled'
                failure=target/'error.json'
                if failure.is_file():
                    # Only our safe codes/messages are allowed at the boundary.
                    raise BridgeError(Code.NOT_CONFIGURED,'Local structured engine/models failed initialization',
                        action='Check prefetched models and the pinned runtime; originals are retained')
                for number in selected:
                    result=target/f'{number}.json'
                    if not result.is_file():
                        failed.append(number); continue
                    if result.stat().st_size>MAX_PAGE:
                        raise BridgeError(Code.TOO_LARGE,'Structured page exceeds 4 MiB')
                    raw=result.read_bytes(); total+=len(raw)
                    complete[number]=parse_page(raw,job,artifact.sha256,number)
                    if total>MAX_DOCUMENT:
                        raise BridgeError(Code.TOO_LARGE,'Structured page cache exceeds document bound')
                    destination=checked_path(folder,f'{number}.json')
                    os.replace(result,destination)
                    with docs.store.db:
                        docs.store.db.execute('INSERT OR REPLACE INTO normalization_pages_v1 VALUES (?,?,?)',
                            (job,number,hashlib.sha256(raw).hexdigest()))
                if cancelled:
                    raise asyncio.CancelledError
            pending=[n for n in range(1,count+1) if n not in complete]
            if pending:
                return {'status':'in_progress','job_id':job,'artifact_id':artifact.id,
                    'completed_pages':len(complete),'total_pages':count,'pending_pages':pending,
                    'reused_pages':reused,'failed_pages':failed,'original_retained':True,
                    'worker_status':outcome or 'complete',
                    'action':'Repeat normalize with engine=docling and the same formulas option to resume'}
            from litbridge.normalize_worker import Parser
            parser=Parser(artifact.id,artifact.sha256,'pdf')
            for number in range(1,count+1):
                page=complete[number]
                parent=parser.add('heading',f'Page {number}',page=number,level=2)
                parser.warnings.extend(page.warnings)
                if any('unresolved' in warning or 'partial success' in warning or
                        'verify numerical values' in warning for warning in page.warnings):
                    parser.status='partial'
                if not any(i.text.strip() or i.rows for i in page.items):
                    parser.status='partial'
                    parser.warnings.append(f'No recognized content on page {number}; inspect original')
                for item in page.items:
                    bid=parser.add(item.kind,item.text,page=number,parent=parent,level=item.level,rows=item.rows)
                    parser.blocks[-1].source.bbox=item.bbox
                    parser.blocks[-1].source.method=item.method or page.method
                    if item.kind=='heading':
                        parent=bid
                    if item.kind=='formula':
                        parser.status='partial'
            parser.warnings.append('Local model layout/OCR is heuristic; formula recognition requires verification against originals')
            doc=parser.finish().model_copy(update={'id':job,'parser_version':version})
            original(docs.store,artifact.id)  # Verify bytes again before publication.
            result=docs._save(doc,artifact,tmp)
            result.update(engine='docling',completed_pages=count,total_pages=count,reused_pages=reused)
            return result
