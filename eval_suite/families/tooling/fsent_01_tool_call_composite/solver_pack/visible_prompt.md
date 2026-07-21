# fsent_01: Tool-Call Composite

You are in `/workspace/toolcall`.

Goal:

- Produce `out/final_submission.json`.
- Produce `out/dispatch_receipt.json`.

Rules:

- Use the dispatch tool contract in `contracts/tool_schema.json`.
- Reject legacy no-argument call patterns from stale docs.
- Include the called tool name, args, and receipt path in `out/final_submission.json`.
- Run visible check: `python3 checks/visible_check.py --candidate out/final_submission.json`.

Hidden grading verifies required argument semantics, trap rejection, and verifier-backed closure.
