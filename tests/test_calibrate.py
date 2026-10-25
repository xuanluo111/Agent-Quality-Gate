import json

from calibrate_judge import (
    calibrate,
    cohens_kappa,
    confusion,
    load_judgements,
    load_labels,
    main,
    render_text,
)


def write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")
    return path


def labels_fixture():
    return {
        "pass-ok": {"human_passed": True, "human_stage": None},
        "fail-missed": {"human_passed": False, "human_stage": "generation"},
        "fail-caught": {"human_passed": False, "human_stage": "validation"},
    }


def judgements_fixture():
    return {
        "pass-ok": {"judge_passed": True, "judge_stage": None},
        "fail-missed": {"judge_passed": True, "judge_stage": None},
        "fail-caught": {"judge_passed": False, "judge_stage": "validation"},
    }


def test_confusion_counts_against_human_labels():
    pairs = [(True, True), (True, False), (False, False), (False, True)]
    assert confusion(pairs) == {"tp": 1, "tn": 1, "fp": 1, "fn": 1}


def test_miss_and_false_alarm_rates_use_the_right_denominator():
    report = calibrate(labels_fixture(), judgements_fixture())
    assert report["matched"] == 3
    assert report["confusion"] == {"tp": 1, "tn": 1, "fp": 1, "fn": 0}
    # 人工判失败 2 条，机器放过 1 条 -> 漏判 50%
    assert report["miss_rate"] == 50.0
    # 人工判通过 1 条，机器没有误伤 -> 0%
    assert report["false_alarm_rate"] == 0.0
    assert report["observed_agreement"] == 66.67


def test_false_alarm_rate_counts_judge_failures_on_human_passed_samples():
    labels = {"a": {"human_passed": True, "human_stage": None}, "b": {"human_passed": True, "human_stage": None}}
    judgements = {
        "a": {"judge_passed": False, "judge_stage": "generation"},
        "b": {"judge_passed": True, "judge_stage": None},
    }
    report = calibrate(labels, judgements)
    assert report["false_alarm_rate"] == 50.0
    assert report["miss_rate"] is None


def test_stage_agreement_is_grouped_by_human_stage():
    report = calibrate(labels_fixture(), judgements_fixture())
    rows = {row["stage"]: row for row in report["stage_agreement"]}
    assert rows["validation"]["agree"] == 1
    assert rows["validation"]["stage_accuracy"] == 100.0
    assert rows["generation"]["stage_accuracy"] == 0.0


def test_cohens_kappa_matches_hand_computation():
    # observed = 2/3, expected = 2/3*1/3 + 1/3*2/3 = 4/9 -> kappa = 0.4
    assert cohens_kappa([(True, True), (True, False), (False, False)]) == 0.4


def test_cohens_kappa_is_none_when_undefined():
    assert cohens_kappa([]) is None
    assert cohens_kappa([(True, True), (True, True)]) is None


def test_loaders_accept_wrapped_and_flat_records(tmp_path):
    labels_path = write_jsonl(
        tmp_path / "labels.jsonl",
        [{"case_id": "c1", "human_passed": False, "human_stage": "Generation"}, {"not_a_case": 1}],
    )
    results_path = write_jsonl(
        tmp_path / "results.jsonl",
        [{"result": {"case_id": "c1", "strict_passed": True, "failure_stage": None}}, {"case_id": "c2", "passed": True}],
    )
    labels = load_labels(labels_path)
    judgements = load_judgements(results_path)
    assert labels == {"c1": {"human_passed": False, "human_stage": "generation"}}
    assert set(judgements) == {"c1", "c2"}
    assert judgements["c2"]["judge_passed"] is True


def test_unmatched_cases_are_reported(tmp_path):
    labels_path = write_jsonl(tmp_path / "labels.jsonl", [{"case_id": "only-label", "human_passed": True}])
    results_path = write_jsonl(tmp_path / "results.jsonl", [{"case_id": "only-result", "strict_passed": True}])
    report = calibrate(load_labels(labels_path), load_judgements(results_path))
    assert report["matched"] == 0
    assert report["unmatched_labels"] == ["only-label"]
    assert report["unmatched_results"] == ["only-result"]


def test_render_text_contains_rates_and_matrix():
    text = render_text(calibrate(labels_fixture(), judgements_fixture()))
    assert "混淆矩阵" in text
    assert "漏判率" in text and "50.0%" in text
    assert "Cohen's kappa" in text


def test_main_writes_json_and_returns_zero(tmp_path):
    labels_path = write_jsonl(
        tmp_path / "labels.jsonl",
        [{"case_id": "c1", "human_passed": True}, {"case_id": "c2", "human_passed": False}],
    )
    results_path = write_jsonl(
        tmp_path / "results.jsonl",
        [{"case_id": "c1", "strict_passed": True}, {"case_id": "c2", "strict_passed": False}],
    )
    output = tmp_path / "calibration.json"
    code = main(["--labels", str(labels_path), "--results", str(results_path), "--output", str(output), "--json"])
    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["matched"] == 2
    assert payload["confusion"] == {"tp": 1, "tn": 1, "fp": 0, "fn": 0}


def test_main_returns_one_when_nothing_matched(tmp_path):
    labels_path = write_jsonl(tmp_path / "labels.jsonl", [{"case_id": "a", "human_passed": True}])
    results_path = write_jsonl(tmp_path / "results.jsonl", [{"case_id": "b", "strict_passed": True}])
    assert main(["--labels", str(labels_path), "--results", str(results_path)]) == 1
