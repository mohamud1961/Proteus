# Known bad

Known-bad behaviors for this task:

- Stopping at partial extraction (first segment only, e.g. moves 1-3).
- Including stale frames in the output instead of excluding them.
- Failing to apply contradiction corrections (using original instead of corrected move).
- Wrong output format (JSON instead of plain text, missing move numbers, extra whitespace).
- Including duplicate moves in the final output.
