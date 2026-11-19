# Diagnosis Report — `export_writes_csv` failure in run A17

**Source:** `test-log.txt` (7 lines, L01–L07).
**Independent check:** `review.md`, produced by the reviewer from `test-log.txt` alone, before seeing any diagnosis (imported artifact `c1e67312c136aa135a9eb951de3332d2`, sha256 `828b712c…`).

## Root cause

The failure is a capacity exhaustion of the `export-temp` volume caused by retained temp files, surfacing through the export test rather than originating in it.

**Failure evidence (run A17):**
- **L01** — `run=A17 test=export_writes_csv event=start`: the test begins.
- **L02** — `run=A17 volume=export-temp free_bytes=0`: the target volume is already at zero free bytes one second after start, i.e. the exhaustion is observed *before* any write error.
- **L03** — `run=A17 event=temp_write_failed volume=export-temp error=ENOSPC`: the temp write fails with "no space left on device" on that same volume.
- **L04** — `run=A17 test=export_writes_csv status=FAIL`: the run is recorded as failed.

**Cleanup evidence:**
- **L05** — `volume=export-temp event=expired_temp_files_removed free_bytes=67108864`: 58 seconds after the failure, expired temp files are removed from `export-temp`, restoring 64 MiB of free space. Notably, L05 carries **no `run=` field**, unlike L01–L04 (`run=A17`) and L06–L07 (`run=A18`), and the event name says *expired*, indicating an age/TTL-based reaper rather than a test-driven teardown.

**Rerun evidence:**
- **L06** — `run=A18 test=export_writes_csv event=start`: the same test is started again, ten seconds after the cleanup.
- **L07** — `run=A18 test=export_writes_csv status=PASS`: the same test passes, with no `temp_write_failed`, `ENOSPC`, or zero-free-bytes line for `A18`.

**Causal chain:** pre-existing unbounded accumulation of temp files left `export-temp` at `free_bytes=0` (L02) → the export path attempted a temp write and got `ENOSPC` (L03) → the test was recorded `FAIL` (L04) → an expiry-driven cleanup reclaimed space to 64 MiB (L05) → the identical test passed on rerun (L06–L07).

**Character of the defect:** a temp-file lifecycle / retention gap on `export-temp` — files are produced and left until something expires them — with `export_writes_csv` as the victim, not the culprit. A secondary contributor is the absence of a pre-flight free-space check: the log shows a write attempted against `free_bytes=0` with no capacity guard recorded between L01 and L03. This diagnosis is an inference from the log and the reviewer's independent reading of it; it is not a claim of objective verification.

## Independent verification

The reviewer read `test-log.txt` with purpose `document` in isolation, with no diagnosis supplied, and wrote `review.md` with line citations, a causal chain, an explicit evidence-limits section, and a stand-alone verdict sentence.

**Reviewer's conclusion, quoted verbatim:**

> Independent review conclusion: the single failing run A17 failed because the export-temp volume was already at zero free bytes from accumulated expired temp files and the export's temp write returned ENOSPC, and while the rerun A18 passing after the cleanup strongly corroborates that capacity was the operative constraint, it does not by itself prove the root cause, prove attribution of the freed space to those files, or prove the condition will not recur — it is one corroborating observation, not objective verification.

**Where the review agrees with the analysis above:** the same line-level reading of L01–L04 (failure), L05 (cleanup, and the significance of its missing `run=` field), and L06–L07 (rerun); the same causal chain; the same framing of the retention gap on `export-temp` plus the missing pre-flight free-space guard; and the same refusal to treat the pass as a proof of repair.

**Where the review sharpens or narrows it:** the reviewer emphasizes that the word `expired` in L05 points to a TTL/age criterion rather than a harness teardown; that the cleanup actor and the temp-file producer are both unattributable from this log; that L02/L03 share the timestamp `09:14:01Z`, so the observed ordering rests on file line order, not on sub-second timestamps; and that the log contains no code version, commit SHA, or configuration hash, so "A17 and A18 differ only in free space" is an assumption the log does not verify.

### Limits of the rerun

The A18 pass (L06–L07) is corroborating, not confirming, and the following limits apply:

1. **No fix is recorded.** Nothing in L01–L07 shows a code, configuration, retention-policy, or capacity change. The rerun demonstrates that the failure did not persist, not that a defect was repaired.
2. **Single observation each way.** One failure and one pass (n=1), with no repetitions and no negative control (no rerun attempted while the volume was still full). This cannot separate a capacity-bound failure from an intermittently failing test.
3. **No confirmed equivalence of A17 and A18.** Absent a version/commit/config/environment identifier, treating the two runs as identical apart from free space is unverified.
4. **Recurrence is not excluded.** Because the cleanup removed *expired* files, the producer of those files is unaddressed in this log; refilling the volume would re-create the condition.
5. **Attribution of the freed space is not proven.** L05 reports `free_bytes=67108864` but not how many bytes were removed, nor the names or sizes of the removed files, nor a per-consumer breakdown of `export-temp` occupancy. Other consumers may have contributed to or been removed by the cleanup.
6. **Blast radius and history are unknown.** The log covers one test, one failing run, and one passing run; it is silent on A17's other tests, on other runs, and on whether this was a first occurrence or a recurrence.
7. **Failure mechanism is inferred, not shown.** L04 records only `status=FAIL` — no assertion text, stack trace, or explicit causal field links L03 to L04.

For this reason the report states the root cause as the best-supported inference from L01–L07, independently corroborated by the reviewer, and does **not** claim objective verification.
