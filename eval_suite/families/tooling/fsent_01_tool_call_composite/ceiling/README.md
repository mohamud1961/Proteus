# Ceiling

Ceiling behavior for this sentinel:

- Calls `dispatch_ticket` with complete required arguments.
- Produces matching `out/dispatch_receipt.json` and candidate summary.
- Runs verifier and closes only after pass evidence is present.
