"""Explicit Mathpix formula-region enhancement; no full-paper uploads or automatic retries."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import httpx
from litbridge.errors import BridgeError, Code
from litbridge.documents import original, MAX_DOCUMENT
from litbridge.models import digest
from litbridge.structured import checked_path, job_lock

VERSION='1.0-litbridge-0.1.1-mathpix.1'
MAX_CROP=1024*1024
MAX_RESPONSE=256*1024
MAX_RECORD=64*1024
MIN_CONFIDENCE=.9


class Mathpix:
    engine = 'mathpix'
    method = 'mathpix'
    version = VERSION

    def __init__(self, app_id, app_key, *, transport=None):
        if not app_id or not app_key:
            raise BridgeError(Code.NOT_CONFIGURED,'Mathpix credentials are not configured',
                action='Set MATHPIX_APP_ID and MATHPIX_APP_KEY in the local process environment')
        self.app_id,self.app_key,self.transport=app_id,app_key,transport

    async def recognize(self, image):
        if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image)>MAX_CROP:
            raise BridgeError(Code.INVALID_CONTENT,'Formula crop is not a bounded PNG')
        payload={'src':'data:image/png;base64,'+base64.b64encode(image).decode('ascii'),
            'formats':['latex_styled'],'metadata':{'improve_mathpix':False},
            'include_equation_tags':True,'enable_document_layout':False}
        try:
            # Fixed origin, no redirects, ambient proxies, response logging or retries.
            async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False,
                    transport=self.transport) as client:
                async with client.stream('POST','https://api.mathpix.com/v3/text',json=payload,
                        headers={'app_id':self.app_id,'app_key':self.app_key}) as response:
                    if response.status_code in (401,403):
                        raise BridgeError(Code.NOT_CONFIGURED,'Mathpix rejected the configured credentials')
                    if response.status_code==429:
                        raise BridgeError(Code.RATE_LIMITED,'Mathpix quota/rate limit reached; no automatic retry')
                    if response.status_code!=200:
                        raise BridgeError(Code.UPSTREAM,'Mathpix request did not succeed; no automatic retry')
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>MAX_RESPONSE:
                            raise BridgeError(Code.TOO_LARGE,'Mathpix response exceeds bound')
            result=json.loads(raw)
            if not isinstance(result,dict) or result.get('error'):
                raise BridgeError(Code.INVALID_CONTENT,'Mathpix did not return a recognized formula')
            text=result.get('latex_styled');confidence=result.get('confidence')
            rate=result.get('confidence_rate');model=result.get('version','unspecified')
            if (not isinstance(text,str) or not 0<len(text.strip())<=8192 or
                    any(not isinstance(c,(int,float)) or isinstance(c,bool) or
                        not math.isfinite(c) or not 0<=c<=1 for c in (confidence,rate)) or
                    not isinstance(model,str) or len(model)>100):
                raise BridgeError(Code.INVALID_CONTENT,'Mathpix formula response is incomplete or invalid')
            # Do not execute/render returned TeX; paper/model content is untrusted.
            return {'text':text.strip(),'confidence':min(confidence,rate),'model':model}
        except BridgeError:
            raise
        except httpx.TimeoutException as exc:
            raise BridgeError(Code.TIMEOUT,'Mathpix timed out; request may have been billed; no automatic retry') from exc
        except httpx.HTTPError as exc:
            raise BridgeError(Code.NETWORK,'Mathpix transport failed; request may have been billed; no automatic retry') from exc
        except (ValueError,TypeError) as exc:
            raise BridgeError(Code.INVALID_CONTENT,'Mathpix returned an invalid response') from exc


async def crop_worker(docs,source,sha,regions,target):
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    for key in list(env):
        if key in ('MATHPIX_APP_ID','MATHPIX_APP_KEY') or key.startswith('LITBRIDGE_MODEL_'):
            env.pop(key,None)
    process=await asyncio.create_subprocess_exec(str(docs.structured_python),'-m','litbridge.formula_crop_worker',
        str(source),sha,json.dumps(regions),str(target),env=env,
        stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(),60)
    except (TimeoutError,asyncio.CancelledError):
        if process.returncode is None: process.kill()
        await process.wait()
        if asyncio.current_task().cancelling(): raise
        raise BridgeError(Code.TIMEOUT,'Local formula crop deadline exceeded; no cloud request sent')
    if process.returncode:
        raise BridgeError(Code.INVALID_CONTENT,'Formula crops could not be rendered; no cloud request sent')


def save_record(store,folder,job,bid,record):
    raw=json.dumps(record,ensure_ascii=False).encode('utf-8')
    if len(raw)>MAX_RECORD: raise BridgeError(Code.TOO_LARGE,'Cloud formula cache record exceeds bound')
    path=checked_path(folder,bid+'.json');stage=checked_path(folder,bid+'.tmp')
    stage.write_bytes(raw);os.replace(stage,path)
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO normalization_cloud_v1 VALUES (?,?,?)',
            (job,bid,hashlib.sha256(raw).hexdigest()))


async def normalize(docs,artifact_id,*,force,page_limit,cloud_limit,retry_cloud,client=None):
    if not docs.cloud_formula_ocr:
        raise BridgeError(Code.NOT_CONFIGURED,'Cloud formula OCR is disabled',
            action='User must explicitly enable cloud_formula_ocr; formula crops leave this machine and may incur charges')
    if not 1<=cloud_limit<=20:
        raise BridgeError(Code.INVALID_INPUT,'cloud_limit must be 1..20')
    client=client or Mathpix(os.getenv('MATHPIX_APP_ID',''),os.getenv('MATHPIX_APP_KEY',''))
    metadata={'engine':client.engine}
    if getattr(client,'profile_id',None): metadata['model_profile']=client.profile_id
    resume=f'Repeat normalize with engine={client.engine} and the same model profile to resume'
    # Keep local engine offline. Cloud work is a separate, narrowly scoped stage.
    from litbridge import structured
    base=await structured.normalize(docs,artifact_id,force=False,page_limit=page_limit,formulas=False)
    if base['status']=='in_progress':
        return {**base,**metadata,'phase':'local_layout',
            'processing_progress_pct':round(100*base['completed_pages']/base['total_pages'],1),
            'verified_quality_pct':None,
            'action':resume+' local layout; no cloud requests have been sent'}
    doc=docs._load(base['document_id']);artifact,source=original(docs.store,artifact_id)
    version=client.version+'-'+digest(doc.parser_version)[:16]
    cached=docs._load(artifact_id,version=version)
    if cached and not force:
        count=sum(b.kind=='formula' for b in cached.blocks)
        return {**docs.summary(cached,True),**metadata,'phase':'complete',
            'base_document_id':doc.id,'completed_formulas':count,'total_formulas':count,
            'processing_progress_pct':100,'verified_quality_pct':None}
    job='doc_'+digest(artifact.id+artifact.sha256+version)[:24]
    root=docs.store.home/'normalization'
    if root.is_symlink(): raise BridgeError(Code.STORAGE,'Unsafe normalization cache directory')
    root.mkdir(exist_ok=True)
    folder=checked_path(root,job);folder.mkdir(exist_ok=True)
    formulas=[b for b in doc.blocks if b.kind=='formula'];ids={b.id for b in formulas}
    with job_lock(folder):
        with docs.store.db:
            docs.store.db.execute('CREATE TABLE IF NOT EXISTS normalization_cloud_v1 '
                '(job TEXT, block TEXT, sha TEXT, PRIMARY KEY(job,block))')
            if force: docs.store.db.execute('DELETE FROM normalization_cloud_v1 WHERE job=?',(job,))
        records={};total=0
        for bid,sha in docs.store.db.execute('SELECT block,sha FROM normalization_cloud_v1 WHERE job=?',(job,)):
            if bid not in ids: raise BridgeError(Code.INVALID_CONTENT,'Cloud formula cache identity mismatch')
            path=checked_path(folder,bid+'.json')
            if path.stat().st_size>MAX_RECORD: raise BridgeError(Code.TOO_LARGE,'Cloud formula cache exceeds bound')
            raw=path.read_bytes();total+=len(raw)
            if total>MAX_DOCUMENT: raise BridgeError(Code.TOO_LARGE,'Cloud formula cache exceeds document bound')
            if hashlib.sha256(raw).hexdigest()!=sha:
                raise BridgeError(Code.INVALID_CONTENT,'Cloud formula cache checksum mismatch')
            record=json.loads(raw)
            if not isinstance(record,dict) or record.get('status') not in ('sent','error','recognized','low_confidence'):
                raise BridgeError(Code.INVALID_CONTENT,'Cloud formula cache is invalid')
            records[bid]=record
        done={bid for bid,r in records.items() if r['status'] in ('recognized','low_confidence')}
        blocked={bid for bid,r in records.items() if r['status'] in ('sent','error')}
        selected=[b for b in formulas if b.id not in done and (b.id not in blocked or retry_cloud)][:cloud_limit]
        with tempfile.TemporaryDirectory(prefix='formula-crops-',dir=folder) as tmp:
            target=Path(tmp)
            if selected:
                regions=[{'id':b.id,'page':b.source.page,'bbox':b.source.bbox} for b in selected]
                await crop_worker(docs,source,artifact.sha256,regions,target)
                for b in selected:
                    crop=checked_path(target,b.id+'.png')
                    if not crop.is_file() or crop.stat().st_size>MAX_CROP:
                        raise BridgeError(Code.INVALID_CONTENT,'Formula crop is missing or oversized; no request for this region')
                    image=crop.read_bytes();crop_sha=hashlib.sha256(image).hexdigest()
                    record={'status':'sent','crop_sha256':crop_sha}
                    # Persist before the paid POST. Cancel/crash leaves a sent
                    # checkpoint, preventing an implicit repeat with unknown billing.
                    save_record(docs.store,folder,job,b.id,record);records[b.id]=record
                    try:
                        recognized=await client.recognize(image)
                        confidence=recognized['confidence']
                        record.update(recognized,status='recognized' if confidence is None or confidence>=MIN_CONFIDENCE else 'low_confidence')
                    except BridgeError as exc:
                        record.update(status='error',error=exc.info.model_dump(mode='json'))
                    save_record(docs.store,folder,job,b.id,record);records[b.id]=record
                    if record['status']=='error': break
            done={bid for bid,r in records.items() if r['status'] in ('recognized','low_confidence')}
            blocked={bid for bid,r in records.items() if r['status'] in ('sent','error')}
            if len(done)<len(formulas):
                return {'status':'needs_action' if blocked else 'in_progress',**metadata,'phase':'cloud_formulas',
                    'job_id':job,'artifact_id':artifact.id,'base_document_id':doc.id,
                    'completed_formulas':len(done),'total_formulas':len(formulas),
                    'processing_progress_pct':round(100*len(done)/len(formulas),1),
                    'verified_quality_pct':None,'blocked_formulas':sorted(blocked),
                    'errors':[r['error'] for r in records.values() if r.get('error')],
                    'action':'Repeat with the same engine to resume. For failed/uncertain billed requests, explicitly choose retry_cloud=true after checking usage'}
            from litbridge.normalize_worker import Parser
            parser=Parser(artifact.id,artifact.sha256,'pdf');parents={}
            parser.warnings=[w for w in doc.warnings if w!='Formula model not enabled or no formula recognized; original region retained']
            parser.status='partial' if formulas else doc.status
            for block in doc.blocks:
                text=block.text;record=records.get(block.id)
                if record and record['status']=='recognized': text='$$\n'+record['text']+'\n$$'
                bid=parser.add(block.kind,text,page=block.source.page,parent=parents.get(block.parent_id),
                    level=block.level,rows=block.rows)
                parents[block.id]=bid;parser.blocks[-1].source=block.source.model_copy(deep=True)
                if record:
                    parser.blocks[-1].source.method=client.method if record['status']=='recognized' else client.method+'_low_confidence'
                    parser.blocks[-1].source.confidence=record['confidence']
                    parser.blocks[-1].source.model=record['model']
                    parser.blocks[-1].source.crop_sha256=record['crop_sha256']
                    parser.blocks[-1].source.model_profile=getattr(client,'profile_id',None)
                    if record['status']=='low_confidence':
                        parser.warnings.append('Low-confidence cloud formula retained local text; inspect original region')
            if formulas: parser.warnings.append('Cloud-generated formulas require original-region verification; confidence is not measured accuracy')
            enhanced=parser.finish().model_copy(update={'id':job,'title':doc.title,'parser_version':version})
            original(docs.store,artifact.id)
            result=docs._save(enhanced,artifact,tmp)
            return {**result,**metadata,'phase':'complete','base_document_id':doc.id,
                'completed_formulas':len(formulas),'total_formulas':len(formulas),
                'processing_progress_pct':100,'verified_quality_pct':None}
