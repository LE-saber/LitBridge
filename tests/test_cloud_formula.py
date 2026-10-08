"""No real cloud requests, credentials or article fixtures in these tests."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import httpx
import pytest
from litbridge import cloud_formula as cloud, structured
from litbridge.documents import Documents
from litbridge.errors import BridgeError, Code
from litbridge.normalize_worker import Parser
from litbridge.storage import Store
from test_documents import pdf_bytes

PNG=b'\x89PNG\r\n\x1a\nsynthetic-crop'


async def test_mathpix_sends_only_explicit_crop_and_disables_retention_request():
    requests=[]
    def handler(request):
        requests.append(request)
        assert str(request.url)=='https://api.mathpix.com/v3/text'
        data=json.loads(request.content)
        assert data['metadata']=={'improve_mathpix':False}
        assert data['src'].startswith('data:image/png;base64,')
        assert data['formats']==['latex_styled'] and data['include_equation_tags']
        return httpx.Response(200,json={'latex_styled':r'P=\frac{TP}{TP+FP}',
            'confidence':.98,'confidence_rate':.95,'version':'fixture-model'})
    result=await cloud.Mathpix('test-id','test-key',transport=httpx.MockTransport(handler)).recognize(PNG)
    assert result['confidence']==.95 and len(requests)==1


@pytest.mark.parametrize('status,code',[(401,Code.NOT_CONFIGURED),(403,Code.NOT_CONFIGURED),
    (429,Code.RATE_LIMITED),(500,Code.UPSTREAM),(302,Code.UPSTREAM)])
async def test_remote_errors_do_not_leak_payload_or_retry(status,code):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={'error':'test-key private-content'},headers={'location':'https://example.org'})
    with pytest.raises(BridgeError) as error:
        await cloud.Mathpix('test-id','test-key',transport=httpx.MockTransport(handler)).recognize(PNG)
    assert error.value.info.code==code and len(calls)==1
    assert 'test-key' not in str(error.value) and 'private-content' not in str(error.value)
    assert not error.value.info.retryable


@pytest.mark.parametrize('response',[{'latex_styled':'x','confidence':float('nan'),'confidence_rate':1},
    {'latex_styled':'x','confidence':1}, {'text':'some text','confidence':1,'confidence_rate':1},
    {'latex_styled':'x'*8193,'confidence':1,'confidence_rate':1},
    {'error':'private text'},{'latex_styled':'x','confidence':True,'confidence_rate':1}])
async def test_malformed_or_non_formula_response_is_rejected(response):
    with pytest.raises(BridgeError,match='formula|invalid'):
        await cloud.Mathpix('test-id','test-key',transport=httpx.MockTransport(
            lambda r:httpx.Response(200,json=response))).recognize(PNG)


async def test_timeout_is_not_automatically_retried():
    requests=[]
    def handler(request):
        requests.append(request);raise httpx.ReadTimeout('private transport message',request=request)
    with pytest.raises(BridgeError,match='may have been billed'):
        await cloud.Mathpix('test-id','test-key',transport=httpx.MockTransport(handler)).recognize(PNG)
    assert len(requests)==1


async def test_oversized_response_is_bounded():
    with pytest.raises(BridgeError,match='bound'):
        await cloud.Mathpix('test-id','test-key',transport=httpx.MockTransport(
            lambda r:httpx.Response(200,content=b' '* (cloud.MAX_RESPONSE+1)))).recognize(PNG)


@pytest.fixture
def setup(tmp_path,monkeypatch):
    store=Store(tmp_path/'data');a=store.save('paper','fixture','pdf','https://example.org',pdf_bytes())
    docs=Documents(store,cloud_formula_ocr=True)
    parser=Parser(a.id,a.sha256,'pdf')
    for page in (1,2):
        heading=parser.add('heading',f'Page {page}',page=page,level=2)
        parser.add('paragraph',f'Native fixture page {page}',page=page,parent=heading)
        parser.add('formula','[Formula structure unverified; consult original region]',page=page,parent=heading)
        parser.blocks[-1].source.bbox=(10,10,80,40)
    base=parser.finish();base.status='partial'
    with tempfile.TemporaryDirectory(dir=store.home) as tmp: docs._save(base,a,tmp)
    async def layout(*args,**kwargs):
        assert kwargs['formulas'] is False
        return docs.summary(base,True)
    async def crops(docs,source,sha,regions,target):
        assert sha==a.sha256 and Path(source).read_bytes()==pdf_bytes()
        for region in regions: (target/(region['id']+'.png')).write_bytes(PNG)
    calls=[]
    async def recognize(self,image):
        calls.append(image)
        return {'text':r'P=\frac{TP}{TP+FP}','confidence':.98,'model':'fixture-model'}
    monkeypatch.setenv('MATHPIX_APP_ID','test-id');monkeypatch.setenv('MATHPIX_APP_KEY','test-key')
    monkeypatch.setattr(structured,'normalize',layout);monkeypatch.setattr(cloud,'crop_worker',crops)
    monkeypatch.setattr(cloud.Mathpix,'recognize',recognize)
    yield store,docs,a,base,calls
    store.close()


async def test_cloud_is_disabled_unless_user_enables_it(setup):
    _,docs,a,_,calls=setup;docs.cloud_formula_ocr=False
    with pytest.raises(BridgeError,match='disabled'): await docs.normalize(a.id,engine='mathpix')
    assert not calls


async def test_enabled_cloud_without_key_is_not_fabricated_success(setup,monkeypatch):
    _,docs,a,_,calls=setup;monkeypatch.delenv('MATHPIX_APP_KEY')
    with pytest.raises(BridgeError,match='credentials'): await docs.normalize(a.id,engine='mathpix')
    assert not calls


async def test_formula_limit_resume_preserves_original_and_separates_quality(setup):
    store,docs,a,base,calls=setup
    first=await docs.normalize(a.id,engine='mathpix',cloud_limit=1)
    assert first['status']=='in_progress' and first['processing_progress_pct']==50
    assert first['verified_quality_pct'] is None and len(calls)==1
    result=await docs.normalize(a.id,engine='mathpix',cloud_limit=1)
    assert result['status']=='partial' and result['processing_progress_pct']==100 and len(calls)==2
    read=await docs.read(result['document_id'])
    assert r'\frac{TP}{TP+FP}' in read['text']
    locations=[x['source'] for x in read['locations'] if x['source']['method']=='mathpix']
    assert len(locations)==2 and all(x['confidence']==.98 and x['model']=='fixture-model' for x in locations)
    assert all(x['crop_sha256']==hashlib.sha256(PNG).hexdigest() for x in locations)
    assert Path(a.path).read_bytes()==pdf_bytes() and (await docs.read(base.id))['text']==base.markdown
    cached=await docs.normalize(a.id,engine='mathpix')
    assert cached['reused'] and len(calls)==2
    assert cached['processing_progress_pct']==100 and cached['verified_quality_pct'] is None


async def test_low_confidence_keeps_native_marker(setup,monkeypatch):
    _,docs,a,_,_=setup
    async def uncertain(self,image):return {'text':'unverified prediction','confidence':.6,'model':'fixture'}
    monkeypatch.setattr(cloud.Mathpix,'recognize',uncertain)
    result=await docs.normalize(a.id,engine='mathpix')
    read=await docs.read(result['document_id'])
    assert result['status']=='partial' and 'unverified prediction' not in read['text']
    assert 'Formula structure unverified' in read['text']
    assert any(x['source']['method']=='mathpix_low_confidence' for x in read['locations'])


async def test_failed_post_needs_explicit_retry_and_never_repeats_success(setup,monkeypatch):
    _,docs,a,_,calls=setup
    async def failing(self,image):
        calls.append(image);raise BridgeError(Code.TIMEOUT,'Request may have been billed')
    monkeypatch.setattr(cloud.Mathpix,'recognize',failing)
    first=await docs.normalize(a.id,engine='mathpix',cloud_limit=1)
    assert first['status']=='needs_action' and len(calls)==1
    second=await docs.normalize(a.id,engine='mathpix',cloud_limit=1)
    assert len(calls)==2 and len(second['blocked_formulas'])==2  # Different second formula.
    await docs.normalize(a.id,engine='mathpix')
    assert len(calls)==2
    async def success(self,image):
        calls.append(image);return {'text':'x=1','confidence':1,'model':'fixture'}
    monkeypatch.setattr(cloud.Mathpix,'recognize',success)
    result=await docs.normalize(a.id,engine='mathpix',retry_cloud=True)
    assert result['processing_progress_pct']==100 and len(calls)==4


async def test_cancel_after_post_keeps_sent_checkpoint(setup,monkeypatch):
    store,docs,a,_,calls=setup
    async def cancelled(self,image):calls.append(image);raise asyncio.CancelledError
    monkeypatch.setattr(cloud.Mathpix,'recognize',cancelled)
    with pytest.raises(asyncio.CancelledError):await docs.normalize(a.id,engine='mathpix',cloud_limit=1)
    job,bid=store.db.execute('SELECT job,block FROM normalization_cloud_v1').fetchone()
    assert json.loads((store.home/'normalization'/job/(bid+'.json')).read_text())['status']=='sent'
    async def success(self,image):
        calls.append(image);return {'text':'x','confidence':1,'model':'fixture'}
    monkeypatch.setattr(cloud.Mathpix,'recognize',success)
    result=await docs.normalize(a.id,engine='mathpix')
    assert result['status']=='needs_action' and len(calls)==2 and result['blocked_formulas']==[bid]


async def test_cloud_checkpoint_tamper_fails_closed(setup):
    store,docs,a,_,_=setup
    first=await docs.normalize(a.id,engine='mathpix',cloud_limit=1)
    record=next((store.home/'normalization'/first['job_id']).glob('*.json'));record.write_text('tampered')
    with pytest.raises(BridgeError,match='checksum'): await docs.normalize(a.id,engine='mathpix')


@pytest.mark.parametrize('limit',[0,21])
async def test_invalid_cloud_limit_is_rejected_before_work(setup,limit):
    _,docs,a,_,calls=setup
    with pytest.raises(BridgeError,match='cloud_limit'):await docs.normalize(a.id,engine='mathpix',cloud_limit=limit)
    assert not calls
