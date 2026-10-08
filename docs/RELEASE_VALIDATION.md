# Release validation — 0.2.0

2026-10-08, Python3.13/Windows: 173 core tests passed. Includes durable queue restart/reuse, source output isolation/identity/cache, bounded document/parser/cloud fixtures, configuration opt-in, official MCP SDK16-tool stdio integration and unpacked client-package startup with zero default sources. No live source entitlement or cloud OCR call is claimed.

Candidate source, wheel and generic client ZIP are audited for unintended source-specific content, private runtime data and known local credentials/service endpoints. The wheel contains only litbridge modules and distribution metadata. The generic client package contains the public contract/base class but no source implementation. Source hashes are LF-normalized and checked by scripts/check_manifest.py; CI repeats on Linux3.11/Linux3.13/Windows3.12.

The release is based on a core-only commit lineage. Repository-wide publication also requires every branch/tag/PR/release/artifact to satisfy this boundary; a clean current tree is insufficient. Processing/read success is not formula, table or scientific accuracy. License/PyPI publication are not implied by this release.
