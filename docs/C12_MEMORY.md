# C12 private memory and ablation

Each agent has its own SQLite database at `agents/<agent>/memory.sqlite3`.
`SQLiteMemoryStore` accepts only experiences for its bound agent. Recall ranks
task descriptions by lexical overlap, excludes unrelated entries, removes
duplicate lessons and returns at most three. Stored records include the task,
technical outcome, lesson, evidence, failure category and creation time.

The C11 `Orchestrator` accepts `memory_factory(agent)` and passes relevant
lessons to the selected agent's `RunRequest.memory`. Successful work is recorded
only after objective verification. Failed and blocked work is recorded as a
caution, never as success evidence. Each bid retains its raw score, bounded
memory adjustment and final score in the existing trace. The `memory_context`
event records how many lessons and outcomes were injected into a Harness run;
it does not claim that the model followed them.

`run(..., memory_enabled=False)` stores the mode in the root task's run
configuration. The setting survives a clarification or approval resume and
skips recall, bid adjustment and experience writes. The CLI accepts
`run <prompt> --no-memory` for the future application gateway; the default
SQLite gateway still does not execute `run`. C13 will use the mode for a
controlled memory versus no-memory evaluation. No improvement claim is made
without that comparison.

This first version uses lexical matching and deterministic lessons derived
from verified task state. It does not use embeddings or train a model.
