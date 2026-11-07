# Evolutionary Multi-Agent System

HKUST ARIN5202 group project. Supported deployment and assessment environment:
**Ubuntu 22.04 LTS, x86_64, Python 3.10**. Agent execution requires Linux Docker
Engine; native Windows/macOS execution is not a supported assessment environment.

## Development status

The original remote contained only README and LICENSE, despite describing a
working prototype. This branch implements member D's assigned work (C01, C04,
C14, C15, C16). It does not import or claim an unavailable baseline, or implement
members A/B/C's orchestration, persistence, tools, memory and evaluation modules.

## Install and verify

```bash
git clone https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202.git
cd Evolutionary-Multi-Agent-System-ARIN5202
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m evo --help
evo-v3 --version
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

No API keys are required for unit tests. Keep runtime state under ignored `.evo/`.
Use real commit authors and dates; no reconstructed or backdated contribution
history. Team members must review integration changes before merging to main.

## Preview D interfaces

```bash
python -m evo --demo web
```

Open http://127.0.0.1:8768. Preview has tasks, inbox, group chat, private chat and
memory screens. It is **in-memory demo mode**: submitting a task records a blocked
preview and never pretends an agent executed it. Refresh updates state. Close the
server with Ctrl+C. For the CLI preview:

```bash
python -m evo --demo run 'Create hello.md'
# Exits 1 with status=blocked because this is not real execution.
```

## Configure HKGAI on Linux

The instructor-provided base is https://api1.hkgai.net and model is
t2_hkgai-v3_fp8_1m_e7. The completion path and live behavior still need verification.
Enter your key interactively without putting it into source or shell history:

```bash
python -m evo setup-key --provider hkgai
```

This creates ~/.config/evo-collab/hkgai.key with private POSIX permissions and
refuses overwriting an existing key. Alternatively set HKGAI_API_KEY through your
local environment configuration. No real key is included in this repository.
See [provider configuration](docs/PROVIDERS.md).

## Connect the team backend

A/B/C provide a factory accepting state=Path and returning ApplicationGateway:

```bash
export EVO_APPLICATION_FACTORY=your_package.bootstrap:create_application
python -m evo web
python -m evo run 'Create hello.md and verify contents'
python -m evo dm builder 'Summarize your role'
python -m evo group 'Discuss how to divide this task'
python -m evo trace TASK_ID
python -m evo metrics TASK_ID
python -m evo memory builder
python -m evo review TASK_ID accepted
python -m evo review TASK_ID changes_requested --feedback 'Fix the output'
python -m evo revision TASK_ID 'Apply the feedback'
python -m evo answer REQUEST_ID --deny
python -m evo eval DATASET.jsonl --output .evo/report.json --no-memory
```

Without a factory, production commands exit with an actionable error. The Web
server only binds loopback. Use the printed 127.0.0.1 URL and keep the terminal
open. Never expose it publicly without a separate reviewed authentication layer.

## Team handoff

- [Integration contract](docs/INTEGRATION.md) — operations and A/B/C dependencies.
- [Architecture](docs/ARCHITECTURE.md), [workflow](docs/WORKFLOW.md).
- [Requirements matrix](docs/REQUIREMENTS.md), [testing](docs/TESTING.md).
- [Evaluation status](docs/EVALUATION_REPORT.md), [submission evidence](docs/SUBMISSION.md).

D interfaces are independently testable. Full platform acceptance, durable
restart recovery, Docker regressions, official datasets and memory ablation await
team integration. Configured CI is not evidence of a successful CI run.
