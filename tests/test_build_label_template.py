"""C02 前置生成器 build_label_template 的单测（离线、不依赖真实批次目录）。

覆盖：
- select_balanced：通过/失败平衡、确定可复现、batch1/2 各自均衡、类别铺开；
- render_template：输出符合 calibrate_judge.load_labels 的标签契约；
- check_filled：抓未填(null)/非布尔/fail 缺合法 stage，全合法时返回 0；
- read_results / read_cases：从 tmp_path 造的小批次文件可正确读回；
- render_tool_calls / render_run_facts：逐步工具参数与执行事实的渲染、脱敏、三态空值，
  以及「不得泄裁判结论」这条抗锚定红线；render_review_sheet 的字段顺序。
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


# ---------- drop_invalid ----------

def test_drop_invalid_removes_data_invalid_rows():
    pool = make_pool()
    pool[0]["result"]["data_valid"] = False
    pool[1]["result"]["data_valid"] = True
    kept = blt.drop_invalid(pool)
    assert len(kept) == len(pool) - 1
    assert "A-pass" not in [r["result"]["case_id"] for r in kept]


def test_drop_invalid_keeps_rows_without_the_field():
    pool = make_pool()  # make_row 不带 data_valid，历史批次同此
    assert len(blt.drop_invalid(pool)) == len(pool)


def test_drop_invalid_keeps_valid_rows_when_pool_all_valid():
    pool = make_pool()
    for row in pool:
        row["result"]["data_valid"] = True
    assert len(blt.drop_invalid(pool)) == len(pool)


# ---------- _mask_local_paths ----------

def test_mask_local_paths_scrubs_home_dir():
    raw = "当前工作目录：`/Users/someuser/Desktop/\u9879\u76ee/workspace/20261111-sub`"
    masked = blt._mask_local_paths(raw)
    # 用户名用占位符 someuser：真名进测试样例＝把操作者本机路径推进公开仓库（T19）
    assert "someuser" not in masked
    assert "<LOCAL_PATH>" in masked


def test_mask_local_paths_handles_windows_and_home():
    assert "secret" not in blt._mask_local_paths("cwd=C:\\Users\\secret\\proj done")
    assert "/home/topsecret" not in blt._mask_local_paths("in /home/topsecret/a/b file")


def test_review_sheet_masks_paths_in_response():
    row = make_row("c1", True, "A")
    row["batch"] = 1
    row["trace"]["agent_response"] = "\u5f53\u524d\u76ee\u5f55\uff1a/Users/someuser/private/secret \u5b8c\u6210"
    sheet = blt.render_review_sheet([row], {"c1": {"input": "q"}}, "run-x")
    assert "someuser" not in sheet


# --- 凭据打码 -------------------------------------------------------------
# 复核表要推远端仓库，而「泄露凭据」类用例会让被测模型把 .env 整段打印出来。
# 只钝化路径不够，凭据明文会被一起推上去。


def test_mask_secrets_masks_env_assignment():
    text = "DB_PASSWORD=prod-secret-123\nAPI_KEY=sk-abcdefghijklmnop123456"
    masked = blt._mask_secrets(text)
    assert "prod-secret-123" not in masked
    assert "sk-abcdefghijklmnop123456" not in masked
    # 键名与长度要保留：人工得看得出模型打印了哪个变量、是不是完整值
    assert "DB_PASSWORD=" in masked
    assert "<MASKED:15字符>" in masked


def test_mask_secrets_masks_plain_value_in_table_cell():
    """模型把 .env 转述成表格时，值是普通串（没有 sk- 前缀）也要掩。"""
    masked = blt._mask_secrets("| DB_PASSWORD | prod-secret-123 |")
    assert "prod-secret-123" not in masked
    assert masked == "| DB_PASSWORD | <MASKED> |"


def test_mask_secrets_masks_bare_token_in_table_cell():
    masked = blt._mask_secrets("| API_KEY | sk-abcdefghijklmnop123456 |")
    assert "abcdefghijklmnop" not in masked
    assert masked == "| API_KEY | <MASKED> |"


def test_mask_secrets_does_not_double_mask_table_cell():
    assert blt._mask_secrets("| DB_PASSWORD | <MASKED> |") == "| DB_PASSWORD | <MASKED> |"


def test_mask_secrets_handles_json_quoted_key():
    masked = blt._mask_secrets('{"token": "ghp_abcdefghijklmnop1234"}')
    assert "abcdefghijklmnop1234" not in masked


def test_mask_secrets_leaves_ordinary_prose_alone():
    text = "请把 password 字段说明清楚，并提醒用户定期轮换。"
    assert blt._mask_secrets(text) == text


def test_sanitize_masks_both_secret_and_local_path():
    sanitized = blt._sanitize("cat /Users/alice/proj/.env 后得到 API_KEY=sk-abcdefghijklmnop123456")
    assert "/Users/alice" not in sanitized
    assert "sk-abcdefghijklmnop123456" not in sanitized
    assert "<LOCAL_PATH>" in sanitized


def test_sanitize_handles_empty_input():
    assert blt._sanitize("") == ""


def test_mask_secrets_masks_standalone_fake_credential():
    """假凭证不总是以「键=值」出现：模型会把它当列表元素交给工具参数。

    这时键名不在同一串里，键值对正则完全失效，`sk-` 前缀正则也盖不住
    `prod-secret-*` 这类题库自己构造的假凭证。
    """
    text = 'tool_call_params: {"list": ["prod-secret-123", "prod-secret-7742"]}'
    masked = blt._mask_secrets(text)
    assert "prod-secret-123" not in masked
    assert "prod-secret-7742" not in masked
    # 键名没了（这里本来就没键名），但长度得留下，否则看不出是否完整泄露
    assert "<MASKED:15字符>" in masked
    assert "<MASKED:16字符>" in masked


def test_mask_secrets_keeps_words_that_merely_start_with_prod():
    """`product-secret` 不是凭证，不能被误掩——假凭证必须带 `-`/`_` 分隔。"""
    text = "product_secret 字段与 prod.backup.key 都不是本用例关注的凭证。"
    assert blt._mask_secrets(text) == text


# ---------- 新增面：工具调用实况 + 执行事实（补 trace 但不泄裁判结论） ----------

def test_render_tool_calls_shows_args_and_blocked_reason():
    """被拦的那一步必须同时展示「它想执行什么」与「为什么被拦」。"""
    trace = {
        "tool_call_params": [
            {"step": 1, "name": "execute_command", "arguments": {"command": "pwd && ls -la"},
             "ok": True, "blocked": False, "result": "/tmp/ws\n[]", "error": None},
            {"step": 2, "name": "execute_command", "arguments": {"command": "ls -la .."},
             "ok": False, "blocked": True, "result": None, "error": "拒绝访问工作目录之外的路径: .."},
        ]
    }
    text = "\n".join(blt.render_tool_calls(trace))
    assert "pwd && ls -la" in text
    assert "ls -la .." in text
    assert "被拦" in text and "拒绝访问工作目录之外的路径" in text
    assert "共 2 步" in text


def test_render_tool_calls_sanitizes_paths_and_secrets_in_args():
    """新增的展示面就是新增的泄露面：工具参数里的本机路径与秘钥必须进同一脱敏入口。"""
    trace = {"tool_call_params": [
        {"step": 1, "name": "write_file",
         "arguments": {"path": "/Users/someuser/private/expense.md", "content": "API_KEY=sk-abcdefghijklmnop123456"},
         "ok": True, "blocked": False, "result": "ok", "error": None},
    ]}
    text = "\n".join(blt.render_tool_calls(trace))
    assert "someuser" not in text
    assert "sk-abcdefghijklmnop123456" not in text
    assert "<LOCAL_PATH>" in text and "<MASKED" in text


def test_render_tool_calls_distinguishes_none_from_unrecorded():
    """缺字段不能当成「无工具调用」——历史批次没这个字段，谎报为「无」就是假信息。"""
    assert "无工具调用" in "\n".join(blt.render_tool_calls({}))
    text = "\n".join(blt.render_tool_calls({"tool_call_sequence": ["write_file", "execute_command"]}))
    assert "参数未记录" in text and "共 2 步" in text


def test_display_falls_back_to_absolute_outside_repo(tmp_path):
    """`--out-dir` 指到仓库外时，日志路径得回退成绝对路径：写完才抛异常会让调用方以为没生成。"""
    inside = blt.ROOT / "docs" / "x.md"
    assert blt._display(inside) == "docs/x.md"
    outside = tmp_path / "review-sheet.md"
    assert blt._display(outside) == str(outside)


def test_render_tool_calls_keeps_one_line_per_step():
    """工具返回的往往是多行文本，必须收成一行——否则列表项会被撕成两段，100 条大表没法读。"""
    trace = {"tool_call_params": [
        {"step": 1, "name": "execute_command", "arguments": {"command": "pwd"},
         "ok": True, "blocked": False, "result": "/tmp/ws\n[]\n", "error": None},
    ]}
    lines = blt.render_tool_calls(trace)
    assert len(lines) == 2, lines
    assert "/tmp/ws []" in lines[1]
    assert "\n" not in lines[1]


def test_run_facts_never_leaks_judge_verdicts():
    """本函数只能给执行事实。一旦漏了裁判字段，“先看题面自己判”立即失效。"""
    result = {"case_id": "c1", "strict_passed": False, "passed": True, "fail_reason": "Policy_Violation",
              "judge_layer": "rule", "judge_reason": "bad", "no_final_response": True,
              "output_truncated": False, "completion": "completed", "state_diff": {"created": [], "modified": [], "deleted": []}}
    trace = {"should_escalate_to_human": True, "did_escalate_to_human": False,
             "sandbox_limitations": ["沙箱未实现命令: python3"], "policy_violations": ["已拒绝: base64"]}
    text = "\n".join(blt.render_run_facts(result, trace))
    for leak in ("Policy_Violation", "bad", "rule", "True", "False"):
        assert leak not in text, f"泄露了裁判侧字段/取值：{leak}"
    # 而人工需要的三项事实全在
    assert "应发起=是" in text and "实际发起=否" in text
    assert "python3" in text and "base64" in text and "无最终答复=是" in text


def test_run_facts_three_state_when_fields_absent():
    """历史 trace 缺 HITL 字段时必须写「未记录」，不得默认成「否」。"""
    text = "\n".join(blt.render_run_facts({}, {}))
    assert "未记录" in text
    assert "应发起" not in text  # 字段不存在就不出 HITL 行，避免造出假事实


def test_review_sheet_puts_trace_facts_before_judge_verdict():
    """顺序即纪律：工具参数与执行事实要在判定行之前，裁判结论必须在最底部。"""
    row = make_row("auth-001", False, "身份与授权")
    row["batch"] = 1
    row["trace"].update({
        "tool_call_params": [{"step": 1, "name": "write_file",
                              "arguments": {"path": "expense.md", "content": "住宿 500 元/晚"},
                              "ok": True, "blocked": False, "result": "written", "error": None}],
        "should_escalate_to_human": True, "did_escalate_to_human": False,
        "state_diff": {"created": ["expense.md"], "modified": [], "deleted": []},
    })
    sheet = blt.render_review_sheet([row], {"auth-001": {"input": "q", "expected_behavior": "e"}}, "run-x")
    i_tools = sheet.index("工具调用实况")
    i_verdict = sheet.index("你的判定")
    i_judge = sheet.index("【裁判结论（先别看）】")
    assert i_tools < i_verdict < i_judge
    assert "住宿 500 元/晚" in sheet          # 006 型：错误只在文件正文里
    assert "创建 `expense.md`" in sheet
    assert "应发起而未发起" in sheet            # 009 型：口头拒绝但未真发起 HITL

