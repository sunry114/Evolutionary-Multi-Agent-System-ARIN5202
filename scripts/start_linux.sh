#!/usr/bin/env bash
# Run from the native Linux source directory, with the existing private key.
set -eu
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
export PATH="$project_dir/.venv/bin:$HOME/.local/bin:$PATH"
if [ "$(uname -s)" != Linux ] || [ "$(id -u)" = 0 ]; then
  echo "Start this workbench as a non-root Linux user." >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Create .venv and install the project first; see docs/LOCAL_LINUX_SETUP.md." >&2
  exit 1
fi
exec .venv/bin/python -m evo --state "$project_dir/.evo/local-workbench" web --port "${1:-8769}"
