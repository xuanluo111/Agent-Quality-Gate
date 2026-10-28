"""C10 供应链锁定校验（check_supply_chain）单测。

这里锁的是「框架侧依赖是否真被钉死」，全离线、不联网：喂文本进去，出来结构化报告。
最后一组还直接对仓库里真实的 requirements.txt / requirements.lock 做一次集成断言，
防止单测用假数据全绿、真实文件却是脏的。
"""
from pathlib import Path

from check_supply_chain import (
    _release_tuple,
    check,
    main,
    normalize,
    parse_lock_entries,
    parse_requirements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_normalize_pep503():
    # 大小写不敏感，`_`/`.`/连续分隔符归一为单个 `-`；但绝不会无中生有地插入分隔符。
    assert normalize("PYTHON_DOTENV") == normalize("python-dotenv") == "python-dotenv"
    assert normalize("pydantic_core") == "pydantic-core"
    assert normalize("Foo.Bar") == "foo-bar"


def test_release_tuple_compares_numeric_prefix():
    assert _release_tuple("2.9.0.post0") == (2, 9, 0)
    assert _release_tuple("1.30.0") < _release_tuple("1.31")
    assert _release_tuple("3.29.0") > _release_tuple("1.30.0")


def test_parse_requirements_skips_comments_and_option_lines():
    text = "# comment\n-r requirements.txt\n\nopenai>=1.30.0\npydantic>=2.0.0  # via x\n"
    entries = parse_requirements(text)
    names = {norm for norm, _orig, _lower in entries}
    assert names == {"openai", "pydantic"}


def test_parse_lock_flags_non_pinned_lines():
    text = "openai==2.0.0\npydantic>=2.0.0\nmatplotlib\n"
    pinned, unpinned = parse_lock_entries(text)
    assert pinned == {"openai": "2.0.0"}
    assert set(unpinned) == {"pydantic>=2.0.0", "matplotlib"}


def test_check_ok_when_every_direct_dep_pinned_at_or_above_bound():
    req = "openai>=1.30.0\npython-dotenv>=1.0.0\n"
    lock = "openai==2.0.0\npython-dotenv==1.2.4\nanyio==4.15.1\n"
    report = check(req, lock)
    assert report["ok"] is True
    assert report["missing_from_lock"] == []
    assert report["unpinned_lines"] == []
    assert report["bound_violations"] == []


def test_check_reports_missing_direct_dep():
    req = "openai>=1.30.0\nrequests>=2.0.0\n"
    lock = "openai==2.0.0\n"
    report = check(req, lock)
    assert report["ok"] is False
    assert report["missing_from_lock"] == ["requests"]


def test_check_reports_lock_below_declared_bound():
    req = "openai>=2.0.0\n"
    lock = "openai==1.5.0\n"
    report = check(req, lock)
    assert report["ok"] is False
    assert report["bound_violations"][0]["package"] == "openai"
    assert report["bound_violations"][0]["locked"] == "1.5.0"


def test_check_rejects_unpinned_lock_line():
    req = "openai>=1.0.0\n"
    lock = "openai>=1.30.0\n"
    report = check(req, lock)
    assert report["ok"] is False
    assert "openai>=1.30.0" in report["unpinned_lines"]


def test_main_exit_codes(tmp_path):
    req = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock"
    req.write_text("openai>=1.30.0\n", encoding="utf-8")
    lock.write_text("openai==2.0.0\n", encoding="utf-8")
    assert main(["--requirements", str(req), "--lock", str(lock)]) == 0
    lock.write_text("openai==1.0.0\n", encoding="utf-8")  # 低于下界
    assert main(["--requirements", str(req), "--lock", str(lock)]) == 1
    assert main(["--requirements", str(req), "--lock", str(tmp_path / "missing.lock")]) == 1


def test_real_repo_lock_is_consistent():
    """真实文件必须过校验：单测假数据全绿不代表仓库自身干净。"""
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    report = check(req, lock)
    assert report["ok"] is True, report
