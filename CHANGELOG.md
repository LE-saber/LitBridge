# Changelog

## 0.2.1 — 2026-10-08

- All project code and documentation in this repository are now MIT licensed, including the core, public Provider protocol, CLI/MCP, generic client wrapper, normalization/model interfaces, examples and tests.
- Include the MIT notice in the wheel, source archive and generic client package; declare the SPDX license expression in Python package metadata and verify license inputs in the source manifest.
- Align the neutral localcatalog example's dependency with core 0.2.x. Provider protocol remains 1.0; independent provider packages and third-party dependencies retain their own licenses.

## 0.2.0 — 2026-10-08

- Complete generic workflow core delivered on main: capability registry, metadata federation/identity, durable retrieval tasks, bounded original storage, canonical normalize/read, CLI and16 MCP tools.
- All website adapters removed from core distribution and initialization. Source plugins install independently and require explicit enabled_plugins. Core config rejects former source-specific top-level fields; those move into provider options.
- Open Provider protocol1.0 retained, additive optional PluginContext.home supplies the profile data root. Browser selected-response capture requires explicit matching rules; exact identity query routes are declared by trusted enabled packages.
- Optional offline structure/OCR and explicit cloud formula/model comparison preserved. Public templates disabled; original papers, runtime data and credentials excluded.
- Standalone core wheel, source archive and generic client wrapper. Protocol and generic example public; no website recipes shipped.

This release changes installation/configuration boundaries. It does not claim new website acceptance, institution entitlements, full formula accuracy, PyPI publication or an open-source license grant.
