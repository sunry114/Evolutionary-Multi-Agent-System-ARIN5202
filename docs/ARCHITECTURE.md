# Architecture

Implemented components include D's installation, provider adapters, CLI, loopback
Web UI and ReviewService, C05 SQLite, C06 collaboration services, C07 file tools
and C08 bounded search/retrieval.
The diagram shows remaining integration boundaries; the full orchestrator, Docker
execution and memory learning are still pending.

```mermaid
flowchart TD
    H[Human] --> CLI[CLI]
    H --> WEB[Loopback Web / CSRF]
    CLI --> G[ApplicationGateway contract]
    WEB --> G
    G -. A supplies .-> O[Chat / planner / orchestrator]
    G --> R[D ReviewService]
    R -. B supplies .-> S[Atomic ReviewRepository / SQLite]
    O -. D provider adapter .-> L[DeepSeek / HKGAI HTTPS]
    O -. C supplies .-> T[Tool broker / approvals / Linux Docker]
    O -. B supplies .-> M[Private memory / objective evaluation]
```

Provider adapters use only Python's standard library, bounded requests and retries,
no redirects, no raw provider exception output, and private external credentials.
Agent tool/network policies remain C's responsibility.

The Web server binds 127.0.0.1 only, requires exact Host on all requests, exact
Origin plus per-process CSRF token on writes, JSON object bodies under 64 KiB,
and refuses transfer encoding. The page uses textContent for untrusted output
and a nonce-based CSP. No CORS, external scripts, remote fonts or public hosting.
Use the printed 127.0.0.1 URL; localhost is deliberately not an alternate Host.

Production persistence belongs to B. The gateway must never return credentials;
known environment key values and secret-named fields are additionally redacted
at CLI/HTTP output. Demo state is volatile and explicitly has no execution.

## C05 durable SQLite storage

Runtime persistence is stored below the ignored `.evo/` directory:

```text
.evo/
├── events.sqlite3
└── agents/
    └── <validated-agent-id>/
        └── memory.sqlite3
```
`events.sqlite3` stores tasks, messages, append-only trace events, tool action metadata, approval or clarification requests, and human review state. Every SQLite operation opens and explicitly closes its connection. The adapter enables foreign keys and a bounded busy timeout for every connection.

Schema changes are versioned under evo.adapters.persistence.migrations. A new empty state directory is migrated automatically. A database with a schema newer than the running application is rejected rather than silently modified.

Human review is separate from technical execution status. Review decisions use an expected-state conditional update in one write transaction. The review update and its audit event commit or roll back together. Revision task creation, parent linking, review-state transition and trace events also share one transaction.

Each agent receives a physically separate memory database. C05 establishes its location, validation and initial schema only. Recall, ranking, bid adjustment and the no-memory ablation are implemented by C12.

SQLite databases are trusted application state. They must never be mounted into an agent Docker sandbox, included in task artifacts, committed to Git, or exposed through Web responses. Event payloads contain bounded non-secret metadata rather than credentials or raw environment variables. 

## C07 workspace and artifact boundary

`adapters/tools/filesystem.py` supplies a broker bound to one agent's
`agents/<agent>/workspace/`, beside its C05 memory database. Paths are resolved
inside that workspace and opened relative to no-follow directory descriptors.
Only bounded UTF-8 regular files with purpose-specific suffixes are accepted;
symlinks, multiple hardlinks, traversal, `.git` and special files are rejected.
Documents default to Markdown/TXT. Writes install complete files atomically.

`adapters/tools/artifacts.py` snapshots published bytes and metadata under the
private `artifacts/<id>/` directory. Visibility and imports require ownership or
an explicit recipient grant. Imports verify SHA-256 and create an independent
file without overwriting. Neither artifact storage nor memory is sandbox-mounted.
The application must select the bound instance using trusted task ownership.
C10/C11 must obtain human approval before enabling deletion; C09 supplies the
process boundary for agent-generated code. See [C07 contract](C07_WORKSPACES.md).

## C08 public web boundary

`WebTools` implements `WebPort` with one trusted instance per task. All DNS
addresses and redirect destinations must be public HTTPS/443; transport connects
to the validated numeric address with the original TLS hostname. Search parsing
is replaceable and offline-testable. Results retain source URLs and explicit
untrusted metadata; fetched HTML becomes labeled text, never executable content.
Body size, network time, redirects and task search count are bounded. C11 must
reuse the task instance and persist/recover budget and action audit state.
See [C08 limits and verification](C08_WEB_TOOLS.md).
