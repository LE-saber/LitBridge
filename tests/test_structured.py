"""Bounded structured normalization protocol, without downloading model weights in CI."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import pytest
from litbridge.documents import Documents
from litbridge.errors import BridgeError
from litbridge.storage import Store
from litbridge import structured
from test_documents import pdf_bytes


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'data')
    models=tmp_path/'models'; models.mkdir()
    (models/'weight.bin').write_bytes(b'synthetic-model')
    manifest={'docling':'2.132.0','formulas':False,'files':[{'path':'weight.bin',
        'size':15,'sha256':hashlib.sha256(b'synthetic-model').hexdigest()}]}
    (models/'litbridge-models.json').write_text(json.dumps(manifest))
    docs=Documents(store,structured_python=sys.executable,structured_models=models)
    artifact=store.save('paper','fixture','pdf','https://example.org',pdf_bytes())
    yield store,docs,artifact,models
    store.close()


async def fake_worker(runtime,models,source,sha,job,pages,output,formulas):
    for n in pages:
        p=structured.Page(job=job,source_sha=sha,page=n,width=400,height=500,method='layout',items=[
            structured.Item(kind='paragraph',text=f'Native fixture page {n}',bbox=(20,20,200,40)),
            structured.Item(kind='table',text='Table',bbox=(20,50,200,100),rows=[['A','B'],['1','2']])])
        (output/f'{n}.json').write_text(p.model_dump_json(),encoding='utf-8')


async def test_resumes_pages_publishes_locators_and_reuses_completed_document(setup,monkeypatch):
    store,docs,a,models=setup
    monkeypatch.setattr(structured,'run_worker',fake_worker)
    first=await docs.normalize(a.id,engine='docling',page_limit=1)
    assert first['status']=='in_progress' and first['completed_pages']==1 and first['pending_pages']==[2]
    assert store.db.execute('SELECT COUNT(*) FROM documents_v1').fetchone()[0]==0
    second=await docs.normalize(a.id,engine='docling',page_limit=1)
    assert second['status']=='ready' and second['completed_pages']==2 and second['reused_pages']==1
    read=await docs.read(second['document_id'])
    assert '| 1 | 2 |' in read['text']
    assert {x['source']['page'] for x in read['locations']}=={1,2}
    assert sum(x['source']['bbox'] is not None for x in read['locations'])==4
    assert Path(a.path).read_bytes()==pdf_bytes()
    assert (await docs.normalize(a.id,engine='docling'))['reused']
    # Artifact ID keeps the lightweight cache contract; explicit model doc ID selects structured output.
    assert (await docs.read(a.id))['document']['parser_version']!=second['parser_version']


async def test_page_cache_tamper_fails_closed(setup,monkeypatch):
    store,docs,a,models=setup
    monkeypatch.setattr(structured,'run_worker',fake_worker)
    first=await docs.normalize(a.id,engine='docling',page_limit=1)
    (store.home/'normalization'/first['job_id']/'1.json').write_text('tampered')
    with pytest.raises(BridgeError,match='checksum'):
        await docs.normalize(a.id,engine='docling')
    repaired=await docs.normalize(a.id,engine='docling',force=True)
    assert repaired['status']=='ready' and repaired['reused_pages']==0


@pytest.mark.parametrize('change',['source','page','bbox','kind'])
async def test_worker_mismatched_or_invalid_record_is_rejected(setup,monkeypatch,change):
    store,docs,a,models=setup
    async def invalid(*args):
        await fake_worker(*args)
        p=args[6]/'1.json'; payload=json.loads(p.read_text())
        if change=='source': payload['source_sha']='not-the-source'
        if change=='page': payload['page']=99
        if change=='bbox': payload['items'][0]['bbox']=[0,0,99999,30]
        if change=='kind': payload['items'][0]['kind']='script'
        p.write_text(json.dumps(payload))
    monkeypatch.setattr(structured,'run_worker',invalid)
    with pytest.raises(BridgeError):
        await docs.normalize(a.id,engine='docling',page_limit=1)
    assert store.db.execute('SELECT COUNT(*) FROM documents_v1').fetchone()[0]==0


async def test_no_implicit_install_or_model_download_when_unconfigured(setup):
    store,docs,a,models=setup
    with pytest.raises(BridgeError,match='not configured'):
        await Documents(store).normalize(a.id,engine='docling')
    with pytest.raises(BridgeError,match='Formula models'):
        await docs.normalize(a.id,engine='docling',formulas=True)
    (models/'weight.bin').write_bytes(b'changed-weights')
    with pytest.raises(BridgeError,match='inventory'):
        await docs.normalize(a.id,engine='docling')


async def test_partial_worker_completion_keeps_progress_for_retry(setup,monkeypatch):
    store,docs,a,models=setup
    async def limited(runtime,models,source,sha,job,pages,output,formulas):
        await fake_worker(runtime,models,source,sha,job,pages[:1],output,formulas)
        return 'timeout'
    monkeypatch.setattr(structured,'run_worker',limited)
    first=await docs.normalize(a.id,engine='docling')
    assert first['status']=='in_progress' and first['failed_pages']==[2]
    assert first['worker_status']=='timeout'
    monkeypatch.setattr(structured,'run_worker',fake_worker)
    assert (await docs.normalize(a.id,engine='docling'))['reused_pages']==1


async def test_model_options_get_separate_cache_identity(setup,monkeypatch):
    store,docs,a,models=setup
    monkeypatch.setattr(structured,'run_worker',fake_worker)
    base=await docs.normalize(a.id,engine='docling')
    manifest=json.loads((models/'litbridge-models.json').read_text()); manifest['formulas']=True
    (models/'litbridge-models.json').write_text(json.dumps(manifest))
    formula=await docs.normalize(a.id,engine='docling',formulas=True)
    assert base['document_id']!=formula['document_id']
    assert (await docs.read(base['document_id']))['document']['parser_version']==base['parser_version']


async def test_cancel_kills_structured_subprocess(setup,monkeypatch):
    original_spawn=asyncio.create_subprocess_exec
    children=[]
    async def spawn(*args,**kwargs):
        process=await original_spawn(sys.executable,'-c','import time; time.sleep(30)',**kwargs)
        children.append(process); return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    task=asyncio.create_task(structured.run_worker(Path(sys.executable),Path('.'),Path('.'),
        'sha','job',[1],Path('.'),False))
    async def started():
        while not children:
            if task.done():
                await task
            await asyncio.sleep(.01)
    await asyncio.wait_for(started(),5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert children[0].returncode is not None


def test_job_lock_excludes_another_owner(tmp_path):
    with structured.job_lock(tmp_path):
        with pytest.raises(BridgeError,match='already running'):
            with structured.job_lock(tmp_path):
                pass


@pytest.mark.parametrize('limit',[0,6])
async def test_invalid_page_limit_is_rejected_before_parsing(setup,limit):
    _,docs,a,_=setup
    with pytest.raises(BridgeError,match='page_limit'):
        await docs.normalize(a.id,engine='docling',page_limit=limit)

def test_cell_ocr_repairs_only_suspicious_mapping_and_keeps_low_confidence(monkeypatch):
    from types import SimpleNamespace as NS
    from litbridge.structured_worker import table_rows
    monkeypatch.setitem(sys.modules,'numpy',NS(array=lambda crop:crop))
    box=NS(l=10,t=10,r=45,b=20)
    bbox=NS(to_top_left_origin=lambda height:box)
    texts=['640 STX 640','92:72','0.01']
    cells=[NS(text=t,bbox=bbox,start_row_offset_idx=n,end_row_offset_idx=n+1,
        start_col_offset_idx=0,end_col_offset_idx=1) for n,t in enumerate(texts)]
    item=NS(data=NS(grid=[[c] for c in cells],table_cells=cells),prov=[NS(page_no=1)])
    image=NS(height=300,width=300,crop=lambda bounds:NS(width=110,height=40))
    doc=NS(pages={1:NS(image=NS(pil_image=image))})
    outputs=iter([NS(txts=['640','×','640'],scores=[.98,.98,.98]),NS(txts=['92.72'],scores=[.4])])
    def reader(crop,**kwargs):
        assert kwargs==dict(use_det=False,use_cls=False,use_rec=True)
        reader.use_det=False
        reader.use_cls=False
        return next(outputs)
    reader.use_det=True
    reader.use_cls=True
    reader.use_rec=True
    rows,repaired,unresolved=table_rows(item,doc,reader,100)
    assert rows==[['640 × 640'],['92:72'],['0.01']]
    assert (repaired,unresolved)==(1,1)
    assert reader.use_det and reader.use_cls and reader.use_rec
    assert cells[0].text=='640 STX 640'  # Native evidence is not rewritten.


async def test_unresolved_table_warns_partial_and_retains_region(setup,monkeypatch):
    _,docs,a,_=setup
    async def worker(*args):
        await fake_worker(*args)
        for n in args[5]:
            path=args[6]/f'{n}.json';p=json.loads(path.read_text())
            p['warnings']=['1 suspicious table font mappings remain unresolved; consult original region']
            path.write_text(json.dumps(p))
    monkeypatch.setattr(structured,'run_worker',worker)
    result=await docs.normalize(a.id,engine='docling')
    assert result['status']=='partial'
    assert '| 1 | 2 |' in (await docs.read(result['document_id']))['text']


def test_malformed_page_has_safe_boundary_error():
    with pytest.raises(BridgeError,match='Invalid structured page record'):
        structured.parse_page(b'{"items": "private-invalid-payload"}','job','sha',1)

async def test_cancel_checkpoints_completed_pages_before_propagating(setup,monkeypatch):
    store,docs,a,_=setup
    async def cancelled(runtime,models,source,sha,job,pages,output,formulas):
        await fake_worker(runtime,models,source,sha,job,pages[:1],output,formulas)
        raise asyncio.CancelledError
    monkeypatch.setattr(structured,'run_worker',cancelled)
    with pytest.raises(asyncio.CancelledError):
        await docs.normalize(a.id,engine='docling')
    assert store.db.execute('SELECT COUNT(*) FROM normalization_pages_v1').fetchone()[0]==1
    assert store.db.execute('SELECT COUNT(*) FROM documents_v1').fetchone()[0]==0
    monkeypatch.setattr(structured,'run_worker',fake_worker)
    result=await docs.normalize(a.id,engine='docling')
    assert result['reused_pages']==1 and result['status']=='ready'
