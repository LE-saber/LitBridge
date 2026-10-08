"""Durability, partial failure, safe errors, concurrency and restart tests."""
import asyncio
import json
from pathlib import Path
import time
import pytest
from litbridge.core import Gateway
from litbridge.errors import BridgeError, Code
from litbridge.models import Paper, Source, Candidate, ProviderInfo
from litbridge.providers.base import Provider
from litbridge.storage import Store
from conftest import XML


class Fixture(Provider):
    info=ProviderInfo(id='fixture',name='fixture',capabilities=['access','retrieve'])
    def __init__(self):
        self.calls=0
    async def access(self,p):
        return [Candidate(provider='fixture',url='https://example.org/'+p.doi.rsplit('/',1)[-1],format='xml',access='unknown',evidence='synthetic')]
    async def retrieve(self,c):
        self.calls+=1
        key=c.url.rsplit('/',1)[-1]
        if key=='human':
            raise BridgeError(Code.HUMAN_REQUIRED,'Synthetic manual verification')
        if key=='denied':
            raise BridgeError(Code.ACCESS_DENIED,'Synthetic denial')
        if key=='retry':
            raise BridgeError(Code.NETWORK,'Synthetic temporary failure',retryable=True)
        if key=='broken':
            raise RuntimeError('API_SECRET_MUST_NOT_APPEAR')
        return XML


def make(tmp_path):
    g=Gateway([Fixture()],Store(tmp_path))
    for key in ['ok','human','denied','retry','broken']:
        g.store.put(Paper(doi='10.5555/'+key,title='Synthetic paper '+key,
            sources=[Source(provider='fixture',record_id=key,url='https://example.org/'+key+'?token=SHOULD_NOT_PERSIST')]))
    return g


async def test_batch_mixed_failures_do_not_stop_other_papers(tmp_path):
    g=make(tmp_path)
    try:
        result=await g.batch(['10.5555/'+k for k in ('human','denied','retry','broken','ok')])
        assert result['counts']=={'human_required':1,'access_denied':1,'retryable':1,'provider_error':1,'success':1}
        assert not result['all_success']
        last=result['items'][-1]
        assert last['artifact'] and last['normalization']['status']=='ready'
        encoded=json.dumps(result)
        assert 'API_SECRET' not in encoded and 'SHOULD_NOT_PERSIST' not in encoded
        assert all(i['entries'] and i['next_action'] for i in result['items'])
        job=result['job_id']
    finally:
        await g.close()
    g=make(tmp_path)
    try:
        restarted=await g.job_status(job)
        assert restarted['counts']==result['counts']
        history=await g.job_history(last['item_id'])
        assert history['attempts'][0]['state']=='success'
        repeated=await g.job_run(job)
        assert repeated['items'][-1]['artifact']==last['artifact']
        assert repeated['items'][-1]['attempts']==1
        assert g.providers['fixture'].calls==1  # Only the retryable item ran again.
    finally:
        await g.close()


async def test_dedup_pagination_and_no_download_on_create(tmp_path):
    g=make(tmp_path)
    try:
        job=await g.job_create(['10.5555/ok','https://doi.org/10.5555/OK','10.5555/human'])
        assert job['counts']=={'queued':2} and g.providers['fixture'].calls==0
        page=await g.job_status(job['job_id'],limit=1)
        assert page['next_offset']==1
        second=await g.job_status(job['job_id'],offset=1,limit=1)
        assert second['next_offset'] is None and second['items'][0]['item_id']!=page['items'][0]['item_id']
        done=await g.job_run(job['job_id'],limit=1)
        assert done['counts']=={'queued':1,'success':1}
    finally:
        await g.close()


async def test_claim_fencing_and_crash_recovery(tmp_path):
    a,b=make(tmp_path),make(tmp_path)
    try:
        job=await a.job_create(['10.5555/ok']); item=job['items'][0]['item_id']
        token,data=a.ledger.claim(item,['queued'])
        assert b.ledger.claim(item,['queued']) is None
        with a.store.db:
            a.store.db.execute('UPDATE retrieval_items_v1 SET lease=? WHERE id=?',(time.time()-1,item))
        b.ledger.recover(job['job_id'])
        fresh=b.ledger.claim(item,['retryable'])
        assert fresh and fresh[0]!=token
        with pytest.raises(BridgeError):
            a.ledger.finish(item,token,'provider_error',data)
        b.ledger.finish(item,fresh[0],'retryable',fresh[1])
        done=await a.job_run(job['job_id'])
        assert done['all_success']
        hist=await a.job_history(item)
        assert any(x.get('reason')=='worker_lease_expired' for x in hist['attempts'])
    finally:
        await a.close(); await b.close()


async def test_saved_original_reused_after_interrupted_job(tmp_path):
    g=make(tmp_path)
    try:
        job=await g.job_create(['10.5555/ok']); item=job['items'][0]['item_id']
        g.ledger.claim(item,['queued'])
        p=g._stored('10.5555/ok')
        a=g.store.save(p.id,'fixture','xml','https://example.org/ok',XML)
        with g.store.db:
            g.store.db.execute('UPDATE retrieval_items_v1 SET lease=0 WHERE id=?',(item,))
        result=await g.job_run(job['job_id'])
        assert result['all_success'] and result['items'][0]['artifact']['id']==a.id
        assert result['items'][0]['reused_original'] and g.providers['fixture'].calls==0
    finally:
        await g.close()


async def test_normalization_failure_does_not_lose_successful_acquisition(tmp_path,monkeypatch):
    g=make(tmp_path)
    try:
        async def failed(*a,**k):
            raise BridgeError(Code.INVALID_CONTENT,'Synthetic parsing failure')
        monkeypatch.setattr(g.documents,'normalize',failed)
        result=await g.batch(['10.5555/ok'])
        item=result['items'][0]
        assert item['state']=='success' and item['normalization']['status']=='error'
        assert Path(item['artifact']['path']).is_file()
    finally:
        await g.close()


async def test_cancellation_releases_item_for_retry(tmp_path,monkeypatch):
    g=make(tmp_path)
    try:
        entered=asyncio.Event()
        async def stall(c):
            entered.set(); await asyncio.sleep(60)
        monkeypatch.setattr(g.providers['fixture'],'retrieve',stall)
        job=await g.job_create(['10.5555/ok'])
        task=asyncio.create_task(g.job_run(job['job_id']))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        status=await g.job_status(job['job_id'])
        assert status['counts']=={'retryable':1}
    finally:
        await g.close()


@pytest.mark.parametrize('values',[[],[''],['x']*101,[None]])
async def test_invalid_job_rejected(tmp_path,values):
    g=make(tmp_path)
    try:
        with pytest.raises(BridgeError):
            await g.job_create(values)
    finally:
        await g.close()


async def test_stale_worker_does_not_abort_later_items(tmp_path,monkeypatch):
    g=make(tmp_path)
    g.store.put(Paper(doi='10.5555/second',title='Second synthetic paper',
        sources=[Source(provider='fixture',record_id='second',url='https://example.org/second')]))
    try:
        finish=g.ledger.finish
        calls=0
        def lost_once(item_id,token,state,data):
            nonlocal calls
            calls+=1
            if calls==1:
                with g.store.db:
                    g.store.db.execute('UPDATE retrieval_items_v1 SET lease=0 WHERE id=?',(item_id,))
            return finish(item_id,token,state,data)
        monkeypatch.setattr(g.ledger,'finish',lost_once)
        job=await g.batch(['10.5555/ok','10.5555/second'])
        assert job['items'][1]['state']=='success'
        job=await g.job_run(job['job_id'])
        assert job['all_success'] and job['items'][0]['reused_original']
    finally:
        await g.close()
