# Provider configuration (C04)

Only DeepSeek and HKGAI are accepted. Defaults for this course are the supplied
HKGAI base URL `https://api1.hkgai.net` and model `t2_hkgai-v3_fp8_1m_e7`.
The course endpoint's completion path needs a live smoke check; if its API
requires a v1 prefix, select the approved `https://api1.hkgai.net/v1` base URL.
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
Real endpoint compatibility and live API behavior remain unverified.
