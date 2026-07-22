# Ceiling

Ceiling behavior for this flagship:

- Binds to 127.0.0.1:18923.
- GET /health returns {"status": "ok", "version": "1.0"} with HTTP 200.
- POST /echo returns {"echo": "<body>"} with HTTP 200.
- Survives both observation windows.
- Shuts down cleanly on SIGTERM with exit code 0.
