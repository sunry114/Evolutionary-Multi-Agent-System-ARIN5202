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
