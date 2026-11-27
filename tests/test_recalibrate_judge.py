import json

import pytest

import recalibrate_judge
import rejudge
from tests.conftest import make_case, make_result, make_trace


def write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")
    return path


def write_run(tmp_path, results):
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    dataset = tmp_path / "cases.jsonl"
    write_jsonl(dataset, [make_case(id=item.case_id).model_dump() for item in results])
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "dataset": str(dataset),
                "run_id": "run-001",
                "workspace_root": str(tmp_path / "ws"),
                "judge_model": "glm-5.3-flash",
                "judge_base_url": "https://open.bigmodel.cn/api/paas/v4",
                "judge_samples": 2,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text("{}", encoding="utf-8")
    write_jsonl(
        run_dir / "results.jsonl",
        [
            {"result": item.model_dump(), "trace": make_trace(case_id=item.case_id).model_dump()}
            for item in results
        ],
    )
    return run_dir


def read_results(path):
    return [json.loads(line)["result"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_validate_labels_rejects_unfilled_duplicate_and_malformed(tmp_path):
    path = tmp_path / "labels.jsonl"
    write_jsonl(path, [{"case_id": "a", "human_passed": None}])
    with pytest.raises(SystemExit, match="尚未填写"):
        recalibrate_judge.validate_labels(path)

    write_jsonl(
        path,
        [
            {"case_id": "a", "human_passed": True},
            {"case_id": "a", "human_passed": False},
        ],
    )
    with pytest.raises(SystemExit, match="重复"):
        recalibrate_judge.validate_labels(path)

    path.write_text('{"case_id": "a", "human_passed": true}\nnot-json\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="不是合法 JSON"):
        recalibrate_judge.validate_labels(path)


def test_prepare_state_filters_labels_and_overrides_judge_without_touching_source(tmp_path):
    results = [
        make_result(case_id="a", judge_layer="rule"),
        make_result(case_id="b", judge_layer="llm"),
        make_result(case_id="unlabeled"),
    ]
    run_dir = write_run(tmp_path, results)
    labels = {
        "a": {"human_passed": True, "human_stage": None},
        "b": {"human_passed": False, "human_stage": "generation"},
    }
    output_dir = tmp_path / "out"

    state = recalibrate_judge.prepare_isolated_state(
        run_dir,
        labels,
        output_dir,
        "glm-5.3",
        "https://token.sensenova.cn/v1/",
        3,
        "default",
    )

    assert [item.case_id for item in state["results"]] == ["a", "b"]
    assert state["config"]["judge_model"] == "glm-5.3"
    assert state["config"]["judge_base_url"] == "https://token.sensenova.cn/v1/"
    assert state["config"]["judge_samples"] == 3
    assert state["config"]["judge_thinking"] == "default"
    assert state["config"]["judge_reasoning_effort"] == "low"
    assert state["config"]["judge_stream"] is True
    assert state["dir"] == output_dir
    assert [item["result"]["judge_layer"] for item in map(json.loads, (run_dir / "results.jsonl").read_text().splitlines())] == [
        "rule",
        "llm",
        "rule",
    ]


def test_prepare_state_rejects_glm53_disabled_thinking(tmp_path):
    run_dir = write_run(tmp_path, [make_result(case_id="a")])
    with pytest.raises(SystemExit, match="thinking 始终开启"):
        recalibrate_judge.prepare_isolated_state(
            run_dir,
            {"a": {"human_passed": True, "human_stage": None}},
            tmp_path / "out",
            "glm-5.3",
            "https://token.sensenova.cn/v1/",
            2,
            "disabled",
        )
    assert not (tmp_path / "out").exists()


def test_prepare_state_fails_when_labeled_case_is_missing(tmp_path):
    run_dir = write_run(tmp_path, [make_result(case_id="a")])
    with pytest.raises(SystemExit, match="缺少人工标签对应样本"):
        recalibrate_judge.prepare_isolated_state(
            run_dir,
            {"missing": {"human_passed": True, "human_stage": None}},
            tmp_path / "out",
            "glm-5.3",
            "https://token.sensenova.cn/v1/",
            2,
            "default",
        )


def test_output_dir_must_not_write_into_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(recalibrate_judge, "ROOT", tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(SystemExit, match="reports/"):
        recalibrate_judge.validate_output_dir(tmp_path / "reports" / "calib", run_dir)
    with pytest.raises(SystemExit, match="覆盖源 run"):
        recalibrate_judge.validate_output_dir(run_dir, run_dir)


def test_glm53_rejects_disabled_thinking_before_writing(tmp_path):
    run_dir = write_run(tmp_path, [make_result(case_id="a")])
    labels = write_jsonl(tmp_path / "labels.jsonl", [{"case_id": "a", "human_passed": True}])
    output_dir = tmp_path / "out"

    with pytest.raises(SystemExit, match="thinking 始终开启"):
        recalibrate_judge.main(
            [
                "--run",
                str(run_dir),
                "--labels",
                str(labels),
                "--output-dir",
                str(output_dir),
                "--judge-model",
                "glm-5.3",
                "--thinking",
                "disabled",
                "--dry-run",
            ]
        )

    assert not output_dir.exists()


def test_dry_run_requires_no_key_and_writes_nothing(tmp_path, monkeypatch):
    results = [make_result(case_id="a"), make_result(case_id="b", strict_passed=False, passed=False)]
    run_dir = write_run(tmp_path, results)
    labels = write_jsonl(
        tmp_path / "labels.jsonl",
        [
            {"case_id": "a", "human_passed": True},
            {"case_id": "b", "human_passed": False, "human_stage": "generation"},
        ],
    )
    output_dir = tmp_path / "out"
    monkeypatch.delenv("SENSENOVA_HH_API_KEY", raising=False)

    code = recalibrate_judge.main(
        [
            "--run",
            str(run_dir),
            "--labels",
            str(labels),
            "--output-dir",
            str(output_dir),
            "--judge-api-key-env",
            "SENSENOVA_HH_API_KEY",
            "--dry-run",
        ]
    )

    assert code == 0
    assert not output_dir.exists()


def test_recalibration_rejudges_all_labeled_rows_and_stays_isolated(tmp_path, monkeypatch):
    results = [
        make_result(case_id="a", judge_layer="rule"),
        make_result(case_id="b", judge_layer="rule", strict_passed=True, passed=True),
        make_result(case_id="unlabeled", judge_layer="rule"),
    ]
    run_dir = write_run(tmp_path, results)
    labels = {
        "a": {"human_passed": True, "human_stage": None},
        "b": {"human_passed": False, "human_stage": "generation"},
    }
    output_dir = tmp_path / "out"
    state = recalibrate_judge.prepare_isolated_state(
        run_dir,
        labels,
        output_dir,
        "glm-5.3",
        "https://token.sensenova.cn/v1/",
        2,
        "default",
    )

    class FakeJudge:
        def __init__(self):
            self.calls = []

        def evaluate(self, case, trace):
            self.calls.append(case.id)
            passed = case.id == "a"
            return make_result(
                case_id=case.id,
                passed=passed,
                strict_passed=passed,
                judge_layer="llm",
                judge_model="glm-5.3",
                fail_reason="None" if passed else "Policy_Violation",
                failure_stage=None if passed else "generation",
            )

    fake = FakeJudge()
    monkeypatch.setattr(rejudge, "build_judge", lambda *args, **kwargs: fake)
    stats = recalibrate_judge.run_recalibration(state, "test-key", workers=2, retries=0)
    report = recalibrate_judge.write_calibration(
        state,
        labels,
        run_dir,
        "SENSENOVA_HH_API_KEY",
        stats,
        exclude_invalid=False,
        exclude_cases=None,
    )

    assert stats["targets"] == 2
    assert sorted(fake.calls) == ["a", "b"]
    assert [item["case_id"] for item in read_results(output_dir / "results.jsonl")] == ["a", "b"]
    assert all(item["judge_model"] == "glm-5.3" for item in read_results(output_dir / "results.jsonl"))
    assert report["kappa"] == 1.0
    assert report["complete"] is True
    assert report["judge_api_key_env"] == "SENSENOVA_HH_API_KEY"
    assert report["judge_reasoning_effort"] == "low"
    assert report["judge_stream"] is True
    assert "test-key" not in (output_dir / "config.json").read_text(encoding="utf-8")
    assert [item["result"]["judge_layer"] for item in map(json.loads, (run_dir / "results.jsonl").read_text().splitlines())] == [
        "rule",
        "rule",
        "rule",
    ]
    assert not (output_dir / "report.md").exists()
    assert not (output_dir / "summary.json").exists()
