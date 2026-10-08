"""Refresh LF hashes for allowlisted distribution/source inputs."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
names=['LICENSE','README.md','README.zh-CN.md','AGENTS.md','CHANGELOG.md','pyproject.toml','.gitignore','.gitattributes','.github/workflows/ci.yml']
for folder in ('src','tests','scripts','docs','examples','plugins'):
    names += [p.relative_to(root).as_posix() for p in (root/folder).rglob('*')
        if p.is_file() and (p.suffix in ('.py','.md','.json','.toml') or p.name == 'LICENSE')
        and not any(part in ('__pycache__','build','dist','.venv') or part.endswith('.egg-info') for part in p.parts)
        and not p.name.startswith('.env')]
manifest={}
for name in sorted(set(names)):
    p=root/name
    if p.is_symlink():raise SystemExit('Linked distribution input: '+name)
    raw=p.read_bytes().replace(b'\r\n',b'\n');p.write_bytes(raw)
    manifest[name]=hashlib.sha256(raw).hexdigest()
(root/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
print('Updated',len(manifest),'core-only source hashes')
