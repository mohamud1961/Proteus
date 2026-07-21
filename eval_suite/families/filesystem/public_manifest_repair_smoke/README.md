# Public Manifest Repair Smoke

This is a small public-safe smoke eval pack for the evaluation substrate.

It uses a synthetic filesystem repair task with:

- a messy workspace fixture;
- a deterministic local grader;
- a board manifest that points at the task pack and grader;
- an example scoreboard labeled as smoke/example output, not benchmark evidence.

The task family is intentionally simple enough to run locally without model
calls, but it still exercises multiple files, red herrings, and deterministic
grading.
