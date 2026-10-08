# LitBridge core MCP package 0.2.1

This portable package contains only the generic core and public provider contract. It has no website provider implementations or source catalog.

The core and this generic client wrapper are MIT licensed. Release archives include LICENSE at the package root and server/LICENSE. Independently installed providers and third-party dependencies retain their own licenses.

1. Extract to a dedicated directory.
2. Run `python scripts/setup_plugin.py` to explicitly create/install its local environment.
3. Copy `examples/litbridge.toml` to `litbridge.local.toml`; configure home/profile locally.
4. Run `python scripts/run_plugin.py`, or use mcp.json with the host's plugin installer.

Use LITBRIDGE_PYTHON and LITBRIDGE_CONFIG to select an existing environment/config. Independent providers must be installed into that environment and explicitly enabled. No implicit install occurs in the launcher. Do not copy credentials or private packages into this distribution.

Cloud OCR is disabled by default. Core supports local normalization/read without a source provider for retained originals. See server/docs/PROVIDER_PROTOCOL.md and server/docs/READING_WORKFLOW.md.
