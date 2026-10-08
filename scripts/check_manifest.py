"""Verify published source bytes match the locally tested source snapshot."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'source-manifest.json').read_text(encoding='utf-8'))
failed = []
for name, expected in manifest.items():
    path = root / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        failed.append(name)
if failed:
    raise SystemExit('Source snapshot mismatch: ' + ', '.join(failed))
print(f'Verified {len(manifest)} source/config/test files against the local snapshot')
