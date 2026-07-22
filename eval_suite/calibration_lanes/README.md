# Calibration Lanes

Public-safe calibration view for adapter-driven surfaces.

## Public Artifacts

- `tool_call/apis/`: API spec fixtures for tool-call calibration (canonical location).
  Note: sample payload data has moved to `eval_suite/fixtures/bfcl/` (benchmark-derived pack).
- `retrieval/`: retrieval calibration reference.
  Note: Verified.csv sample data has moved to `eval_suite/fixtures/contextbench/` (benchmark-derived pack).
- `filesystem/`: filesystem calibration workspace reference.
  Note: alpha.txt sample has moved to `eval_suite/fixtures/letta/letta/filesystem-agent/files/` (benchmark-derived pack).
- `terminal/`: terminal-style task contract reference (no benchmark data; descriptive only).
- `../boards/public_calibration_lanes_v1.json`
- `../scoreboards/public_calibration_lanes_v1.example.scoreboard.json`

## Notes

This surface is for calibration and audit reading only. It is not the inner loop
for promotion authority. Fixture data for benchmark-derived packs is canonical in the
`eval_suite/fixtures/` and `eval_suite/families/` directories; the calibration lanes
contain metadata and API spec references only.
