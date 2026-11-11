"""C02 前置生成器 build_label_template 的单测（离线、不依赖真实批次目录）。

覆盖：
- select_balanced：通过/失败平衡、确定可复现、batch1/2 各自均衡、类别铺开；
- render_template：输出符合 calibrate_judge.load_labels 的标签契约；
- check_filled：抓未填(null)/非布尔/fail 缺合法 stage，全合法时返回 0；
- read_results / read_cases：从 tmp_path 造的小批次文件可正确读回。
"""
import json

import build_label_template as blt


def make_row(case_id, passed, category, severity="high"):
    return {
        "result": {
            "case_id": case_id,
            "strict_passed": passed,
            "category": category,
            "risk_level": severity,
            "fail_reason": "" if passed else "Policy_Violation",
            "failure_stage": None if passed else "generation",
            "judge_layer": "rule",
            "judge_reason": "ok" if passed else "bad",
        },
        "trace": {
            "agent_response": f"resp-{case_id}",
            "tool_call_sequence": ["write_file"],
        },
    }


def make_pool():
    """每种类别 1 条通过 + 1 条失败，共 4 类别 × 2 = 8 条。"""
    rows = []
    for cat in ("A", "B", "C", "D"):
        rows.append(make_row(f"{cat}-pass", True, cat))
        rows.append(make_row(f"{cat}-fail", False, cat))
    return rows


# ---------- select_balanced ----------

def test_select_balanced_is_even_and_bounded():
    picked = blt.select_balanced(make_pool(), per_verdict=3, total=6)
    assert len(picked) == 6
    n_fail = sum(1 for r in picked if not r["result"]["strict_passed"])
    assert n_fail == 3  # 失败按 per_verdict，通过补足


def test_select_balanced_is_deterministic():
    pool = make_pool()
    a = [r["result"]["case_id"] for r in blt.select_balanced(pool, per_verdict=3, total=6)]
    b = [r["result"]["case_id"] for r in blt.select_balanced(pool, per_verdict=3, total=6)]
    assert a == b


def test_select_balanced_each_batch_stays_balanced():
    picked = blt.select_balanced(make_pool(), per_verdict=4, total=8)
    for batch in (1, 2):
        sub = [r for r in picked if r["batch"] == batch]
        n_fail = sum(1 for r in sub if not r["result"]["strict_passed"])
        assert n_fail == len(sub) / 2  # 每个 batch 内部通过/失败各半


def test_select_balanced_covers_multiple_categories():
    picked = blt.select_balanced(make_pool(), per_verdict=4, total=8)
    cats = {r["result"]["category"] for r in picked}
    assert cats == {"A", "B", "C", "D"}


def test_select_balanced_pads_when_fail_pool_short():
    # 只有 1 条失败，其余通过：total=4 时应用通过补齐到 4。
    pool = [make_row("X-fail", False, "A")] + [make_row(f"P{i}", True, "B") for i in range(5)]
    picked = blt.select_balanced(pool, per_verdict=20, total=4)
    assert len(picked) == 4


# ---------- render_template ----------

def test_render_template_matches_calibrate_contract():
    picked = blt.select_balanced(make_pool(), per_verdict=2, total=4)
    lines = [json.loads(l) for l in blt.render_template(picked).splitlines() if l.strip()]
    assert len(lines) == 4
    for rec in lines:
        assert "case_id" in rec
        assert rec["human_passed"] is None
        assert rec["human_stage"] is None
        assert rec["batch"] in (1, 2)
        # calibrate 会忽略下划线开头的辅助元信息，但它们应存在以便复核
        assert "_judge_strict_passed" in rec


def test_calibrate_load_labels_reads_template():
    """端到端：生成的模板能被 calibrate_judge.load_labels 正确解析（human_passed 转正类 False 不报错）。"""
    import calibrate_judge as cj

    picked = blt.select_balanced(make_pool(), per_verdict=2, total=4)
    text = blt.render_template(picked)
    # 用 tmp 文件喂 load_labels
    import tempfile
    import os

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        labels = cj.load_labels(path)
    finally:
        os.unlink(path)
    assert len(labels) == 4
    for rec in labels.values():
        assert rec["human_passed"] is False  # None → bool(False)，待人工覆盖


# ---------- check_filled ----------

def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")


def test_check_filled_flags_unfilled(tmp_path, capsys):
    p = tmp_path / "labels.jsonl"
    _write_jsonl(p, [{"case_id": "c1", "human_passed": None, "human_stage": None}])
    assert blt.check_filled(p) == 1
    assert "未填(null) 1" in capsys.readouterr().out


def test_check_filled_flags_fail_missing_stage(tmp_path, capsys):
    p = tmp_path / "labels.jsonl"
    _write_jsonl(p, [
        {"case_id": "c1", "human_passed": False, "human_stage": None},
        {"case_id": "c2", "human_passed": False, "human_stage": "bogus_stage"},
    ])
    assert blt.check_filled(p) == 1
    out = capsys.readouterr().out
    assert "fail 缺合法 stage 2" in out


def test_check_filled_flags_non_bool(tmp_path):
    p = tmp_path / "labels.jsonl"
    _write_jsonl(p, [{"case_id": "c1", "human_passed": "yes", "human_stage": None}])
    assert blt.check_filled(p) == 1


def test_check_filled_passes_when_complete(tmp_path, capsys):
    p = tmp_path / "labels.jsonl"
    _write_jsonl(p, [
        {"case_id": "c1", "human_passed": True, "human_stage": None},
        {"case_id": "c2", "human_passed": False, "human_stage": "generation"},
    ])
    assert blt.check_filled(p) == 0
    assert "OK" in capsys.readouterr().out


# ---------- read_results / read_cases ----------

def test_read_results_and_cases_roundtrip(tmp_path):
    (tmp_path / "results.jsonl").write_text(
        json.dumps(make_row("c1", True, "A")) + "\n\n" + json.dumps(make_row("c2", False, "B")) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "sampled_cases.jsonl").write_text(
        json.dumps({"id": "c1", "input": "hi", "expected_behavior": "eb"}) + "\n",
        encoding="utf-8",
    )
    rows = blt.read_results(tmp_path)
    cases = blt.read_cases(tmp_path)
    assert len(rows) == 2
    assert rows[0]["trace"]["agent_response"] == "resp-c1"
    assert cases["c1"]["input"] == "hi"


# ---------- _mask_local_paths ----------

def test_mask_local_paths_scrubs_home_dir():
    raw = "当前工作目录：`/Users/luoxuan/Desktop/\u9879\u76ee/workspace/20261111-sub`"
    masked = blt._mask_local_paths(raw)
    assert "luoxuan" not in masked
    assert "<LOCAL_PATH>" in masked


def test_mask_local_paths_handles_windows_and_home():
    assert "secret" not in blt._mask_local_paths("cwd=C:\\Users\\secret\\proj done")
    assert "/home/topsecret" not in blt._mask_local_paths("in /home/topsecret/a/b file")


def test_review_sheet_masks_paths_in_response():
    row = make_row("c1", True, "A")
    row["batch"] = 1
    row["trace"]["agent_response"] = "\u5f53\u524d\u76ee\u5f55\uff1a/Users/luoxuan/private/secret \u5b8c\u6210"
    sheet = blt.render_review_sheet([row], {"c1": {"input": "q"}}, "run-x")
    assert "luoxuan" not in sheet
