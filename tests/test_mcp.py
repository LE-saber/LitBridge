"""Optional official SDK stdio integration, not a home-grown JSON-RPC substitute."""
import json
import os
from pathlib import Path
import sys
import pytest

pytestmark = pytest.mark.mcp


async def test_official_sdk_initialize_list_and_call(tmp_path):
    pytest.importorskip('mcp')
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from litbridge.storage import Store
    from conftest import XML
    seed = Store(tmp_path)
    artifact = seed.save('fixture', 'fixture', 'xml', 'https://example.org', XML)
    seed.close()
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'litbridge', '--home', str(tmp_path), 'mcp'],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {'providers', 'search', 'resolve', 'access', 'retrieve',
                                                    'read', 'references', 'import_url', 'doctor', 'batch', 'job_create',
                                                    'job_run', 'job_status', 'job_history', 'human_run', 'normalize'}
            result = await session.call_tool('providers', {})
            assert not result.isError
            data = json.loads(result.content[0].text)
            assert len(data['providers']) == 0
            normalized = await session.call_tool('read', {'artifact_id': artifact.id, 'max_chars': 500})
            assert not normalized.isError
            assert json.loads(normalized.content[0].text)['locations']
            created = await session.call_tool('job_create', {'identifiers': ['10.5555/fixture']})
            job_id = json.loads(created.content[0].text)['job_id']
            job = await session.call_tool('job_status', {'job_id': job_id})
            assert json.loads(job.content[0].text)['counts'] == {'queued': 1}
            error = await session.call_tool('read', {'artifact_id': '../../etc/passwd'})
            assert error.isError
            assert json.loads(error.content[0].text)['error']['code'] == 'not_found'
