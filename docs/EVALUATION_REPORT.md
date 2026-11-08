# Evaluation report — official results pending

Updated: 2026-11-08 (Asia/Shanghai).

No official mid/final dataset has been run. No goal/subtask completion percentage,
end-to-end task execution timing improvement or memory evolution benefit is
claimed. The report remains an evidence-backed status and experiment plan,
not a completed course performance report.

## Evidence available now

| Evidence | Result | What it establishes |
|---|---|---|
| Current repository tests | 38 methods; Windows: 36 passed, 2 POSIX skips | Tested D module behavior under fixtures; see [TESTING](TESTING.md) |
| Ubuntu 22.04/Python 3.10 CI | [PR #2 succeeded](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/actions/runs/42449807107) | Installation, offline tests incl. Linux credentials, compilation and packaging |
| Real course HKGAI connection | Text and JSON checks succeeded on 2026-11-08 | API reachability and expected completion format; see [smoke report](API_SMOKE_REPORT.md) |

The API report's per-request elapsed seconds are not the instructor's total
task-assignment-to-output execution time. No synthetic task completion rate is
substituted for the missing objective evaluation. Review acceptance is separate
from technical and objective assessment status.

## Required experiment record (B/team)

Record commit SHA, dataset SHA-256, Ubuntu environment/architecture,
Python/Docker/Git versions, image digests, CPU/RAM, provider/model/temperature,
exact commands, case order, approval policy and memory policy. Never include keys.

Compare a fresh no-memory run against a predefined memory warm-up/run using the
same model, task order, approvals, hardware and dataset. Save original
machine-generated JSON only if course publication rules allow it. Do not edit raw
outcomes manually.

The final report must include:

- Dataset identity, case count and objective evaluation denominator.
- Overarching goal and subtask completion rates.
- Assignment-to-final-output execution time, summary statistics and units.
- Failure categories with concrete cases.
- Agent memory use and success/failure evolution, with a controlled comparison.
- Environment, immutable code/result references, limitations and reproduction steps.

Timing of the official dataset release must follow the instructor's announcement;
the provided specification contains differing release-window descriptions.
Final report and week 13 presentation are whole-team deliverables.
