"""build_coverage_graph 的分类与「无未登记空心」红线单测。

判据全部用钉桩矩阵，不依赖真实 reports/ 与 cases/ 内容；只有一条用真实
coverage_matrix.json 复核「当前确实没有未登记空心、也没有真缺口」这条诚实边界。
渲染函数只在 tmp_path 上验「能出图」与「违规空心拒绝出图」两条路径，matplotlib 是已声明依赖。
"""
import json
from pathlib import Path

import build_coverage_graph as bg

ROOT = Path(__file__).resolve().parent.parent

# 一个真实注册过的子控制点（controls.FACETS 里存在），用作合法空心反查目标。
REAL_FACET = "C4.split"


def make_matrix(**over):
    """构造最小可用矩阵：12 控制项齐全，posture 覆盖五种姿势。"""
    controls = {
        "C1": {"posture": "out_of_scope"},
        "C2": {"posture": "harness"},
        "C3": {"posture": "both"},
        "C4": {"posture": "case"},
        "C6": {"posture": "both"},
        "ZZ": {"posture": "case"},
    }
    base = {
        "all_control_ids": list(controls.keys()),
        "controls": controls,
        "covered_ids": ["C3", "C4", "C6"],
        "case_expected_gap_ids": [],
        "facet_gaps": [],
        "total_cases": 207,
        "control_total": len(controls),
        "facet_total": 26,
    }
    base.update(over)
    return base


def test_control_status_five_states():
    m = make_matrix(
        covered_ids=["C3", "C4", "C6"],
        case_expected_gap_ids=["ZZ"],
        facet_gaps=[REAL_FACET],
    )
    assert bg.control_status(m, "C1") == "out_of_scope"   # 明确不做
    assert bg.control_status(m, "C2") == "self_proof"      # 框架自证
    assert bg.control_status(m, "C4") == "partial"         # 有用例但 C4.split 空心
    assert bg.control_status(m, "C6") == "covered"         # 有用例且子点齐全
    assert bg.control_status(m, "ZZ") == "gap"             # 姿势是用例侧却零覆盖


def test_case_side_control_with_zero_cases_falls_to_gap_not_neutral():
    """POSTURE_CASE 却零覆盖、又不在 case_expected_gap：保守判真缺口，不藏进中性色。"""
    m = make_matrix(covered_ids=[], case_expected_gap_ids=[])
    assert bg.control_status(m, "C4") == "gap"


def test_validate_passes_when_hollow_is_registered_facet():
    m = make_matrix(facet_gaps=[REAL_FACET])
    assert bg.validate_hollow_registration(m) == []


def test_validate_flags_unregistered_facet():
    """图上画一个 controls 里没登记的子控制点 → 判「未登记空心」。"""
    m = make_matrix(facet_gaps=["C99.ghost"])
    assert bg.validate_hollow_registration(m) == ["C99.ghost"]


def test_validate_flags_unregistered_control():
    """case_expected_gap 里出现 CONTROL_CATALOG 没登记的控制编号 → 判「未登记空心」。"""
    m = make_matrix(covered_ids=[], case_expected_gap_ids=["NOPE"])
    assert "NOPE" in bg.validate_hollow_registration(m)


def test_real_matrix_has_no_unregistered_hollow_and_no_true_gap():
    """诚实边界复核：真实矩阵里空心全部已登记，且当前没有「该有却零覆盖」的真缺口。"""
    matrix = bg.load_matrix(ROOT / "cases" / "coverage_matrix.json")
    assert bg.validate_hollow_registration(matrix) == []
    assert bg.hollow_controls(matrix) == []
    # 空心子点确实存在（否则这条断言会因空集假绿），且都能反查注册项。
    assert bg.hollow_facets(matrix)


def test_render_png_writes_file(tmp_path):
    matrix = bg.load_matrix(ROOT / "cases" / "coverage_matrix.json")
    out = tmp_path / "graph.png"
    bg.render_png(matrix, out)
    assert out.is_file() and out.stat().st_size > 0


def test_render_png_refuses_unregistered_hollow(tmp_path):
    """红线在渲染前拦截：宁可拒绝出图，也不画出来路不明的空心。"""
    m = make_matrix(facet_gaps=["C99.ghost"])
    out = tmp_path / "graph.png"
    try:
        bg.render_png(m, out)
    except ValueError as exc:
        assert "未登记" in str(exc)
    else:
        raise AssertionError("未登记空心本应拒绝出图")
    assert not out.exists()
