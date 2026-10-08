"""Actual CLI processes over durable local artifacts; no network needed."""
import json
import os
from pathlib import Path
import subprocess
import sys
from litbridge.models import Paper, Source
from litbridge.storage import Store
from conftest import XML


def test_cli_batch_read_resume_and_normalize(tmp_path):
    home=tmp_path/'data'
    store=Store(home)
    p=store.put(Paper(doi='10.5555/local-cli',title='CLI fixture',
        sources=[Source(provider='fulltext',record_id='PMC123',url='https://fulltext.example.org/articles/PMC123')]))
    a=store.save(p.id,'fulltext','xml','https://fulltext.example.org/articles/PMC123',XML)
    store.close()
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    def cli(*args):
        result=subprocess.run([sys.executable,'-m','litbridge','--home',str(home),*args],
            cwd=tmp_path,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        assert result.returncode==0,result.stdout+result.stderr
        return json.loads(result.stdout)
    created=cli('job-create',p.id)
    assert created['counts']=={'queued':1}
    job=cli('job-run',created['job_id'])
    assert job['all_success'] and job['items'][0]['reused_original']
    assert cli('job-status',job['job_id'])['items'][0]['artifact']['id']==a.id
    assert cli('read',a.id,'--max-chars','200')['locations']
    assert cli('normalize',a.id,'--force')['normalization']['status']=='ready'
    assert cli('job-run',job['job_id'])['items'][0]['attempts']==1
    assert len(cli('job-history',job['items'][0]['item_id'])['attempts'])==1
