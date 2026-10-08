import json
from pathlib import Path
import subprocess
import sys
import os
import pytest
from litbridge.config import Settings, build_gateway, load_settings
from litbridge.models import ProviderInfo
from litbridge.providers.base import Provider

async def test_core_has_no_implicit_sources(tmp_path):
    g=build_gateway(Settings(home=tmp_path))
    assert g.providers == {}
    result=await g.search('synthetic')
    assert result['papers']==[]
    await g.close()

async def test_installed_disabled_package_is_never_loaded(tmp_path,monkeypatch):
    class Entry:
        name='example'
        def load(self):raise AssertionError('Disabled implementation executed')
    monkeypatch.setattr('litbridge.config.entry_points',lambda group:[Entry()])
    g=build_gateway(Settings(home=tmp_path));assert not g.providers;await g.close()

async def test_protocol_mismatch_isolated_and_context_data_root(tmp_path,monkeypatch):
    class Entry:
        def __init__(self,name,protocol):self.name,self.protocol=name,protocol
        def load(self):
            def factory(context):
                assert context.home.is_relative_to(tmp_path)
                p=Provider();p.info=ProviderInfo(id=self.name,name=self.name,protocol=self.protocol,capabilities=[])
                return p
            return factory
    monkeypatch.setattr('litbridge.config.entry_points',lambda group:[Entry('good','1.0'),Entry('bad','2.0')])
    g=build_gateway(Settings(home=tmp_path,enabled_plugins=['good','bad']))
    assert g.providers['bad'].info.state=='incompatible'
    assert g.providers['good'].info.protocol=='1.0'
    await g.close()

def test_duplicate_and_unknown_ids_fail(tmp_path):
    with pytest.raises(Exception):build_gateway(Settings(home=tmp_path,enabled_plugins=['same','same']))
    with pytest.raises(Exception):build_gateway(Settings(home=tmp_path,default_providers=['unknown']))

def test_generic_credentials_and_invalid_config(tmp_path,monkeypatch):
    monkeypatch.delenv('EXAMPLE_API_KEY',raising=False)
    path=tmp_path/'config.toml';path.write_text('profile="test"\n')
    (tmp_path/'.env.litbridge.toml').write_text('EXAMPLE_API_KEY="synthetic"\n')
    load_settings(str(path));assert os.environ['EXAMPLE_API_KEY']=='synthetic'
    path.write_text('unrecognized_field=true\n')
    with pytest.raises(Exception):load_settings(str(path))

