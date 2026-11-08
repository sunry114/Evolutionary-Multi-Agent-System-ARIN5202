# Provider configuration (C04)

Only DeepSeek and HKGAI are accepted. Defaults for this course are the supplied
HKGAI base URL `https://api1.hkgai.net/v1` and model `t2_hkgai-v3_fp8_1m_e7`.
The /v1 base passed real text and JSON smoke checks on 2026-11-08; the bare
domain returned HTTP 404. See API_SMOKE_REPORT.md for evidence.
No real key is required for tests.

Public reference formats:
[DeepSeek](https://api-docs.deepseek.com/api/create-chat-completion/),
[HKGAI Studio](https://hkgai-studio.prod.hkchat.app/en/docs/modelhub).
The Studio endpoint is distinct from the instructor-provided course endpoint.

ProviderConfig accepts provider/model/base_url/timeout/retries. Custom HTTPS URLs
require explicit development mode and are forbidden in evaluation mode. There is
no third-party provider. Transport refuses redirects and ignores proxy environment
variables to avoid accidentally forwarding authorization. Retry bounds: at most
five attempts, only 429, selected transient 5xx and network failures. Malformed,
empty or truncated completions are errors. JSON mode accepts only an object.

Use HKGAI_API_KEY or DEEPSEEK_API_KEY, or Linux setup-key. Private files live at
~/.config/evo-collab/{provider}.key (directory 0700, file 0600, current owner,
regular file, no symlink). Setup refuses existing files; remove the old key
explicitly before rotating. Never paste keys into tracked files, issues or PRs.
Do not use a key disclosed in chat for public demonstrations; ask for replacement.
The course endpoint passed the checks in API_SMOKE_REPORT.md; availability is not guaranteed.


create_provider() reads ProviderConfig.from_environment() and is the shared entry
point for the agent backend. CLI provider-info and check-provider run without an
ApplicationGateway. check-provider requires --live, disables retries, limits the
response to 64 output tokens and omits all model content from its report.

ProviderError.code distinguishes authentication, permission, endpoint, rate_limit,
http_error, connection, tls, invalid_response and invalid_completion. Exceptions
never copy raw provider bodies. Certificate validation failure is not retried;
TLS verification remains enabled. Per-call api_key is available for the temporary
hidden CLI prompt and is not retained on the provider instance.
