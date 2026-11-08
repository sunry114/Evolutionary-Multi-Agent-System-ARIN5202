# Course HKGAI connection verification

Date: 2026-11-08 (Asia/Shanghai).
Environment: Windows, Python 3.12.7, local development connectivity check.
Model: t2_hkgai-v3_fp8_1m_e7.

| Request | Result | Elapsed seconds |
|---|---|---:|
| POST https://api1.hkgai.net/chat/completions | HTTP 404; incorrect path | 0.5717 |
| POST https://api1.hkgai.net/v1/chat/completions, fixed text prompt | success, expected OK | 0.3957 |
| Same endpoint, response_format=json_object, fixed JSON prompt | success, expected object | 0.5690 |

Each diagnostic used zero retries and a 64-token output limit. Prompts are fixed
in adapters/llm/diagnostics.py. No agent tools, user files, or project data were
sent. Credentials were used in process memory and were not committed. No provider
response bodies, authorization headers or key material are included here.

These are individual connection timings, not task execution benchmarks. They do
not establish completion rates, memory evolution, Docker isolation or official
mid/final evaluation results. This Windows check does not replace Linux system
acceptance. Provider service availability and keys may change after this run.

The verified course base URL is now the HKGAI default:
https://api1.hkgai.net/v1. No automatic endpoint fallback is performed.

Reproduce on the supported Linux environment:
```bash
python -m evo provider-info
python -m evo check-provider --live --ask-key
python -m evo check-provider --live --json-object --ask-key
```

--ask-key reads a temporary credential without saving it. An existing private
credential file or HKGAI_API_KEY may be used by omitting --ask-key. Do not paste
keys into shell command arguments, screenshots, PRs or test fixtures. Real checks
are opt-in and are never part of ordinary CI.


Implementation reference: [6cb3780](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/commit/6cb3780),
merged through [PR #2](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/pull/2).
For current delivery status and remaining acceptance items, see
[SUBMISSION.md](SUBMISSION.md).
