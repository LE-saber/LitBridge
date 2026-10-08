import httpx
import pytest
from litbridge.errors import BridgeError, Code
from litbridge.transport import HTTP, URLPolicy, public_url, retry_after


@pytest.mark.parametrize('url', [
    'http://api.example.org/a', 'https://api.example.org:444/a', 'https://127.0.0.1/a',
    'https://api.example.org.evil.test/a', 'https://u:p@api.example.org/a',
    'file:///etc/passwd', 'https://api.example.org\\@evil.test', 'https://api.example.org/a\n'])
def test_url_policy(url):
    with pytest.raises(BridgeError):
        URLPolicy({'api.example.org'}).check(url)


async def test_redirect_credentials_never_cross_origin():
    seen = []
    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(302, headers={'location': 'https://other.example.org/steal'})
    h = HTTP(URLPolicy({'api.example.org', 'other.example.org'}), headers={'X-Key': 'secret'},
             transport=httpx.MockTransport(handler), interval=0)
    try:
        with pytest.raises(BridgeError) as e:
            await h.request('GET', 'https://api.example.org/a')
        assert e.value.info.code == Code.UNSAFE_URL and len(seen) == 1
    finally:
        await h.close()


async def test_signed_query_preserved_same_origin_redirect():
    seen = []
    def handler(r):
        seen.append(str(r.url))
        if len(seen) == 1:
            assert r.url.params['signature'] == 'original'
            return httpx.Response(302, headers={'location': '/b?signature=next'})
        assert r.url.params['signature'] == 'next' and r.url.params['apikey'] == 'key'
        return httpx.Response(200, content=b'done')
    h = HTTP(URLPolicy({'api.example.org'}), params={'apikey': 'key'}, transport=httpx.MockTransport(handler), interval=0)
    try:
        assert await h.request('GET', 'https://api.example.org/a?signature=original') == b'done'
    finally:
        await h.close()


@pytest.mark.parametrize(('status', 'code'), [(400, 'invalid_input'), (401, 'auth_required'),
    (403, 'access_denied'), (404, 'not_found'), (429, 'rate_limited'), (500, 'upstream_error')])
async def test_error_taxonomy(status, code):
    h = HTTP(URLPolicy({'api.example.org'}), transport=httpx.MockTransport(
        lambda r: httpx.Response(status, headers={'retry-after': '3600'})), retries=0, interval=0)
    try:
        with pytest.raises(BridgeError) as e:
            await h.request('GET', 'https://api.example.org/a')
        assert e.value.info.code == code
    finally:
        await h.close()


async def test_response_bound_and_json_validation():
    h = HTTP(URLPolicy({'api.example.org'}), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b'x' * 100)), interval=0)
    try:
        with pytest.raises(BridgeError) as e:
            await h.request('GET', 'https://api.example.org/a', max_bytes=10)
        assert e.value.info.code == Code.TOO_LARGE
        with pytest.raises(BridgeError) as e:
            await h.json('GET', 'https://api.example.org/a')
        assert e.value.info.code == Code.UPSTREAM_CHANGED
    finally:
        await h.close()


def test_redaction_and_retry_after():
    url = 'https://records.example.org/a?filename=abc&apiKey=secret&token=secret&signature=secret#secret'
    assert public_url(url) == 'https://records.example.org/a?filename=abc'
    assert retry_after('120') == 120
    assert retry_after('nonsense') == 0


def test_identity_query_routes_require_explicit_registration():
    from litbridge.transport import register_identity_route
    base = 'https://records.example.org/article'
    assert public_url(base + '?v=record-A&token=secret') == base
    register_identity_route('records.example.org','/article',{'v'})
    assert public_url(base + '?v=record-A&token=secret') == base + '?v=record-A'
    assert public_url('https://other.example.org/article?v=secret') == 'https://other.example.org/article'
