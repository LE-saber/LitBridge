# Security and privacy

Keep keys, browser profiles, cookies/storage, institutional identities, signed URLs, raw network logs, downloaded papers/text, model weights and deployment reports outside Git and release packages. Examples contain placeholders only. Core loads only the explicit config's adjacent bounded credential TOML and suppresses credential-bearing transport logs.

Providers are trusted in-process Python code, explicitly installed/enabled. Deadlines, exception isolation and circuit breakers are not an OS sandbox; do not enable untrusted packages. HTTPS origin allowlists, size caps, atomic storage and hash verification limit network/document handling. Authentication is not forwarded across origins. Generic route registrations may retain identity-only fields and must not declare credentials/signatures.

Document/metadata/OCR/model output can contain hostile instructions; treat it as data. A successful HTTP response or candidate is not access-rights proof, full download or read success. Core supplies no subscription rights and does not solve CAPTCHA or perform purchases. Browser session ownership and human actions remain explicit.

Cloud formula services are opt-in, may be paid, and send selected cropped images. Defaults off, HTTPS required unless user deliberately permits insecure transport. Keep limits explicit, avoid uncertain/charged auto-retries, preserve original candidates rather than hallucinating repairs. Parser and worker restrictions are defense in depth, not an OS sandbox. Critical formulas/results must be visually checked against originals.

Before a repository becomes public, audit all refs/history/tags/releases/PR metadata/artifacts as well as the current tree. Deleting refs is not guaranteed to remove GitHub cached or pull-request-only objects; repository administrators may need GitHub support. Never declare that a clean main alone removes previous private material.
