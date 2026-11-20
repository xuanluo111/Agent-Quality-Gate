"""入库前置单测：check_keys.py（取 key 链路）与 judge_token_monitor.py（token 外推）。

纪律来源：08 待拍板第 7 行——两份脚本按「含业务逻辑必须配套单测」刻意留白未入库。
覆盖口径：
- check_keys 只测「取 key 路径与 run_batch 一致」，**不打网络**（probe 的网络分支不测）；
- judge_token_monitor 用钉桩 results.jsonl 测累计、坏行容忍与池外推。
"""
import json

import pytest

import check_keys
import judge_token_monitor as jtm
import run_batch


# ---------------------------------------------------------------------------
# check_keys.resolve_secret：与 run_batch 同逻辑（08 第 7 行承诺的验证点）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec,env_name,env_value",
    [
        ({"name": "m", "api_key": "sk-direct"}, "AQG_TEST_KEY", "sk-from-env"),
        ({"name": "m", "api_key_env": "AQG_TEST_KEY"}, "AQG_TEST_KEY", "sk-from-env"),
        ({"name": "m", "api_key": "  ", "api_key_env": "AQG_TEST_KEY"}, "AQG_TEST_KEY", " sk-env \t"),
        ({"name": "m"}, "AQG_TEST_KEY", "ignored"),
        ({"name": "m", "api_key_env": "MISSIVE"}, "AQG_TEST_KEY", "ignored"),
    ],
)
def test_resolve_secret_matches_run_batch_on_common_paths(monkeypatch, spec, env_name, env_value):
    """四种主路径（直接值优先 / env 指向 / 空值回落 / 无配置）两份实现必须逐值相等。"""
    monkeypatch.setenv(env_name, env_value)
    monkeypatch.delenv("MISSIVE", raising=False)
    got = check_keys.resolve_secret(spec, "api_key")
    want = run_batch.resolve_secret(spec, "api_key", required=False)
    assert got == want


def test_resolve_secret_empty_env_returns_empty_instead_of_exiting(monkeypatch):
    """有意的差异：run_batch(required=True) 空 env 直接 SystemExit，自检脚本要逐项报告而非崩。"""
    monkeypatch.delenv("AQG_TEST_KEY", raising=False)
    spec = {"name": "m", "api_key_env": "AQG_TEST_KEY"}
    assert check_keys.resolve_secret(spec, "api_key") == ""
    with pytest.raises(SystemExit):
        run_batch.resolve_secret(spec, "api_key", required=True)


def test_probe_local_boundaries_no_network(monkeypatch):
    """probe 的两条本地边界（key 空 / base_url 空）不得发出请求。"""

    def no_urlopen(*a, **kw):  # pragma: no cover - 被拦则根本不会调用
        raise AssertionError("单测不允许打网络")

    monkeypatch.setattr(check_keys.urllib.request, "urlopen", no_urlopen)
    ok, msg = check_keys.probe("https://example.invalid", "", "m")
    assert ok is False and "key 为空" in msg
    ok, msg = check_keys.probe("", "sk-x", "m")
    assert ok is False and "base_url 为空" in msg


def test_main_uses_same_sut_key_fallback_and_filters_only(monkeypatch, tmp_path):
    """main 的 SUT 取 key 须与 run_batch.child_env 同链（api_key_env → AQG_API_KEY → GC_API_KEY）；
    --only 过滤与退出码 0/1 是可脚本化 gating 的契约。"""
    matrix = tmp_path / "models.json"
    matrix.write_text(json.dumps({"models": [
        {"name": "a", "model": "ma", "base_url": "https://a.invalid", "api_key_env": "AQG_TEST_A"},
        {"name": "b", "model": "mb", "base_url": "https://b.invalid", "api_key_env": "AQG_TEST_B"},
    ]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # ROOT/.env 不存在 → load_dotenv 无副作用
    monkeypatch.delenv("AQG_TEST_A", raising=False)
    monkeypatch.delenv("AQG_API_KEY", raising=False)  # 真实 shell 可能有值，不清会顶掉 GC 回落链
    monkeypatch.setenv("AQG_TEST_B", "sk-b")
    monkeypatch.setenv("GC_API_KEY", "sk-fallback")

    seen = []

    def fake_probe(base_url, key, model):
        seen.append((base_url, key, model))
        # 真实 probe 对 key 空短路返 False，这里同样不返假绿
        return (False, "key 为空") if not key else (True, "ok")

    monkeypatch.setattr(check_keys, "probe", fake_probe)
    rc = check_keys.main(["--matrix", str(matrix), "--only", "a,b", "--no-judge"])
    assert rc == 0
    # a: api_key_env 空 → 回落 AQG_API_KEY（未设）→ GC_API_KEY；b: 用自身 env
    assert [s[1] for s in seen] == ["sk-fallback", "sk-b"]

    monkeypatch.delenv("GC_API_KEY")
    monkeypatch.delenv("AQG_API_KEY", raising=False)
    rc = check_keys.main(["--matrix", str(matrix), "--only", "a", "--no-judge"])
    assert rc == 1  # key 拿不到 → 必红，供开跑前 gating


# ---------------------------------------------------------------------------
# judge_token_monitor：钉桩 results.jsonl
# ---------------------------------------------------------------------------

def write_results(run_dir, rows, trailing_garbage=False):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
        if trailing_garbage:
            f.write('{"result": {"token_cost": 100')  # 模拟批写了一半的末行


def test_case_token_cost_sums_and_skips_partial_last_line(tmp_path):
    p = tmp_path / "results.jsonl"
    p.write_text(
        json.dumps({"result": {"token_cost": 120}}) + "\n"
        + json.dumps({"result": {}}) + "\n"          # 缺字段按 0 计
        + '{"result": {"token_cost": 999',           # 半行必须跳过且不计入样本点
        encoding="utf-8")
    total, n = jtm.case_token_cost(str(p))
    assert (total, n) == (120, 2)


def test_find_batch_runs_filters_by_stamp_and_skips_missing_results(tmp_path, monkeypatch):
    (tmp_path / "20260101-000000-modelA").mkdir()
    write_results(tmp_path / "20260101-000000-modelB", [{"result": {"token_cost": 1}}])
    (tmp_path / "attack-probe").mkdir()  # 非批次前缀必须被过滤
    monkeypatch.setattr(jtm, "RUNS", tmp_path)
    runs = jtm.find_batch_runs("20260101-000000")
    assert [name for name, _ in runs] == ["modelB"]  # modelA 无 results.jsonl 不收录


def test_latest_batch_ignores_non_timestamp_dirs(tmp_path, monkeypatch):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "batch-attack-probe").mkdir()
    (tmp_path / "reports" / "batch-20260101-000000").mkdir()
    monkeypatch.setattr(jtm, "ROOT", tmp_path)
    assert jtm.latest_batch() == "20260101-000000"
    (tmp_path / "reports" / "batch-20260101-000000").rmdir()
    assert jtm.latest_batch() == ""


def _pin_runs(monkeypatch, tmp_path, costs):
    """costs: {model: (每点 token, 已完成点数)}，生成各 run 的 results.jsonl 并把 RUNS 指过去。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    for name, (per_pt, n) in costs.items():
        write_results(runs / f"20260101-000000-{name}",
                      [{"result": {"token_cost": per_pt}}] * n)
    monkeypatch.setattr(jtm, "RUNS", runs)
    monkeypatch.setattr(jtm, "ROWS_PER_MODEL", 207)


def test_render_projects_from_completed_models_average(monkeypatch, tmp_path):
    # 一个模型跑满 207 点 × 100 token，另一个跑 7 点：外推必须按「完整模型均单价」而非在跑样本
    _pin_runs(monkeypatch, tmp_path, {"cheap": (100, 207), "loud": (1_000_000, 7)})
    out = jtm.render("20260101-000000", pool=50_000_000, expected_models=2)
    assert "已完成模型：1/2" in out
    assert "41,400" in out  # 预计总量按完整模型均单价 20,700 × 2，不含在跑模型的贵样本
    assert "✅ 充裕" in out


def test_render_projects_from_per_point_mean_when_nothing_completed(monkeypatch, tmp_path):
    _pin_runs(monkeypatch, tmp_path, {"loud": (1_000_000, 7)})
    out = jtm.render("20260101-000000", pool=1_000, expected_models=1)
    assert "已完成模型：0/1" in out
    assert "每点均值" in out
    assert "❌ 预计超池" in out  # 7×1e6 = 7M 外推 >> 池 1000


def test_render_no_data_reports_zero_projection(monkeypatch, tmp_path):
    monkeypatch.setattr(jtm, "RUNS", tmp_path / "missing")
    out = jtm.render("20260101-000000", pool=50_000_000, expected_models=9)
    assert "预计总量：0" in out and "尚无数据" in out
