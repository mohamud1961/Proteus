# structured_retrieval_reduction: Frame-Text Extraction and Reconciliation

You are in `/workspace/extract`.

Goal:

- Produce `out/moves.txt` containing the correct sequence of chess moves extracted from frame-text fixtures.

Sources:

- `source/frames/frame_001.txt` through `source/frames/frame_012.txt` contain ordered frame-text captures of chess move notations.
- Some frames are **duplicates** (same move appears in multiple frames).
- Some frames contain **contradictions** (a later frame corrects an earlier one -- the correction is authoritative).
- Some frames are **stale** (prefixed with `[STALE - re-recorded]` and must be excluded entirely).

Rules:

- Read all frames in order.
- Exclude any frame marked as stale.
- When two frames contradict on the same move number, the later frame (higher frame number) is authoritative.
- Remove duplicates -- each move number appears exactly once.
- Consult `spec/output_format.md` for the exact output format.
- Run the readiness gate: `python3 checks/visible_check.py`.

Hidden grading enforces exact move list content and format.
