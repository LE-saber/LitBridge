# LitBridge

**English** | [简体中文](README.zh-CN.md)

**Local-first literature workflows and document normalization for AI agents.**

LitBridge preserves search results, provenance and original documents, manages resumable retrieval jobs, and converts documents into Markdown with page or node references for reading through CLI or MCP. All network sources connect through the open Provider protocol. The core ships without literature website implementations, source catalogs, login recipes or source credentials.

## Features

- Provider protocol 1.0 with capability declarations and explicit opt-in; federated results, DOI/source identity, caching, timeout isolation and circuit breakers.
- Durable job queues, leases, failure classification, human-assisted recovery and reuse of existing originals. Retrieval, format validation, normalization and reading report separate results.
- Bounded PDF/XML storage with SHA-256 verification; normalized Markdown and structured blocks with page/node references. Explicit local HTML imports can also be normalized.
- Optional offline layout/OCR, page checkpoints and cancellation recovery; optional cloud formula recognition and configurable image-model comparison. Cloud processing is disabled by default.
- A shared service layer for the CLI and 16 MCP tools. Generic browser tools operate only on controlled targets supplied by a Provider and include no website rules.

## Installation

Requires Python 3.11+. Download a wheel from [Releases](https://github.com/LE-saber/LitBridge/releases), or install from the source directory. The following commands use Windows PowerShell:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[mcp]"
Copy-Item examples/litbridge.toml litbridge.local.toml
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml providers
```

The provider list is empty until you install and enable a Provider. To try the neutral local catalog example for the open protocol:

```powershell
.venv/Scripts/python.exe -m pip install --no-deps -e examples/localcatalog
```

Save metadata you own as a JSON array, such as `[{"title":"Synthetic study","doi":"10.5555/example","year":2024}]`, then add these settings to your local configuration:

```toml
enabled_plugins = ["localcatalog"]
[plugin_options.localcatalog]
path = "D:/YOUR_PATH/catalog.json"
```

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml search "Synthetic" --provider localcatalog
```

The local catalog example supplies metadata only. Its sample DOI is not guaranteed to exist online, and it does not provide full text. Install other sources independently and explicitly add their IDs to `enabled_plugins`; the core has no dependency on their repositories or implementations.

## Usage and MCP

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml doctor
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml mcp
```

MCP uses stdio; see the [configuration example](examples/mcp.json). Tools include `providers`, `search`, `resolve`, `access`, `retrieve`, `references`, `import_url`, `doctor`, `batch`, `job_create`, `job_run`, `job_status`, `job_history`, `human_run`, `normalize` and `read`. Inspect provider capabilities, select and retrieve a small set of targets, then check normalization and reading after retaining the originals. See the [reading workflow](docs/READING_WORKFLOW.md) for details.

The release asset `litbridge-core-plugin.zip` is a generic Codex/MCP client wrapper containing the core service without literature website implementations. Extract it, follow its dependency setup instructions and provide your local configuration. The wheel suits an existing Python environment; the source ZIP is intended for development. This version has not been published to PyPI, so do not assume `pip install litbridge` will install it.

## Normalization and models

Lightweight parsing requires no large model. Complex layouts can use a separate offline runtime. Scanned pages, two-column reading order, tables and formulas may need review; `ready` status and processing progress do not measure accuracy. Documents with unknown opening passwords are skipped, and originals are preserved.

Connect image models through explicit local configuration, keeping credentials in the process environment or ignored files. HTTPS is required by default. External processing is limited to selected formula crops when explicitly enabled; agreement between models is not accuracy. See [model configuration](docs/MODEL_SERVICES.md) and [normalization](docs/NORMALIZATION.md).

## Security and boundaries

Treat literature, web pages and model output as untrusted data. Providers are trusted Python packages and are not isolated by a process sandbox. The core does not grant subscription access, solve CAPTCHAs or log in for you. API rights, browser rights, successful retrieval and readability require separate verification. Core network tools enforce HTTPS, target domains, size limits and credential handling across redirects.

Do not commit keys, institutional sessions, browser profiles, downloaded papers, full text, model weights or local reports. Cloud enhancement may send selected images and incur charges; review configuration and limits first. See [security and privacy](docs/SECURITY.md).

## Development

```powershell
.venv/Scripts/python.exe -m pip install -e ".[dev,mcp,browser]"
.venv/Scripts/python.exe -m pytest -m "not browser and not live" -q
.venv/Scripts/python.exe scripts/update_manifest.py
.venv/Scripts/python.exe scripts/check_manifest.py
.venv/Scripts/python.exe scripts/build_plugin.py
```

Public API: [Provider protocol](docs/PROVIDER_PROTOCOL.md). Versions: [changelog](CHANGELOG.md). CI verifies the core, official MCP SDK and distribution packages in independent environments without accessing institutional sessions. Synthetic tests do not replace network/browser acceptance checks.

Current version: 0.2.1. Provider protocol: 1.0. Source-specific fields from older configurations belong in the corresponding independent Provider options; the core starts with zero sources.

## License

All project code and documentation in this repository use the [MIT License](LICENSE), including the core, open Provider protocol, CLI/MCP, generic client wrapper, normalization/model interfaces, examples and tests. Independently installed Provider packages use their own licenses; this repository's license does not grant rights to them. Third-party dependencies and model weights retain their own licenses, and original literature remains subject to its rights holders' copyright.
