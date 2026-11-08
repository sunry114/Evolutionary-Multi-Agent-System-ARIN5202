"""Check tracked text, including staged files, without printing matching secrets."""
import re
import subprocess
from pathlib import Path

PATTERNS = [
    re.compile(rb"sk-[A-Za-z0-9_-]{24,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
ROOT = Path(__file__).resolve().parents[1]


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    violations = []
    for raw in filter(None, paths):
        name = raw.decode("utf-8")
        path = ROOT / name
        if path.is_file() and any(pattern.search(path.read_bytes()) for pattern in PATTERNS):
            violations.append(name)
    if violations:
        print("Potential credential found in: " + ", ".join(violations))
        return 1
    print("Tracked secret-pattern check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
