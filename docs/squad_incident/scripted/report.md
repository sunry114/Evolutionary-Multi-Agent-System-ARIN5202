# CSV export test failure

## Root cause

Run A17 ran out of space on export-temp (L02). The temporary write failed with ENOSPC (L03), and export_writes_csv failed (L04). Cleanup restored 67,108,864 bytes (L05).

## Independent verification

> Independent review conclusion: The same test passed after temporary space was freed, but one rerun does not establish that the failure will stay resolved.

The same test passed once in run A18 (L06–L07). The log records neither the source of disk growth nor additional runs under sustained load. Check temporary-file retention and available space.
