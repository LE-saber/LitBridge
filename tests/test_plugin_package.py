"""Unpacked core distribution must run with no website implementation or provider."""
import json,os,subprocess,sys,zipfile
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]

async def test_core_package_and_sdk_startup(tmp_path):
    pytest.importorskip('mcp')
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    subprocess.run([sys.executable,str(ROOT/'scripts/build_plugin.py')],check=True,capture_output=True)
    with zipfile.ZipFile(ROOT/'dist/litbridge-core-plugin.zip') as z:
        names=z.namelist()
        assert 'litbridge/server/docs/PROVIDER_PROTOCOL.md' in names
        assert z.read('litbridge/LICENSE') == (ROOT/'LICENSE').read_bytes()
        assert z.read('litbridge/server/LICENSE') == (ROOT/'LICENSE').read_bytes()
        assert all(n.startswith('litbridge/') for n in names)
        source=[n for n in names if n.startswith('litbridge/server/src/')]
        assert all(n.startswith('litbridge/server/src/litbridge/') for n in source)
        assert set(n for n in source if '/providers/' in n)=={
            'litbridge/server/src/litbridge/providers/__init__.py',
            'litbridge/server/src/litbridge/providers/base.py'}
        assert not any(any(p in n for p in ('.env','.venv','browser-profile','deployment','.sqlite','.pdf')) for n in names)
        z.extractall(tmp_path/'package')
    plugin=tmp_path/'package/litbridge'
    config=tmp_path/'config.toml';config.write_text('home='+json.dumps(str(tmp_path/'data'))+'\n')
    params=StdioServerParameters(command=sys.executable,args=[str(plugin/'scripts/run_plugin.py')],
        env={**os.environ,'LITBRIDGE_PYTHON':sys.executable,'LITBRIDGE_CONFIG':str(config),
             'PYTHONPATH':str(plugin/'server/src')})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();assert len((await session.list_tools()).tools)==16
            result=await session.call_tool('providers',{})
            assert not result.isError and json.loads(result.content[0].text)['providers']==[]
            result=await session.call_tool('doctor',{'live':False})
            assert not result.isError
