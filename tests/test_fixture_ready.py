"""C14′ fixture_ready 的单测：静态判定与沙箱真实行为必须一致。

最要紧的一条是 `test_every_static_issue_also_breaks_sandbox`——静态预检一旦宽松，
它就从"拦在跑批前"退化成"跑批前给个安心提示"，跑批时照样炸、照样把失败记到模型
头上。所以判据不靠枚举，靠与 `Sandbox` 对拍。
"""
import pytest

from src.engine.fixture_ready import (
    DIAGNOSIS_ORDER,
    FixtureIssue,
    check_fixtures,
    fixture_summary,
    render_issues,
)
from src.engine.tools import MAX_FIXTURE_BYTES, Sandbox
from src.models.schema import NO_FINAL_RESPONSE_PREFIX
from tests.conftest import FakeClient, make_case, text_response
from tests.test_runner import build_runner


def _reasons(fixtures):
    return [(issue.path, issue.reason) for issue in check_fixtures(fixtures)]


def test_empty_fixtures_pass():
    assert check_fixtures({}) == []
    assert check_fixtures(None) == []


def test_legit_multi_file_fixtures_pass():
    fixtures = {
        "data/users.csv": "name,age\n张三,28\n",
        "notes/todo.md": "- 核对数据",
        "report.txt": "草稿",
    }
    assert check_fixtures(fixtures) == []


def test_clean_relative_segments_pass():
    """`a/./b` 与 `..foo/x` 都不是越界：前者归一化后仍在工作区内，后者只是名字以点开头。"""
    assert check_fixtures({"a/./b.txt": "x", "..foo/x.txt": "y"}) == []


def test_empty_path_is_reported():
    assert _reasons({"  ": "x"}) == [("  ", "empty_path")]


def test_absolute_path_is_reported():
    assert _reasons({"/etc/passwd": "x"}) == [("/etc/passwd", "absolute_path")]


@pytest.mark.parametrize("path", ["../x.txt", "../../etc/passwd", "a/../../x.txt", ".."])
def test_escape_is_reported(path):
    assert _reasons({path: "x"}) == [(path, "escape")]


@pytest.mark.parametrize("path", [".", "a/..", "./"])
def test_root_directory_target_is_not_a_file(path):
    assert _reasons({path: "x"}) == [(path, "not_a_file")]


def test_oversize_fixture_is_reported():
    issues = check_fixtures({"big.txt": "a" * (MAX_FIXTURE_BYTES + 1)})
    assert [issue.reason for issue in issues] == ["too_large"]
    assert "超过上限" in issues[0].detail


def test_size_counts_utf8_bytes_not_characters():
    """上限按字节算：中文三字节，按字符数判会把超限的当成合法。"""
    text = "中" * ((MAX_FIXTURE_BYTES // 3) + 1)
    assert len(text) <= MAX_FIXTURE_BYTES < len(text.encode("utf-8"))
    assert [issue.reason for issue in check_fixtures({"big.txt": text})] == ["too_large"]


def test_duplicate_target_reported_on_the_overwritten_one():
    """两条写同一路径时，被静默丢掉的是后声明的那条，报错也该报在它身上。"""
    issues = check_fixtures({"data/x.txt": "先", "data//x.txt": "后"})
    assert len(issues) == 1
    assert issues[0].path == "data//x.txt"
    assert issues[0].reason == "duplicate_target"
    assert "data/x.txt" in issues[0].detail


def test_case_only_conflict_is_reported():
    """大小写不敏感文件系统上 Notes.md 与 notes.md 同一个文件，只能留一个。"""
    issues = check_fixtures({"Notes.md": "a", "notes.md": "b"})
    assert [(issue.path, issue.reason) for issue in issues] == [("notes.md", "case_conflict")]


def test_issues_keep_declaration_order():
    issues = check_fixtures({"/abs": "x", "../up": "y", "ok.txt": "z", ".": "w"})
    assert [issue.reason for issue in issues] == ["absolute_path", "escape", "not_a_file"]


def test_every_static_issue_also_breaks_sandbox(tmp_path):
    """对拍：静态报出来的每一类，`Sandbox` 构造时都真的失败（或真的丢文件）。

    `duplicate_target` / `case_conflict` 在沙箱里不抛异常，所以单独验它们确实丢了内容。
    """
    throwing = {
        "empty_path": {"  ": "x"},
        "absolute_path": {"/etc/passwd": "x"},
        "escape": {"../x.txt": "x"},
        "too_large": {"big.txt": "a" * (MAX_FIXTURE_BYTES + 1)},
    }
    for reason, fixtures in throwing.items():
        assert [issue.reason for issue in check_fixtures(fixtures)] == [reason]
        with pytest.raises(Exception):
            Sandbox(tmp_path / reason, fixtures)

    # not_a_file：不抛 SandboxViolation，但写盘必然 IsADirectoryError。
    with pytest.raises(IsADirectoryError):
        Sandbox(tmp_path / "not_a_file", {".": "x"})

    silent = tmp_path / "silent"
    Sandbox(silent, {"data/x.txt": "先", "data//x.txt": "后"})
    assert (silent / "data" / "x.txt").read_text(encoding="utf-8") == "后"


def test_fixture_summary_counts_cases_and_reasons():
    cases = [
        make_case(id="ok", fixtures={"a.txt": "x", "b/c.txt": "y"}),
        make_case(id="bad", fixtures={"../oops.txt": "x"}),
        make_case(id="nofix", fixtures={}),
    ]
    summary = fixture_summary(cases)

    assert summary["total"] == 3
    assert summary["with_fixtures"] == 2
    assert summary["files"] == 3
    assert [item["case_id"] for item in summary["issues"]] == ["bad"]
    assert summary["by_reason"]["escape"] == 1
    # 顺序固定，报告里的分布不会随数据变来变去。
    assert list(summary["by_reason"]) == list(DIAGNOSIS_ORDER)


def test_render_issues_truncates():
    issues = [FixtureIssue(path=f"p{i}.txt", reason="escape", detail="越界") for i in range(7)]
    rendered = render_issues(issues, limit=5)

    assert "p4.txt" in rendered
    assert "p5.txt" not in rendered
    assert "共 7 条" in rendered


def test_render_issues_handles_empty_path():
    assert "<空路径>" in render_issues([FixtureIssue(path="", reason="empty_path", detail="空")])


def test_runner_marks_fixture_failure_as_precondition(tmp_path):
    """跑批层的落点：预检不过必须 `precondition_failed=True`，且一次模型调用都不发生。

    原来的分支只写 `error_message="沙箱初始化失败: ..."`、`precondition_failed` 保持
    默认 False，gate 于是把环境故障算进分母。这条断言就是钉这个归因。
    """
    case = make_case(id="bad-fixture", fixtures={"../escape.txt": "x"})
    client = FakeClient([text_response(f"{NO_FINAL_RESPONSE_PREFIX}不该被调用")])
    runner = build_runner(tmp_path, [], client=client)
    trace = runner.run_case(case)

    assert trace.precondition_failed is True
    assert trace.is_success is False
    assert "fixture 预检未通过" in trace.error_message
    assert "escape.txt" in trace.error_message
    assert "越出工作区" in trace.error_message
    assert client.calls == []


def test_runner_does_not_precreate_workspace_when_fixture_fails(tmp_path):
    """预检在 `Sandbox` 之前跑，失败时连工作区目录都不该建。"""
    case = make_case(id="bad-fixture-dir", fixtures={"../escape.txt": "x"})
    runner = build_runner(tmp_path, [], client=FakeClient([]))

    trace = runner.run_case(case)

    assert not trace.execution_workspace or not (tmp_path / "workspace").exists()


def test_runner_keeps_normal_case_untouched(tmp_path):
    """预检不能顺手改变合法用例的行为。"""
    case = make_case(id="fine", fixtures={"data/users.csv": "name,age\n张三,28\n"})
    runner = build_runner(tmp_path, [], client=FakeClient([text_response("完成。")]))

    trace = runner.run_case(case)

    assert trace.is_success
    assert trace.precondition_failed is False
    workspace = tmp_path / "workspace" / "test-run"
    assert any(path.name == "users.csv" for path in workspace.rglob("users.csv"))
