# Independent Review — export_writes_csv failure in run A17

**Source reviewed:** `test-log.txt` only (7 lines, L01–L07). I had no prior diagnosis, and I did not read `report.md` or any author notes. Everything below is inferred from the source log alone.

## 1. Evidence: the failing run

- **L01** — `2026-11-19T09:14:00Z run=A17 test=export_writes_csv event=start`
  Run `A17` begins the test `export_writes_csv`.
- **L02** — `2026-11-19T09:14:01Z run=A17 volume=export-temp free_bytes=0`
  One second later, the volume the test writes to is reported with `free_bytes=0`. This is an absolute exhaustion of the `export-temp` volume, observed *before* any write error is logged.
- **L03** — `2026-11-19T09:14:01Z run=A17 event=temp_write_failed volume=export-temp error=ENOSPC`
  The temp write fails with `error=ENOSPC` ("no space left on device") on the same volume, in the same second as the zero-free-bytes reading.
- **L04** — `2026-11-19T09:14:02Z run=A17 test=export_writes_csv status=FAIL`
  The run is recorded as `status=FAIL`.

Line order (L02 → L03 → L04) plus the timestamps establish the sequence: capacity is already exhausted, then the write fails, then the test is marked failed.

## 2. Evidence: the cleanup event

- **L05** — `2026-11-19T09:15:00Z volume=export-temp event=expired_temp_files_removed free_bytes=67108864`
  Fifty-eight seconds after the failure, an event reports that **expired** temp files were removed from `export-temp` and that free space is now `67108864` bytes (64 MiB).

Two structural observations matter here:
1. **L05 carries no `run=` field.** Every other line except L05 is tagged with a run (`run=A17` on L01–L04, `run=A18` on L06–L07). The cleanup is therefore not attributable, from this log, to the failing run, to the test, or to the test harness at all.
2. The event name is `expired_temp_files_removed`. The word "expired" indicates a retention/age criterion, not a test-driven teardown — i.e. a pre-existing reaper acted on files that had already outlived their TTL.

## 3. Evidence: the rerun

- **L06** — `2026-11-19T09:15:10Z run=A18 test=export_writes_csv event=start`
  A new run `A18` starts the same test, ten seconds after the cleanup.
- **L07** — `2026-11-19T09:15:11Z run=A18 test=export_writes_csv status=PASS`
  The same test now reports `status=PASS`. No `temp_write_failed`, `ENOSPC`, or zero-free-bytes line appears for `A18`.

## 4. Causal chain inferred from the log

1. The `export-temp` volume was **already exhausted before run A17 attempted its write** (L02: `free_bytes=0`). The exhaustion predates the test and is not shown to be caused by it.
2. Because there was no free space, the temp write for the CSV export failed with `ENOSPC` (L03).
3. The test `export_writes_csv` could not complete its temp-write path and was recorded as `FAIL` (L04). The failure is a **capacity/environment condition surfaced through the test**, not evidence of a logic defect in the export code — the log shows no assertion mismatch, no data discrepancy, and no exception other than the write error.
4. The space was reclaimed by a cleanup that removed **expired** temp files (L05), restoring 64 MiB. The cleaned-up files are the plausible occupants of the volume, and the reduction from full to 64 MiB free is consistent with them being the cause of the exhaustion — but see §6, the log does not prove ownership or byte-for-byte attribution.
5. The rerun under the restored capacity passed (L06 → L07).

**Inferred root cause:** pre-existing, unbounded accumulation of expired temp files on `export-temp` filled the volume to zero free bytes; the export test's temp write hit `ENOSPC` and failed; removal of those expired files freed space and the identical test then passed.

**Nature of the defect:** this is best characterized as a **temp-file lifecycle / retention gap on `export-temp`** (files are produced and left behind until something expires them), with the export test as the victim rather than the culprit. A secondary contributor is the **absence of a pre-flight free-space guard** in the export path — the log shows the write being attempted against `free_bytes=0` with no capacity check recorded between L01 and L03.

Independent review conclusion: the single failing run A17 failed because the export-temp volume was already at zero free bytes from accumulated expired temp files and the export's temp write returned ENOSPC, and while the rerun A18 passing after the cleanup strongly corroborates that capacity was the operative constraint, it does not by itself prove the root cause, prove attribution of the freed space to those files, or prove the condition will not recur — it is one corroborating observation, not objective verification.

## 5. What the rerun does and does not prove

The rerun is **corroborating**, not confirming:
- **Supports:** the failure was reproducible in the same test/volume configuration but disappeared once 64 MiB was available. That is a strong, coherent story for a capacity-bound failure.
- **Does not support a "fix":** no code, configuration, retention-policy, or capacity change is recorded anywhere in L01–L07. A18 differs from A17, as far as the log shows, only in the free space on `export-temp`. The pass therefore demonstrates **the failure was not stable enough to persist**, not that a defect was repaired.
- **Nothing prevents recurrence:** because the cleanup was of *expired* files, the producer of those files is still unaddressed in this log. Refilling the volume re-creates the condition.

## 6. Limits of the evidence — what this log cannot establish

1. **Who or what created the temp files.** L01–L07 never record a writer, a producing component, or a file count. The export test is the only named actor, but the exhaustion in L02 appears one second after start, which is too early to attribute the fill to it — and the log offers no alternative attribution either.
2. **Who or what triggered the cleanup.** L05 has no `run=` field and no actor field. It cannot be determined whether the cleanup was an automated TTL reaper, an operator, or part of the harness. It also cannot be shown that A17's failure caused or scheduled it.
3. **Byte-for-byte attribution of the freed space.** L05 reports `free_bytes=67108864`. The log does not report how many bytes were actually removed, nor the sizes or names of the removed files. That 64 MiB became free after a removal is consistent with the files being the sole occupant, but it does not exclude other consumers of `export-temp` (e.g. other concurrent runs' scratch data) having also contributed or been removed.
4. **Whether `free_bytes=0` was caused by these files or merely accompanied by them.** Zero free space is a symptom; the log shows no per-consumer breakdown of `export-temp` occupancy.
5. **The precise mechanism of the test failure.** L04 records only `status=FAIL`. There is no assertion text, no stack trace, and no indication of whether the test asserted file contents, expected a file to exist, or propagated an exception. The link from `temp_write_failed` (L03) to `FAIL` (L04) is inferred from same-run adjacency and timing, not from an explicit causal field.
6. **Whether any other test, run, or volume was affected.** The log contains exactly one failing run and one passing run for exactly one test. It is silent on A17's other tests and on any other runs in the period, so the blast radius is unknown.
7. **Whether A17's failure was a first occurrence or a recurrence.** No history, no counts, no prior-run references. Frequency and trend cannot be assessed.
8. **Whether A18 passed for the same reason it would have passed before.** The log carries no code version, commit SHA, configuration hash, or environment identifier. A18 and A17 are assumed identical apart from free space; that assumption is unverified by the log.
9. **Statistical significance.** One failure and one pass (n=1 each, no repetitions, no negative control such as a rerun attempted while the volume was still full) cannot distinguish a capacity-bound failure from an intermittently failing test.
10. **Durability of any remediation.** No retention threshold, TTL value, quota, alert, or capacity-planning change appears in the log, so nothing here establishes that the condition is prevented from recurring.
11. **Ordering within the same second.** L02 and L03 share timestamp `09:14:01Z`. The precedence "exhaustion observed, then write failed" rests on line order in the file; sub-second ordering and thus strict simultaneity or causality between the two readings is not provable from the timestamps.

## 7. What I would need to go further

Owner-side evidence, none of which is in this log: the retention/TTL configuration for `export-temp`; a listing of the files removed at L05 with sizes; a per-consumer occupancy breakdown of the volume at the time of L02; the raw test output and assertion behind L04; and confirmation that A17 and A18 ran the same code and configuration.

*Scope note: this review is a single-source independent reading of `test-log.txt`. I did not perform or claim objective verification, and I did not consult `report.md` or any author notes. Findings above are inferences from L01–L07 and are bounded by §6.*
