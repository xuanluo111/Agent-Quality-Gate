"""自检脚本的体检规则单测：规则失效会让 verify 变成“永远通过”，比没有更危险。"""
from types import SimpleNamespace

from scripts.verify import (
    COUNT_CLAIM_ANCHORS,
    REQUIRED_CAPABILITY_TAGS,
    REQUIRED_SECTIONS,
    REQUIRED_SUMMARY_KEYS,
    missing_capability_tags,
    missing_report_sections,
    missing_summary_fields,
    parse_collected_count,
    parse_declared_count,
)


def test_missing_summary_fields_lists_only_absent_keys():
    summary = {key: 0 for key in REQUIRED_SUMMARY_KEYS if key != "gate"}
    assert missing_summary_fields(summary) == ["gate"]
    assert missing_summary_fields({key: 0 for key in REQUIRED_SUMMARY_KEYS}) == []


def test_missing_report_sections_detects_chapter_regression():
    """章节标题是 visualize_reports.parse_report 的解析契约，改标题必须能被发现。"""
    full = "\n".join(REQUIRED_SECTIONS)
    assert missing_report_sections(full) == []

    broken = full.replace("## 三、故障事件聚合", "## 三、故障事件")
    assert missing_report_sections(broken) == ["## 三、故障事件聚合"]


def test_missing_capability_tags_lists_absent_tags():
    # 不写死个数：缺哪个报哪个，补齐后为空。
    present = list(REQUIRED_CAPABILITY_TAGS[:-1])
    assert missing_capability_tags(present) == [REQUIRED_CAPABILITY_TAGS[-1]]
    assert missing_capability_tags(set(REQUIRED_CAPABILITY_TAGS)) == []


def test_capability_dataset_covers_all_required_tags():
    """真机层跑的就是这份集合：如果筛漏了能力 tag，“验六项能力”就名不副实。"""
    from scripts.verify import _capability_cases

    picked = _capability_cases(0)
    tags = {tag for case in picked for tag in case.tags}
    assert missing_capability_tags(tags) == []
    assert len(picked) >= 3


def test_capability_dataset_respects_limit():
    from scripts.verify import _capability_cases

    assert len(_capability_cases(2)) == 2
    assert _capability_cases(0) == _capability_cases(999)


def test_step_dataset_keeps_manifest_timestamp_when_content_unchanged(tmp_path, monkeypatch):
    """自检只做校验：数据集内容没变时，不能把 manifest 的 generated_at 改掉。

    否则每跑一次 `make verify` 就会多一个只差时间戳的 diff，把工作区弄脏。
    """
    import json
    from types import SimpleNamespace

    import scripts.verify as verify

    cases = tmp_path / "cases"
    cases.mkdir()
    tags = list(REQUIRED_CAPABILITY_TAGS)
    (cases / "curated_cases.jsonl").write_text('{"id": "a"}\n', encoding="utf-8")
    (cases / "core_suite.jsonl").write_text(
        json.dumps({"id": "a", "tags": tags}) + "\n", encoding="utf-8"
    )
    (cases / "smoke.jsonl").write_text(json.dumps({"id": "a", "tags": tags}) + "\n", encoding="utf-8")
    manifest = cases / "suite_manifest.json"
    manifest.write_text(
        json.dumps({"generated_at": "2026-01-01 00:00:00", "core": {"written": 1}, "smoke": {"written": 1}}),
        encoding="utf-8",
    )
    original = manifest.read_bytes()

    def fake_run(args, timeout=1800):
        # 模拟 build_suite：用例内容不变，只刷新 generated_at
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["generated_at"] = "2026-01-02 00:00:00"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "_run", fake_run)

    step = verify.step_dataset()

    assert step.ok is True, step.detail
    assert manifest.read_bytes() == original


def test_step_dataset_fails_when_a_suite_keeps_duplicate_ids(tmp_path, monkeypatch):
    """去重没生效时自检必须报错：否则 `--suite regression` 会默默把同一条用例再评一遍。"""
    import json
    from types import SimpleNamespace

    import scripts.verify as verify

    cases = tmp_path / "cases"
    cases.mkdir()
    tags = list(REQUIRED_CAPABILITY_TAGS)
    duplicate = json.dumps({"id": "a", "tags": tags}) + "\n"
    (cases / "curated_cases.jsonl").write_text('{"id": "a"}\n', encoding="utf-8")
    (cases / "core_suite.jsonl").write_text(duplicate * 2, encoding="utf-8")
    (cases / "smoke.jsonl").write_text(duplicate, encoding="utf-8")
    (cases / "suite_manifest.json").write_text(
        json.dumps({"generated_at": "2026-01-01 00:00:00", "core": {"written": 2}, "smoke": {"written": 1}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify, "_run", lambda args, timeout=1800: SimpleNamespace(returncode=0, stdout="", stderr=""))

    step = verify.step_dataset()

    assert step.ok is False
    assert "core_suite.jsonl" in step.detail and "重复 id" in step.detail


# ---------------------------------------------------------------------------
# T15：「README 声明条数 == collect 实测条数」自检
# 这条规则本身如果解析错（把日期当条数、锚点丢了退化成假绿），比没有这条规则更糟。
# ---------------------------------------------------------------------------
def _write_readmes(tmp_path, cn_count, en_count):
    """造两份只含锚点与条数的假 README，键名与 `COUNT_CLAIM_ANCHORS` 对齐。"""
    anchors = dict(COUNT_CLAIM_ANCHORS)
    for name, anchor in anchors.items():
        count = cn_count if name.endswith("README.md") else en_count
        (tmp_path / name).write_text(
            f"> **{anchor}**：**{count} 条（collect 口径，实测 2026-11-20）**\n", encoding="utf-8"
        )
    return anchors


def _patch_count_step(monkeypatch, verify, tmp_path, collected):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(
        verify,
        "_run",
        lambda args, timeout=1800: SimpleNamespace(
            returncode=0, stdout=f"{collected} tests collected in 1.23s\n", stderr=""
        ),
    )


def test_declared_count_matches_the_real_readmes():
    """真仓两份 README 都必须锁得到条数，且中英给出同一个数。"""
    from pathlib import Path

    import scripts.verify as verify

    values = {}
    for name, anchor in verify.COUNT_CLAIM_ANCHORS.items():
        text = (Path(verify.ROOT) / name).read_text(encoding="utf-8")
        values[name] = parse_declared_count(text, anchor)
        assert values[name] is not None, f"{name} 的锚点文案被改动了"
    assert len(set(values.values())) == 1


def test_parse_declared_count_skips_dates_and_picks_the_count():
    """日期（2026-11-20）不能被当成条数——锚点丢了时必须返回 None，而不是退化成年份。"""
    anchor = "全仓唯一一处单测条数声明"
    assert parse_declared_count(f"{anchor}：**673 条（实测 2026-11-20）**", anchor) == 673
    # 只有日期、条数被删：宁可 None（自检判红），不要 2026（假绿）。
    assert parse_declared_count(f"{anchor}：实测 2026-11-20", anchor) is None
    # 锚点整段被换掉：同样 None。
    assert parse_declared_count("别的文案：**673 条**", anchor) is None


def test_parse_collected_count_from_pytest_output():
    assert parse_collected_count("673 tests collected in 1.23s") == 673
    assert parse_collected_count("1 test collected in 0.01s") == 1
    assert parse_collected_count("no summary line here") is None
    assert parse_collected_count("") is None
    assert parse_collected_count(None) is None


def test_step_test_count_claim_green_when_declared_equals_collected(tmp_path, monkeypatch):
    import scripts.verify as verify

    _write_readmes(tmp_path, 681, 681)
    _patch_count_step(monkeypatch, verify, tmp_path, 681)

    step = verify.step_test_count_claim()

    assert step.ok is True, step.detail
    assert "681" in step.detail


def test_step_test_count_claim_red_when_declared_count_drifts(tmp_path, monkeypatch):
    """原验收的机器化：新增用例后忘了改声明（或把声明改成假值），自检必须红。"""
    import scripts.verify as verify

    _write_readmes(tmp_path, 673, 673)
    _patch_count_step(monkeypatch, verify, tmp_path, 681)

    step = verify.step_test_count_claim()

    assert step.ok is False
    assert "673" in step.detail and "681" in step.detail


def test_step_test_count_claim_red_when_readmes_disagree(tmp_path, monkeypatch):
    """中英两份必须同数：只改一份是历史上最常见的漏改。"""
    import scripts.verify as verify

    _write_readmes(tmp_path, 681, 673)
    _patch_count_step(monkeypatch, verify, tmp_path, 681)

    step = verify.step_test_count_claim()

    assert step.ok is False
    assert "不一致" in step.detail


def test_step_test_count_claim_red_when_anchor_or_readme_missing(tmp_path, monkeypatch):
    """锚点文案被改坏 / README 被删，都必须判红而不是跳过。"""
    import scripts.verify as verify

    _write_readmes(tmp_path, 681, 681)
    _patch_count_step(monkeypatch, verify, tmp_path, 681)
    readme = tmp_path / "README.md"
    readme.write_text("锚点文案被换掉了：**681 条**\n", encoding="utf-8")

    assert verify.step_test_count_claim().ok is False

    readme.unlink()
    step = verify.step_test_count_claim()
    assert step.ok is False
    assert "缺少 README.md" in step.detail


def test_step_test_count_claim_red_when_collect_output_unparsable(tmp_path, monkeypatch):
    """collect 输出解析不出来（pytest 挂了/口径变了）不能当成通过。"""
    import scripts.verify as verify

    _write_readmes(tmp_path, 681, 681)
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(
        verify,
        "_run",
        lambda args, timeout=1800: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    step = verify.step_test_count_claim()

    assert step.ok is False
    assert "collect" in step.detail


# ---- 入库件本机身份扫描（T19 的验收式变成自检项）------------------------------
def _fake_identity(monkeypatch, user, home):
    import scripts.verify as verify

    monkeypatch.setattr(verify, "current_identity_env", lambda: (user, home))
    return verify


def _fake_grep(monkeypatch, results):
    """按 token 返回预设 (returncode, stdout)；未列出的 token 视为无命中。"""
    import scripts.verify as verify

    calls = []

    def fake_run(args, timeout=1800):
        token = args[-1]
        calls.append(args)
        code, out = results.get(token, (1, ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(verify, "_run", fake_run)
    return calls


def test_identity_tokens_collects_personal_user_and_home():
    from scripts.verify import identity_tokens

    assert identity_tokens("zhangsan", "/Users/zhangsan") == {"zhangsan", "/Users/zhangsan"}


def test_identity_tokens_skips_generic_accounts():
    """CI 账户与 root 不收：扫它们只会到处误报，误报三次就等于废掉这条自检。"""
    from scripts.verify import identity_tokens

    assert identity_tokens("runner", "/home/runner") == set()
    assert identity_tokens("root", "/root") == set()


def test_identity_tokens_requires_length_but_home_tail_is_checked_on_its_own():
    """用户名太短（bob）不扫——它会在无关文本里误命中；但家目录尾段够长就单独收。"""
    from scripts.verify import identity_tokens

    assert identity_tokens("bob", "/Users/bob") == set()
    assert identity_tokens("bob", "/Users/bobby") == {"/Users/bobby"}


def test_parse_grep_hits_dedupes_and_ignores_noise():
    from scripts.verify import parse_grep_hits

    out = "docs/a.md:3:x\nsrc/b.py:12:y\nsrc/b.py:12:z\nBinary file README.md matches\nbad line\n"
    assert parse_grep_hits(out) == ["docs/a.md:3", "src/b.py:12"]
    assert parse_grep_hits("") == []
    assert parse_grep_hits(None) == []


def test_step_local_identity_hygiene_green_when_no_hits(monkeypatch):
    verify = _fake_identity(monkeypatch, "zhangsan", "/Users/zhangsan")
    _fake_grep(monkeypatch, {})

    step = verify.step_local_identity_hygiene()

    assert step.ok is True, step.detail
    assert "0 命中" in step.detail


def test_step_local_identity_hygiene_red_lists_offending_files(monkeypatch):
    verify = _fake_identity(monkeypatch, "zhangsan", "/Users/zhangsan")
    _fake_grep(
        monkeypatch,
        {
            "zhangsan": (0, "tests/test_x.py:5:/Users/zhangsan/tmp\n"),
            "/Users/zhangsan": (0, "tests/test_x.py:5:/Users/zhangsan/tmp\ndocs/y.md:9:/Users/zhangsan\n"),
        },
    )

    step = verify.step_local_identity_hygiene()

    assert step.ok is False
    # 同一行被两个 token 各命中一次，只能算一处：2 处命中 / 2 个文件
    assert "2 处命中 / 2 个文件" in step.detail
    assert "tests/test_x.py" in step.detail and "docs/y.md" in step.detail


def test_step_local_identity_hygiene_red_when_git_grep_cannot_run(monkeypatch):
    """扫描没跑成（rc 非 0/1）不得当成干净——假绿比红更糟。"""
    verify = _fake_identity(monkeypatch, "zhangsan", "/Users/zhangsan")
    _fake_grep(monkeypatch, {"zhangsan": (128, "")})

    step = verify.step_local_identity_hygiene()

    assert step.ok is False
    assert "git grep 执行失败" in step.detail


def test_step_local_identity_hygiene_skip_is_stated_not_silent(monkeypatch):
    """通用账户下判 OK，但必须把「跳过」写在输出里，不能伪装成“已扫过且干净”。"""
    verify = _fake_identity(monkeypatch, "runner", "/home/runner")
    calls = _fake_grep(monkeypatch, {})

    step = verify.step_local_identity_hygiene()

    assert step.ok is True
    assert "跳过" in step.detail
    assert calls == []


def test_step_local_identity_hygiene_is_registered_in_offline_layer():
    """写出来但没进 main() 的 steps，等于一条永远不跑的自检。"""
    import re
    from pathlib import Path

    import scripts.verify as verify

    source = (Path(verify.ROOT) / "scripts" / "verify.py").read_text(encoding="utf-8")
    block = re.search(r"steps = \[(.*?)\n    \]", source, re.S).group(1)
    assert "step_local_identity_hygiene()" in block


def test_the_repo_itself_passes_the_identity_gate():
    """真仓实测：本机跑要求 0 命中；CI 的通用账户下要求明写跳过。两种都不许红。"""
    import scripts.verify as verify

    step = verify.step_local_identity_hygiene()

    assert step.ok is True, step.detail
