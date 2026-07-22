from eval_suite.adapters.letta_context_bench import (
    grade_letta_filesystem_answer,
    selected_letta_filesystem_specs,
)


def test_selected_specs_bind_easy_medium_hard_cases():
    specs = selected_letta_filesystem_specs()
    assert [spec["difficulty"] for spec in specs] == ["easy", "medium", "hard"]
    assert [spec["benchmark_class"] if "benchmark_class" in spec else spec["class"] for spec in specs] == [
        "letta_context_bench",
        "letta_context_bench",
        "letta_context_bench",
    ]


def test_grade_accepts_name_anywhere_in_response():
    grade = grade_letta_filesystem_answer("The correct answer is Tammy Roberts.", "Tammy Roberts")
    assert grade["verdict"] == "pass"


def test_grade_accepts_currency_normalization():
    grade = grade_letta_filesystem_answer("145315.33", "$145,315.33")
    assert grade["verdict"] == "pass"


def test_grade_accepts_number_words():
    grade = grade_letta_filesystem_answer("The answer is fourteen total records.", "14")
    assert grade["verdict"] == "pass"


def test_grade_rejects_wrong_answer():
    grade = grade_letta_filesystem_answer("George Peterson", "Tammy Roberts")
    assert grade["verdict"] == "fail"
    assert grade["reason_codes"] == ["letta_ground_truth_mismatch"]
