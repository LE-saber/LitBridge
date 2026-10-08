import asyncio
import json
import httpx
import pytest
from litbridge.core import Gateway
from litbridge.errors import BridgeError, Code
from litbridge.models import Paper, ProviderInfo, Query, Source, merge_papers, normalize_doi, same_work
from litbridge.providers.base import Provider
from conftest import MetadataFixture, FullTextFixture
from litbridge.storage import Store, validate_content
from conftest import DOI, TITLE, XML, handler


@pytest.mark.parametrize('value', ['10.5555/AB.C', 'https://doi.org/10.5555/AB.C', 'doi:10.5555/AB.C'])
def test_doi_normalization(value):
    assert normalize_doi(value) == '10.5555/ab.c'


def test_identity_conflicts_and_cjk():
    a = Paper(title=TITLE, doi=DOI, year=2024, authors=['Ada Example'])
    b = a.model_copy(update={'doi': '10.5555/other'})
    assert not same_work(a, b)
    with pytest.raises(ValueError):
        merge_papers(a, b)
    c = a.model_copy(update={'doi': None})
    assert same_work(a, c)
    c.title = '\u6587\u732e\u7f51\u5173\u7684\u65b9\u6cd5\u548c\u7ed3\u679c\u53ef\u91cd\u590d\u9a8c\u8bc1\u7814\u7a76\u4e0e\u5b9e\u73b0\u5206\u6790'
    d = c.model_copy(update={'title': c.title + '\u3002'})
    assert same_work(c, d)
    d.year = 2023
    assert not same_work(c, d)


async def test_vertical_search_merge_access_retrieve_read(gateway):
    result = await gateway.search('literature gateway', limit=3)
    assert result['status'] == 'ok' and len(result['papers']) == 1
    p = result['papers'][0]
    assert p['doi'] == DOI and len(p['sources']) == 2
    assert p['abstract'].startswith('A longer')
    access = await gateway.access(p['id'])
    assert access['candidates'][0]['provider'] == 'fulltext'
    assert access['candidates'][0]['verified'] is False
    retrieval = await gateway.retrieve(p['id'])
    assert retrieval['verified'] is True
    artifact = retrieval['artifact']
    assert artifact['format'] == 'xml' and artifact['size'] == len(XML)
    read = gateway.store.read(artifact['id'], max_chars=30)
    assert len(read['text']) == 30 and read['next_offset'] == 30
    rest = gateway.store.read(artifact['id'], offset=30)
    assert 'synthetic full text' in read['text'] + rest['text']
    refs = await gateway.references(p['id'])
    assert {r['provider'] for r in refs['references']} == {'metadata', 'fulltext'}


async def test_cross_source_fulltext_from_metadata_only(gateway):
    p = (await gateway.search('fixture', providers=['metadata']))['papers'][0]
    assert [s['provider'] for s in p['sources']] == ['metadata']
    assert (await gateway.retrieve(p['id']))['artifact']['provider'] == 'fulltext'


class Broken(Provider):
    info = ProviderInfo(id='broken', name='Broken test provider', capabilities=['search'])
    async def search(self, query):
        raise RuntimeError('SECRET=not-for-agent')


async def test_partial_failure_and_circuit(tmp_path):
    good = MetadataFixture(transport=httpx.MockTransport(handler))
    good.http.interval = 0
    g = Gateway([good, Broken()], Store(tmp_path), timeout=0.1)
    try:
        for _ in range(3):
            r = await g.search('fixture')
            assert r['partial'] and len(r['papers']) == 1
            assert 'SECRET' not in json.dumps(r)
        r = await g.search('fixture')
        assert r['errors'][0]['code'] == 'circuit_open'
    finally:
        await g.close()


async def test_timeout_isolated(tmp_path):
    class Slow(Broken):
        async def search(self, query):
            await asyncio.sleep(10)
    g = Gateway([Slow()], Store(tmp_path), timeout=0.01)
    try:
        r = await g.search('fixture')
        assert r['status'] == 'error' and r['errors'][0]['code'] == 'timeout'
    finally:
        await g.close()


async def test_cache_and_native_query_guards(gateway):
    p = gateway.providers['metadata']
    calls = 0
    original = p.search
    async def counted(query):
        nonlocal calls
        calls += 1
        return await original(query)
    p.search = counted
    await gateway.search('same', providers=['metadata'])
    await gateway.search('same', providers=['metadata'])
    assert calls == 1
    with pytest.raises(BridgeError):
        await gateway.search('x AND y', mode='native')
    with pytest.raises(BridgeError):
        await gateway.search('x', providers=['missing'])
    with pytest.raises(BridgeError):
        await gateway.search('x', providers=['metadata'], cursors={'fulltext': '*'})


async def test_retrieve_format_html_and_fallback(gateway):
    p = (await gateway.search('fixture'))['papers'][0]
    provider = gateway.providers['fulltext']
    async def login_page(c):
        return b'<html>Please log in</html>'
    provider.retrieve = login_page
    result = await gateway.retrieve(p['id'])
    assert result['status'] == 'error'
    assert result['errors'][-1]['code'] == 'invalid_content'
    assert list(gateway.store.downloads.iterdir()) == []


def test_store_aliases_expiry_and_restart(tmp_path, monkeypatch):
    s = Store(tmp_path)
    original = Paper(title=TITLE, sources=[Source(provider='x', record_id='1', url='https://x.test/1')])
    saved = s.put(original)
    enriched = saved.model_copy(update={'doi': DOI})
    enriched = s.put(enriched)
    assert s.get(saved.id).id == enriched.id
    assert s.get('x:1').doi == DOI
    s.cache_put('k', {'x': 1}, ttl=10)
    assert s.cache_get('k') == {'x': 1}
    import litbridge.storage
    original_time = litbridge.storage.time.time()
    monkeypatch.setattr(litbridge.storage.time, 'time', lambda: original_time + 20)
    assert s.cache_get('k') is None
    s.close()
    s = Store(tmp_path)
    assert s.get(saved.id).doi == DOI
    s.close()


def test_source_alias_does_not_overwrite_conflicting_doi(tmp_path):
    s = Store(tmp_path)
    a = Paper(title=TITLE, doi=DOI, sources=[Source(provider='x', record_id='1', url='https://x.test')])
    s.put(a)
    b = s.put(a.model_copy(update={'doi': '10.5555/different'}))
    assert s.get('x:1').doi == DOI
    assert s.get(b.id).doi == '10.5555/different'
    s.close()


@pytest.mark.parametrize('payload', [b'<html>login</html>', b'<article><abstract>Not a body</abstract></article>',
    b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><article><body>&x;</body></article>'])
def test_content_rejects_invalid_xml(payload):
    with pytest.raises(BridgeError):
        validate_content(payload, 'xml')


def test_atomic_artifact_validation_and_tamper(tmp_path):
    s = Store(tmp_path)
    a = s.save('paper', 'test', 'xml', 'https://example.org', XML)
    from pathlib import Path
    assert not list(s.downloads.glob('*.part'))
    Path(a.path).write_bytes(XML + b'changed')
    with pytest.raises(BridgeError):
        s.read(a.id)
    with pytest.raises(BridgeError):
        s.read(a.id, max_chars=20001)
    with pytest.raises(BridgeError):
        s.read('../../etc/passwd')
    s.close()


def test_profiles_separate_metadata(tmp_path):
    a, b = Store(tmp_path, 'a'), Store(tmp_path, 'b')
    a.put(Paper(title=TITLE, doi=DOI))
    assert b.get('doi:' + DOI) is None
    a.close(); b.close()


async def test_invalid_plugin_result_is_isolated(tmp_path):
    class Malformed(Broken):
        async def search(self, query):
            return {'invented_response': 'not a SearchPage'}
    good = MetadataFixture(transport=httpx.MockTransport(handler))
    good.http.interval = 0
    g = Gateway([good, Malformed()], Store(tmp_path))
    try:
        result = await g.search('fixture')
        assert len(result['papers']) == 1
        assert result['errors'][0]['code'] == 'upstream_changed'
    finally:
        await g.close()
