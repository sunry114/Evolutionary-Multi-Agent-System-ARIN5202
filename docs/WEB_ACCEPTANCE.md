# C14 browser acceptance and C15 HTTP boundaries

Updated: 2026-11-14 (Asia/Shanghai). Based on main 1db0197 (SQLite PR #7 merged).
This closes the independently testable browser/boundary work for the current
SQLite workspace. It does not complete agent execution, chat, approval resume,
memory or evaluation integrations.

## Actual browser acceptance

Command: node tests/browser/web_acceptance.cjs.
Local runtime: Windows, real headless Microsoft Edge 157.0.4341.42 driven by
Playwright 1.62.1, against a real local Python HTTP server and isolated SQLite DB.
Desktop viewport: 1280x900; mobile viewport: 390x844.

The browser actually clicks and validates:

1. Stored tasks render, HTML/script-like content remains inert, run is disabled.
2. Invalid review displays an error and allows retry; approval is read-only.
3. Accept result, request changes, create linked revision and load audit trace.
4. Task/inbox/group/DM/memory navigation and unavailable chat controls.
5. Stop/restart the server process, reload/refresh and recover decisions/revision.
6. Mobile layout has no horizontal overflow; no uncaught page JavaScript errors.

Found and fixed: loading trace appended the trace then immediately refreshed the
whole task list, deleting the log before it could be read. Trace loading now
updates its detail locally, replaces the previous log and preserves the display.

Artifacts are generated under ignored .evo/browser-acceptance/:
desktop.png, mobile.png and report.json. These contain seeded test fixtures,
not real Agent performance or Git contribution evidence. A new CI browser job
runs the same acceptance with Chromium on Ubuntu and uploads the artifacts.

## Reproduce

Install Python package as in README, Node 22 and browser test dependencies:

```bash
npm --prefix tests/browser install --ignore-scripts --no-audit --no-fund --package-lock=false
cd tests/browser
npx playwright install --with-deps chromium
cd ../..
node tests/browser/web_acceptance.cjs
```

EVO_TEST_PYTHON selects the installed Python executable (default python).
EVO_BROWSER_EXECUTABLE optionally selects a locally installed Edge/Chromium;
otherwise Playwright's installed Chromium is used. EVO_PLAYWRIGHT_MODULE can
select an existing local Playwright installation; normal npm installs need no
override. Test setup may download dependencies; the acceptance itself uses only
loopback, no real API keys or remote model requests. It deletes only its own
validated temporary fixture directory.

## HTTP boundary regression

Eight new unittest methods send actual raw TCP requests to the local HTTP server.
They verify both the response and the absence of backend dispatch on rejection:

| Boundary | Response |
|---|---|
| Non-ASCII CSRF; duplicate Host/Origin/CSRF | 403 |
| Duplicate or wrong Content-Type | 415 |
| Missing, duplicate, signed or malformed Content-Length | 400 |
| Zero or over-64-KiB body | 413 |
| Exactly 64 KiB valid JSON object | Accepted |
| Transfer-Encoding, even empty | 400 |
| Truncated body or invalid UTF-8 | 400 |
| Body read deadline exceeded | 408 |
| Duplicate JSON keys, NaN/Infinity, excessively nested JSON | 400 |
| Valid UTF-8 object and application/json; charset=utf-8 | Accepted |

Fixes: safe ASCII CSRF comparison, unambiguous security/length headers, strict
UTF-8/object JSON parsing and explicit partial-body/timeout handling.
Ordinary HTTP/write authorization and SQLite atomicity tests remain in the suite.

Full local Python suite: 74 methods, 72 passed, 2 POSIX-only skips on Windows.
The six browser acceptance groups are separate from these 74 unittest methods.
Ubuntu CI runs the Python and browser jobs without real credentials.

## Remaining boundaries

Browser acceptance covers the current SQLite mode and fixture-driven review flow.
No real Agent task execution, chat reply, critical tool approval/resume, memory
evolution, Docker sandbox or official dataset metric is established. Those flows
must receive separate acceptance tests when A/B/C provides the implementations.
