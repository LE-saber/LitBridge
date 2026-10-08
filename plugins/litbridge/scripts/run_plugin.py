"""Portable stdio launcher. Dependencies and secrets stay outside the distributed ZIP."""
import logging
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    configured = os.getenv('LITBRIDGE_PYTHON')
    python = Path(configured) if configured else ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        print('LitBridge runtime is missing. Run python scripts/setup_plugin.py first or set LITBRIDGE_PYTHON.', file=sys.stderr)
        return 2
    if Path(sys.executable).resolve() != python.resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve())])
    config = Path(os.getenv('LITBRIDGE_CONFIG', str(ROOT / 'litbridge.local.toml'))).expanduser()
    if not config.is_file():
        print('LitBridge config is missing. Copy examples/litbridge.toml to litbridge.local.toml or set LITBRIDGE_CONFIG.', file=sys.stderr)
        return 2
    # Shared config loader reads the adjacent credential file for both CLI and MCP.
    # API request URLs can contain keys; retain safe application errors, suppress URL logging.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    sys.argv = ['litbridge', '--config', str(config), 'mcp']
    runpy.run_module('litbridge', run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main())
