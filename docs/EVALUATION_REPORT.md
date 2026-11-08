# Evaluation report — pending integration and official dataset

No official mid/final case has been run. No completion rate, timing improvement,
or memory evolution benefit is claimed.

## Verified interface behavior
Offline tests cover provider failures, credential boundaries, CLI dispatch and
exit codes, HTTP write protections, demo blocking and review/revision transitions.
These are software checks, not an agent capability evaluation.

## Required experiment record (B/team)
Record commit SHA, dataset SHA-256, Ubuntu 22.04 x86_64 environment, Python/Docker/
Git versions, image digests, CPU/RAM, provider/model/temperature, exact commands,
case order, approval policy and memory policy. Never include credentials.

Run fresh no-memory and a predefined memory warm-up comparison with the same
model, task order, approvals, hardware and dataset. Save raw machine-generated
JSON only if the course permits publishing it. Do not manually alter results.

Report objective case count and denominator, objective/goal/subtask completion,
total/mean/p50/p95 time, failure categories, memory recalled/used, plus failures
and limitations. Keep self-reported outcomes separate from objective evidence.
Human acceptance must never overwrite an objective failure.
