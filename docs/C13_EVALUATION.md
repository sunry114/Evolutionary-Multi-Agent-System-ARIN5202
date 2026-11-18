# C13 objective evaluation

`evo.evaluation.dataset.load_dataset` reads a nonempty UTF-8 JSONL file. Each
case needs a unique `id` and a `prompt`. An optional `expected` list makes the
case objective; without it, the case is explicitly `self_reported`.

Supported check kinds:

| Kind | Fields | Observation |
|---|---|---|
| `result_contains` | `text` | Text in the saved task/subtask result |
| `file_exists` | `agent`, `path`, optional `purpose` | Fresh file in that agent's workspace |
| `file_contains` / `file_exact` | File fields plus `text` | Fresh file and matching content |
| `test` | `name` | A named, trusted callback registered by the evaluator |

Dataset files cannot provide a shell command or Python body. Paths must be
relative and satisfy the existing workspace policy. A case may also supply
`subtask_checks` keyed by `s1`–`s5`; these are the trusted C11 artifact checks
needed to mark planned subtasks successful. They should be authored with the
dataset, not by the agent being evaluated. See [`examples/smoke.jsonl`](../examples/smoke.jsonl).

`evaluate_jsonl(dataset_path, output_path, application_factory,
memory_enabled=True, approval_policy="manual")` validates and writes a JSON
report in one call. Its `EvaluationRunner` creates a fresh temporary state directory for each
case and passes it to the caller's C11 application factory. The runner captures
expected files before execution. A preexisting unchanged file cannot pass;
neither can a file from a failed or blocked run. The case directory is removed
after its observations are recorded. The factory must bind its store and
workspaces to the supplied directory.

The report records each case's basis, technical status, check outcomes, failure
category, completed and total subtasks, and monotonic elapsed seconds. Summary
rates distinguish all-case goal completion from objective completion, whose
denominator is `objective_count`. `dataset_sha256`, memory mode and approval
policy are recorded. `write_report` checks the report and writes JSON atomically;
`verify_report` recalculates aggregates and checks the dataset identity. It does
not prove that a reported observation was honestly produced; raw run traces and
environment records remain necessary for formal evaluation.

The default CLI/Web application connects `eval` to the C11 execution factory.
The output must be a `.json` file under the state directory's `reports/` folder.
For example, run `python -m evo --state .evo eval examples/smoke.jsonl --output .evo/reports/smoke.json`.
The scripted end-to-end test exercises
the full C11 planner/bid/Harness/file-check path. Official mid/final scores and
a controlled memory ablation have not been run.
