"""Third-party image API contract and comparison, with synthetic credentials/crops only."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import httpx
import pytest
from litbridge import cloud_formula as cloud
from litbridge import model_services as models
from litbridge.config import load_settings
from litbridge.errors import BridgeError, Code
from litbridge.cli import parser
from test_cloud_formula import setup, PNG


def profile(**kwargs):
    return models.ModelProfile(model='test/vision-model',enabled=True,base_url='https://models.example/v1',**kwargs)


def response(text='x=1', **kwargs):
    return {'model':'reported-model','choices':[{'finish_reason':'stop','message':{'content':text}}],
        'usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':14},**kwargs}


async def test_configured_endpoint_crop_prompt_and_auth(monkeypatch):
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret');calls=[]
    def handle(request):
        calls.append(request)
        assert str(request.url)=='https://models.example/v1/chat/completions'
        assert request.headers['authorization']=='Bearer synthetic-secret'
        value=json.loads(request.content)
        assert value['stream'] is False and value['model']=='test/vision-model'
        assert value['chat_template_kwargs']=={'enable_thinking':False}
        content=value['messages'][0]['content']
        assert content[0]=={'type':'text','text':'Formula Recognition:'}
        assert base64.b64decode(content[1]['image_url']['url'].split(',')[1])==PNG
        return httpx.Response(200,json=response('```latex\nx=1\n```'))
    client=models.OpenAIChat('paddle',profile(prompt='Formula Recognition:',extra_body={'chat_template_kwargs':{'enable_thinking':False}}),
        transport=httpx.MockTransport(handle))
    result=await client.recognize(PNG)
    assert result['text']=='x=1' and result['confidence'] is None and result['profile']=='paddle'
    assert result['usage']['total_tokens']==14 and result['latency_seconds']>=0 and len(calls)==1
    assert 'synthetic-secret' not in client.version


@pytest.mark.parametrize('url',['http://remote.example/v1','https://user:secret@example.com/v1',
    'https://example.com/v1?key=secret','https://example.com/v1#secret','https://example.com/../v1',
    'https://example.com/%2e%2e/v1','https://example.com:bad/v1'])
def test_endpoint_validation_never_echoes_urls(url):
    with pytest.raises(BridgeError) as exc:models.endpoint(url,'/chat/completions')
    assert 'secret' not in str(exc.value) and 'remote.example' not in str(exc.value)


def test_loopback_and_base_path_preserved():
    assert models.endpoint('http://127.0.0.1:8000/v1/','/chat/completions')=='http://127.0.0.1:8000/v1/chat/completions'
    assert models.endpoint('https://example.com/gateway/compatible/v1','/chat/completions')=='https://example.com/gateway/compatible/v1/chat/completions'


def test_remote_http_requires_explicit_opt_in():
    with pytest.raises(BridgeError):models.endpoint('http://internal.example/v1','/chat/completions')
    assert models.endpoint('http://internal.example/v1','/chat/completions',allow_insecure_http=True)=='http://internal.example/v1/chat/completions'
    with pytest.raises(ValueError):profile(allow_insecure_http='true')


async def test_opted_in_http_client_sends_once(monkeypatch,tmp_path):
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret')
    config=tmp_path/'models.toml'
    config.write_text('schema_version=1\n[profiles.test]\nenabled=true\n'
        'model="test/vision"\nbase_url="http://internal.example/v1"\n'
        'allow_insecure_http=true\n',encoding='utf-8')
    calls=[]
    def handle(request):
        calls.append(request)
        assert str(request.url)=='http://internal.example/v1/chat/completions'
        assert request.headers['authorization']=='Bearer synthetic-secret'
        return httpx.Response(200,json=response())
    client=models.create(config,'test',transport=httpx.MockTransport(handle))
    assert (await client.recognize(PNG))['text']=='x=1' and len(calls)==1


@pytest.mark.parametrize('text',['$$x_i$$',r'\[x_i\]',r'\(x_i\)','$x_i$','```LaTeX\nx_i\n```'])
def test_only_formula_display_wrappers_removed(text):
    assert models.formula_text(text)=='x_i'
    assert models.formula_text(r'x_i=\varpi')==r'x_i=\varpi'


@pytest.mark.parametrize('value',[{'model':'wrong'}, {'messages':[]}, {'stream':True}, {'tools':[]}, {'foo':float('nan')}])
def test_extra_body_cannot_override_crop_or_enable_tools(value):
    with pytest.raises(ValueError):profile(extra_body=value)


@pytest.mark.parametrize('status,code',[(401,Code.AUTH_REQUIRED),(403,Code.AUTH_REQUIRED),(429,Code.RATE_LIMITED),
    (302,Code.UPSTREAM),(500,Code.UPSTREAM)])
async def test_remote_failures_sanitized_no_retry(monkeypatch,status,code):
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret');calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(status,text='synthetic-secret private text',headers={'location':'https://elsewhere.example'})
    with pytest.raises(BridgeError) as exc:
        await models.OpenAIChat('paddle',profile(),transport=httpx.MockTransport(handle)).recognize(PNG)
    assert exc.value.info.code==code and not exc.value.info.retryable and len(calls)==1
    assert 'synthetic-secret' not in str(exc.value) and 'private text' not in str(exc.value)


@pytest.mark.parametrize('value',[{'choices':[]},response(choices=[{'finish_reason':'length','message':{'content':'x'}}]),
    response(choices=[{'finish_reason':'stop','message':{'content':'x','tool_calls':[{}]}}]),
    response(choices=[{'finish_reason':'stop','message':{'content':[]}}]),response('<think>reasoning</think>x'),
    response('x'*8193),response(usage={'total_tokens':True}),{'error':'synthetic-secret'},response('$$ $$'),
    response('synthetic-secret'),response(model='synthetic-secret')])
async def test_malformed_truncated_or_reasoning_output_rejected(monkeypatch,value):
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret')
    with pytest.raises(BridgeError) as exc:
        await models.OpenAIChat('paddle',profile(),transport=httpx.MockTransport(lambda r:httpx.Response(200,json=value))).recognize(PNG)
    assert exc.value.info.code==Code.INVALID_CONTENT and 'synthetic-secret' not in str(exc.value)


async def test_bounded_response_and_timeout(monkeypatch):
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret')
    with pytest.raises(BridgeError) as exc:
        await models.OpenAIChat('paddle',profile(),transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*262145))).recognize(PNG)
    assert exc.value.info.code==Code.TOO_LARGE
    def timeout(request):raise httpx.ReadTimeout('synthetic-secret',request=request)
    with pytest.raises(BridgeError) as exc:
        await models.OpenAIChat('paddle',profile(),transport=httpx.MockTransport(timeout)).recognize(PNG)
    assert exc.value.info.code==Code.TIMEOUT and 'synthetic-secret' not in str(exc.value)


def write_config(tmp_path, *, enabled=True, qwen_enabled=True):
    path=tmp_path/'models.local.toml'
    path.write_text('schema_version = 1\n'+''.join(
        f'[profiles.{name}]\nenabled = {str(active).lower()}\nmodel = "{name}-model"\nbase_url = "https://models.example/v1"\n'
        for name,active in [('paddle',enabled),('qwen',qwen_enabled),('other',enabled)]),encoding='utf-8')
    return path


def test_config_path_relative_credentials_cli_and_no_secret_info(tmp_path,monkeypatch):
    monkeypatch.delenv('LITBRIDGE_MODEL_API_KEY',raising=False)
    write_config(tmp_path)
    main=tmp_path/'config.toml';main.write_text('model_services = "models.local.toml"\n',encoding='utf-8')
    (tmp_path/'.env.litbridge.toml').write_text('LITBRIDGE_MODEL_API_KEY = "synthetic-secret"\n',encoding='utf-8')
    monkeypatch.chdir(tmp_path.parent)
    settings=load_settings(str(main))
    assert settings.model_services==tmp_path/'models.local.toml'
    assert models.info(settings.model_services)['profiles'][0]['credentials_configured']
    assert 'synthetic-secret' not in json.dumps(models.info(settings.model_services))
    args=parser().parse_args(['normalize','artifact','--engine','compare','--compare-profile','paddle','--compare-profile','qwen'])
    assert args.compare_profiles==['paddle','qwen']


def test_unknown_credential_names_do_not_set_environment(tmp_path,monkeypatch):
    monkeypatch.delenv('LITBRIDGE_MODEL_API_KEY',raising=False)
    main=tmp_path/'config.toml';main.write_text('',encoding='utf-8')
    (tmp_path/'.env.litbridge.toml').write_text('LITBRIDGE_MODEL_API_KEY = "synthetic-secret"\nPATH = "bad"\n',encoding='utf-8')
    with pytest.raises(BridgeError):load_settings(str(main))
    assert 'LITBRIDGE_MODEL_API_KEY' not in __import__('os').environ


def test_preflight_vision_key_disabled_and_fingerprint(monkeypatch):
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret')
    for kwargs in ({'vision':False},{'enabled':False}):
        p=profile().model_copy(update=kwargs)
        with pytest.raises(BridgeError):models.OpenAIChat('paddle',p)
    a=models.OpenAIChat('paddle',profile()).version
    assert models.OpenAIChat('paddle',profile()).version==a
    assert models.OpenAIChat('paddle',profile(prompt='different')).version!=a
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','rotated-secret')
    assert models.OpenAIChat('paddle',profile()).version!=a
    monkeypatch.delenv('LITBRIDGE_MODEL_API_KEY')
    with pytest.raises(BridgeError):models.OpenAIChat('paddle',profile())


@pytest.fixture
def service_setup(setup,tmp_path,monkeypatch):
    store,docs,a,base,_=setup
    docs.model_services=write_config(tmp_path)
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret');calls=[]
    async def recognize(client,image):
        calls.append((client.profile_id,image))
        number=sum(p==client.profile_id for p,_ in calls)
        return {'text':'x=1' if client.profile_id!='qwen' or number==1 else 'x=2',
            'confidence':None,'model':client.profile.model,'profile':client.profile_id,
            'latency_seconds':.5,'usage':{'total_tokens':14}}
    monkeypatch.setattr(models.OpenAIChat,'recognize',recognize)
    return store,docs,a,base,calls


async def test_single_model_can_add_another_profile_without_code_change(service_setup):
    _,docs,a,base,calls=service_setup
    first=await docs.normalize(a.id,engine='model',model_profile='other',cloud_limit=1)
    assert first['engine']=='model' and first['model_profile']=='other' and first['processing_progress_pct']==50
    result=await docs.normalize(a.id,engine='model',model_profile='other')
    assert result['status']=='partial' and result['verified_quality_pct'] is None
    read=await docs.read(result['document_id'])
    assert any(x['source']['model_profile']=='other' and x['source']['confidence'] is None for x in read['locations'])
    assert (await docs.read(base.id))['text']==base.markdown
    await docs.normalize(a.id,engine='model',model_profile='other')
    assert len(calls)==2


async def test_compare_identical_crops_resumes_and_does_not_claim_accuracy(service_setup):
    _,docs,a,base,calls=service_setup
    before=hashlib.sha256(Path(a.path).read_bytes()).hexdigest()
    first=await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'],cloud_limit=1)
    assert first['status']=='in_progress' and first['processing_progress_pct']==50 and len(calls)==2
    result=await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'],cloud_limit=1)
    assert result['status']=='partial' and result['processing_progress_pct']==100 and len(calls)==4
    assert result['candidate_agreement_pct']==50 and result['verified_quality_pct'] is None
    report=json.loads(Path(result['report_json_path']).read_text(encoding='utf-8'))
    assert all(x['same_crop'] for x in report['formulas'])
    assert report['model_statistics']['paddle']['reported_usage']=={'total_tokens':28}
    assert report['model_statistics']['qwen']['recorded_latency_seconds']==1
    assert 'synthetic-secret' not in json.dumps(report) and 'models.example' not in json.dumps(report)
    assert len({x['normalization']['document_id'] for x in result['models']})==2
    assert hashlib.sha256(Path(a.path).read_bytes()).hexdigest()==before
    assert (await docs.read(base.id))['text']==base.markdown
    await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'])
    assert len(calls)==4


async def test_preflight_both_services_before_upload(service_setup):
    _,docs,a,_,calls=service_setup
    write_config(docs.model_services.parent,qwen_enabled=False)
    with pytest.raises(BridgeError):await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'])
    assert not calls
    docs.cloud_formula_ocr=False
    with pytest.raises(BridgeError):await docs.normalize(a.id,engine='model',model_profile='paddle')
    assert not calls


@pytest.mark.parametrize('profiles',[None,[],['paddle'],['paddle','paddle'],['paddle','qwen','other'],[1,'qwen']])
async def test_comparison_requires_exactly_two_profiles(service_setup,profiles):
    _,docs,a,_,calls=service_setup
    with pytest.raises(BridgeError):await docs.normalize(a.id,engine='compare',compare_profiles=profiles)
    assert not calls


async def test_failure_no_implicit_paid_repeat_and_peer_not_discarded(service_setup,monkeypatch):
    _,docs,a,_,calls=service_setup
    original=models.OpenAIChat.recognize
    async def fail(client,image):
        if client.profile_id=='paddle':calls.append(('paddle',image));raise BridgeError(Code.TIMEOUT,'Possibly billed')
        return await original(client,image)
    monkeypatch.setattr(models.OpenAIChat,'recognize',fail)
    result=await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'],cloud_limit=1)
    assert result['status']=='needs_action' and len(calls)==2
    await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'],cloud_limit=1)
    await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'])
    assert len(calls)==4  # Each formula/service at most once; uncertain sent/error not repeated.
    monkeypatch.setattr(models.OpenAIChat,'recognize',original)
    result=await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'],retry_cloud=True)
    assert result['status']=='partial' and len(calls)==6


async def test_local_layout_phase_does_not_double_page_budget(service_setup,monkeypatch):
    _,docs,a,_,calls=service_setup;layouts=[]
    from litbridge import structured
    async def pending(*args,**kwargs):
        layouts.append(kwargs);return {'status':'in_progress','completed_pages':1,'total_pages':2}
    monkeypatch.setattr(structured,'normalize',pending)
    result=await docs.normalize(a.id,engine='compare',compare_profiles=['paddle','qwen'],page_limit=1)
    assert result['phase']=='local_layout' and len(layouts)==1 and not calls


async def test_crop_worker_receives_no_model_credentials(monkeypatch,tmp_path):
    captured={}
    class Process:
        returncode=0
        async def wait(self):return 0
    async def spawn(*args,**kwargs):captured.update(kwargs);return Process()
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret')
    monkeypatch.setenv('LITBRIDGE_MODEL_OTHER_KEY','another-secret')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    await cloud.crop_worker(SimpleNamespace(structured_python=Path(sys.executable)),tmp_path/'source','sha',[],tmp_path)
    assert not any(k.startswith('LITBRIDGE_MODEL_') or k in ('MATHPIX_APP_ID','MATHPIX_APP_KEY') for k in captured['env'])


async def test_local_layout_worker_receives_no_model_credentials(monkeypatch,tmp_path):
    from litbridge import structured
    captured={}
    class Process:
        returncode=0
        async def wait(self):return 0
    async def spawn(*args,**kwargs):captured.update(kwargs);return Process()
    monkeypatch.setenv('LITBRIDGE_MODEL_API_KEY','synthetic-secret')
    monkeypatch.setenv('MATHPIX_APP_KEY','synthetic-mathpix')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    assert await structured.run_worker(Path(sys.executable),tmp_path,tmp_path/'source','sha','job',[1],tmp_path,False) is None
    assert not any(k.startswith('LITBRIDGE_MODEL_') or k in ('MATHPIX_APP_ID','MATHPIX_APP_KEY') for k in captured['env'])


def test_public_model_templates_parse_and_remain_disabled():
    root=Path(__file__).resolve().parents[1]
    example=root/'examples/model-services.toml'
    parsed=models.load_profiles(example)
    assert set(parsed)=={'paddle','qwen'} and all(not p.enabled and not p.base_url and not p.allow_insecure_http for p in parsed.values())
    assert example.read_bytes()==(root/'plugins/litbridge/examples/model-services.toml').read_bytes()
