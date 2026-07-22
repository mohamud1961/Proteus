# Known bad

Known-bad candidate behaviors for this sentinel:

- Using `legacy_dispatch` or no-argument dispatch calls.
- Declaring closure without producing `out/dispatch_receipt.json`.
- Omitting verifier pass evidence in trace.
