import json
import sys
from pathlib import Path


def main() -> int:
    pack_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(pack_root))
    from reviewer_pack.hidden_verifier import evaluate  # pylint: disable=import-error

    truth = json.loads((pack_root / "reviewer_pack" / "hidden_truth.json").read_text())
    result = evaluate(pack_root / "candidate", truth)

    grade_result = {
        "task_pack_id": "hidden_verifier_repair",
        "score": result["score"],
        "passed": result["passed"],
        "reason_codes": result["reasons"],
        "contamination_gate": "clean",
        "direct_public_row_copy": False,
    }
    print(json.dumps(grade_result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
