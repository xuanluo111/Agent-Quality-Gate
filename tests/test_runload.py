"""run 产物统一读取口（T32 前置）的判据测试。

这里要钉住的是三类"错得很安静"的情况：
- 缺字段被当成失败或通过（会凭空造出结论位，噪声带与 ΔJ 都跟着失真）；
- 拿逐次 attempts 冒充最终结论（会抹掉"按最差一次合并"的门禁规则）；
- 坏行被静默吞掉（样本量缩水而读数上看不出来）。

刻意不重构 rejudge/build_label_template/judge_token_monitor/run_batch 那 4 处旧解析：
它们的测试在各自文件里，本模块只保证新入口的语义。
"""

import json

from src.engine.runload import (
    attempt_bits,
    attempts_of,
    bit_of,
    case_attempts_from_run,
    case_bits_from_run,
    case_final_bits,
    case_id_of,
    iter_result_rows,
    read_run_config,
    seed_of,
)


def _write_rows(tmp_path, rows, *, raw_lines=()):
    path = tmp_path / "results.jsonl"
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    lines.extend(raw_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _result_row(case_id, attempt_bits_list, final=None, seeds=None):
    attempts = [
        {"attempt": index + 1, "strict_passed": bit}
        for index, bit in enumerate(attempt_bits_list)
    ]
    if seeds:
        for attempt, seed in zip(attempts, seeds):
            attempt["seed"] = seed
    row = {
        "result": {
            "case_id": case_id,
            "attempts": attempts,
            "strict_passed": final if final is not None else all(
                bit is True for bit in attempt_bits_list
            ),
        },
        "trace": {"case_id": case_id},
    }
    return row


# --------------------------------------------------------------------------
# 结论位三态
# --------------------------------------------------------------------------


def test_missing_outcome_key_is_none_not_false():
    """把"没落盘"读成"没通过"会凭空造失败项，直接污染噪声带与分母。"""
    assert bit_of({"attempt": 1}) is None
    assert bit_of({"strict_passed": None}) is None
    assert bit_of({"strict_passed": "unknown"}) is None


def test_string_booleans_are_normalized():
    assert bit_of({"strict_passed": "true"}) is True
    assert bit_of({"strict_passed": "PASS"}) is True
    assert bit_of({"strict_passed": "false"}) is False
    assert bit_of({"strict_passed": "failed"}) is False


def test_seed_of_absent_is_none_rather_than_zero():
    """seed 缺失只说明"未落盘"，不等于"没变序"；返回 None 让调用方分得清这两件事。"""
    assert seed_of({}) is None
    assert seed_of({"seed": "42"}) is None  # 非 int 不当证据
    assert seed_of({"seed": 42}) == 42


# --------------------------------------------------------------------------
# attempts 顺序与形态兼容
# --------------------------------------------------------------------------


def test_attempts_are_ordered_by_attempt_index():
    """乱序落盘时若不排序，"第 k 期"在不同题上指代不同东西，配对就失去意义。"""
    row = {
        "result": {
            "case_id": "c1",
            "attempts": [
                {"attempt": 3, "strict_passed": True},
                {"attempt": 1, "strict_passed": False},
                {"attempt": 2, "strict_passed": False},
            ],
        }
    }
    assert attempt_bits(row) == [False, False, True]


def test_attempts_keep_seed_keys_for_variation_proof():
    """attempts_of 必须保留 seed：变序是否生效只能从原始记录里核对。"""
    row = _result_row("c1", [True, False], seeds=[42, 7961])
    seeds = [seed_of(item) for item in attempts_of(row)]
    assert seeds == [42, 7961]


def test_single_shot_rows_fall_back_to_result_body():
    """repeat=1 的历史批没有 attempts 数组：最终结论本身即那一次尝试，不能读成空。"""
    row = {"result": {"case_id": "c1", "strict_passed": True}}
    assert attempt_bits(row) == [True]
    assert len(attempts_of(row)) == 1


def test_all_none_attempts_fall_back_to_top_level():
    """attempts 只记了非判定字段时退回顶层，避免整条题被当成 0 次尝试而消失。"""
    row = {"result": {"case_id": "c1", "strict_passed": False, "attempts": [{"attempt": 1}]}}
    assert attempt_bits(row) == [False]


def test_case_id_falls_back_across_four_keys():
    assert case_id_of({"result": {"case_id": "a"}}) == "a"
    assert case_id_of({"result": {"id": "b"}}) == "b"
    assert case_id_of({"trace": {"case_id": "c"}}) == "c"
    assert case_id_of({"result": {}}) == ""


# --------------------------------------------------------------------------
# 两种口径不可互换
# --------------------------------------------------------------------------


def test_final_bits_use_merged_verdict_not_first_attempt(tmp_path):
    """合并规则是"按最差一次"：attempts 里有 False 时最终必须 False。

    拿 attempts[0] 当最终结论会得到 True，ΔJ 就跟发布门禁看的不是同一个数。
    """
    rows = [_result_row("c1", [True, False], final=False)]
    _write_rows(tmp_path, rows)
    finals, audit = case_final_bits(tmp_path)
    bits, _ = case_bits_from_run(tmp_path)
    assert finals["c1"] is False          # 门禁口径
    assert bits["c1"] == [True, False]     # 噪声口径：两次各自的原始结论
    assert audit["rows"] == 1


# --------------------------------------------------------------------------
# 坏行不静默
# --------------------------------------------------------------------------


def test_bad_lines_are_counted_not_dropped_silently(tmp_path):
    rows = [_result_row("c1", [True, True]), _result_row("c2", [True, False], final=False)]
    _write_rows(tmp_path, rows, raw_lines=['{"result": {"case_id": "c3"', ""])
    attempts, audit = case_attempts_from_run(tmp_path)
    assert sorted(attempts) == ["c1", "c2"]
    assert audit["bad_lines"] == 1
    assert audit["rows"] == 2


def test_missing_case_id_is_counted(tmp_path):
    _write_rows(tmp_path, [{"result": {"attempts": [{"attempt": 1, "strict_passed": True}]}}])
    attempts, audit = case_attempts_from_run(tmp_path)
    assert attempts == {}
    assert audit["missing_case_id"] == 1
    assert audit["rows"] == 1


def test_iter_result_rows_yields_bad_flag_for_truncated_tail(tmp_path):
    path = _write_rows(tmp_path, [], raw_lines=['{"result": {"case_id": "x"}}', "{ broken"])
    produced = list(iter_result_rows(path))
    assert produced[0][1] is False
    assert produced[1] == (None, True)


def test_absent_run_directory_returns_empty_with_zero_audit(tmp_path):
    missing = tmp_path / "nope"
    missing.mkdir()
    assert case_final_bits(missing) == ({}, {"rows": 0, "bad_lines": 0, "missing_case_id": 0})
    assert case_attempts_from_run(missing)[0] == {}


# --------------------------------------------------------------------------
# 配置快照
# --------------------------------------------------------------------------


def test_read_run_config_tolerates_missing_and_corrupt(tmp_path):
    assert read_run_config(tmp_path) == {}
    (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
    assert read_run_config(tmp_path) == {}
    (tmp_path / "config.json").write_text("[1, 2]", encoding="utf-8")
    assert read_run_config(tmp_path) == {}
    (tmp_path / "config.json").write_text(json.dumps({"repeat": 3}), encoding="utf-8")
    assert read_run_config(tmp_path)["repeat"] == 3
