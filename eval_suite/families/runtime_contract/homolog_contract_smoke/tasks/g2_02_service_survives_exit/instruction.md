Start the provided HTTP service as a detached background job.

Requirements:
1. Use the provided file `server_ok.py` in the current workspace.
2. Start it as a background job using the harness `start_job` tool.
3. The service must listen on `127.0.0.1:8123`.
4. A GET request to `http://127.0.0.1:8123/` must return exactly `ok`.
5. The service must keep running after the agent loop exits so the external verifier can check it.
6. Before calling `task_done`, verify the service responds exactly `ok`.

Do not use `python3 -m http.server`; it serves directory listings and will fail this task.
