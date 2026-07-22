# Task: Implement a local HTTP service

You are in `/workspace/service`.

## Goal

Create `out/service_impl.py` -- a standalone Python HTTP server that:

1. Listens on `localhost:18923`
2. Responds to `GET /health` with JSON `{"status": "ok", "version": "1.0"}`
3. Responds to `POST /echo` with JSON `{"echo": <request_body_as_string>}`
4. Runs until terminated (SIGTERM or SIGINT)

## Contract details

Read `spec/service_contract.md` for the full contract.

## Requirements

- The server must bind to `127.0.0.1:18923` exactly.
- `GET /health` must return HTTP 200 with `Content-Type: application/json`.
- `POST /echo` must read the raw request body, and return HTTP 200 with
  `Content-Type: application/json` and body `{"echo": "<the_body>"}`.
- The server must survive for at least 2 seconds of continuous operation.
- On receiving SIGTERM, the server must shut down cleanly (exit code 0).
- Use only Python standard library (no pip packages).

## Output

Place your implementation at `out/service_impl.py`.

## Visible check

Run: `python3 checks/visible_check.py`

This starts `out/service_impl.py`, probes `/health` and `/echo`, checks that
the service survives a short observation window, sends SIGTERM, and requires a
clean exit.

## Grading

An independent verifier will start your server, make its own HTTP requests,
and validate responses against the contract. It will also verify the server
survives two observation windows and shuts down cleanly.
