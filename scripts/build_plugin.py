"""Build an allowlisted, self-contained local plugin; exclude machine data and credentials."""
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
template = ROOT / 'plugins/litbridge'
target = ROOT / 'dist/litbridge'
target.mkdir(parents=True, exist_ok=True)
files = [template / name for name in (
    'plugin.json', 'mcp.json', '.codex-plugin/plugin.json', 'README.md',
    'scripts/run_plugin.py', 'scripts/setup_plugin.py', 'examples/litbridge.toml',
    'examples/model-services.toml', 'examples/model-credentials.toml',
    'skills/literature-workflow/SKILL.md')]
for source in files:
    out = target / source.relative_to(template)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, out)
for name in ('pyproject.toml', 'README.md', 'README.zh-CN.md', 'LICENSE'):
    out = target / 'server' / name
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / name, out)
shutil.copyfile(ROOT / 'LICENSE', target / 'LICENSE')
server_files = []
for name in ('docs/PROVIDER_PROTOCOL.md','docs/READING_WORKFLOW.md','docs/SECURITY.md',
             'docs/NORMALIZATION.md','docs/MODEL_SERVICES.md','scripts/prefetch_normalization.py'):
    out = target / 'server' / name
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / name, out)
    server_files.append(out)
for source in (ROOT / 'src').rglob('*.py'):
    out = target / 'server' / source.relative_to(ROOT)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, out)
manifest = json.loads((target / 'plugin.json').read_text(encoding='utf-8'))
overlay = json.loads((target / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
assert (manifest['name'], manifest['version']) == (overlay['name'], overlay['version'])
assert len(manifest['extensions']['com.openai']['interface']['shortDescription']) <= 30
archive = ROOT / 'dist/litbridge-core-plugin.zip'
# Archive only this build's enumerated inputs, never stale files from a previous build.
packaged = [target / p.relative_to(template) for p in files]
packaged += [target / 'LICENSE']
packaged += [target / 'server' / n for n in ('pyproject.toml', 'README.md', 'README.zh-CN.md', 'LICENSE')]
packaged += server_files
packaged += [target / 'server' / p.relative_to(ROOT) for p in (ROOT / 'src').rglob('*.py')]
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
    for p in packaged:
        output.write(p, 'litbridge/' + p.relative_to(target).as_posix())
print(archive)
