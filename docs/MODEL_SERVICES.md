# Third-party image services

Copy examples/model-services.toml to the ignored models.local.toml, set its path in model_services, and fill deployment model IDs/base endpoints locally. Supported service transport is OpenAI-compatible chat/image content with a formula-recognition contract; a different API needs its own adapter. Model names alone do not establish endpoint compatibility.

Credentials are environment variables named by each profile's api_key_env, or in the explicit main config's adjacent `.env.litbridge.toml`. Core accepts bounded LITBRIDGE_MODEL_* keys and generic *_API_KEY/*_INSTTOKEN string keys plus Mathpix app ID/key. Never insert credential values in tracked templates, endpoints or logs. Process values are not echoed by models/doctor.

Run CLI models or MCP doctor(live=false) to inspect readiness without uploading. Global cloud_formula_ocr and the chosen profile's enabled flag must be explicit true. HTTPS is required by default; allow_insecure_http is an explicit risk opt-in and never inferred. Paid requests may transmit selected formula crops; no whole-paper uploads. cloud_limit bounds each service per call. Sent/error checkpoints do not auto-retry; retry_cloud/force can incur extra usage.

normalize(engine="model",model_profile="PROFILE") retains one result; compare_profiles selects two independent candidates with identical crop SHA. Inspect derived document IDs and local comparison reports. Agreement percentages are not accuracy percentages. Default profiles/templates disabled. Mathpix is a separate optional paid service; the release does not supply accounts, credits or credentials.
