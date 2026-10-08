"""Explicit dependency setup; never installs packages during an MCP request."""
from pathlib import Path
import os
import subprocess
import sys
import venv

root = Path(__file__).resolve().parents[1]
venv.EnvBuilder(with_pip=True).create(root / '.venv')
python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
subprocess.run([str(python), '-m', 'pip', 'install', '-e', str(root / 'server') + '[mcp,browser]'], check=True)
print('Runtime ready. Copy examples/litbridge.toml to litbridge.local.toml; keep credentials local.')
