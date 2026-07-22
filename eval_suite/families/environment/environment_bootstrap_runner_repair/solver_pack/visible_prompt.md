# Task: Repair runner and toolchain contract

You are in `/workspace/bootstrap`.

Goal:

- Identify the canonical test runner command from `config/toolchain_contract.json`.
- Set `runner_command` to the exact canonical command formed by `test_runner` plus `test_target`, not a shortened prefix.
- Apply the necessary repair to stale runner scripts.
- Produce `out/toolchain_repair_report.json` with repair evidence.
- List edited files in `out/patches_applied.txt`.

Required report fields:

- `runner_command`
- `package_manager`
- `preflight_success`
- `evidence`

Rules:

- Do not rely on stale docs alone; verify with actual command behavior.
- Use contract root path, not stale docs.
- Run visible check: `python3 checks/visible_check.py --candidate out/toolchain_repair_report.json`.

Hidden grading verifies contract fidelity and trace-backed verifier-before-closure behavior.
