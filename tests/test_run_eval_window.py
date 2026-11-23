"""run_eval 的跨窗口接线（T32-4）判据测试。

要钉住的三件事，每件都对应一种"看起来在跑协议、其实没跑"的错法：
- 只传 `--window` 不传 manifest（或反之）必须立刻拒——半套参数会被读成"启用了一半协议"；
- **裁剪必须发生在抽样之前**：顺序反了，未来题会先进样本再被裁掉；更糟的是抽样恰好
  没抽到未来题，于是违规静默通过，报告显示一切正常；
- 窗口审计要进 run_meta，且带 manifest 指纹：只记路径等于允许事后改写切片定义。

跑的都是 `--dry-run`，不调任何模型；输出目录指到 tmp，绝不污染 reports/ 与 workspace/。
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pytest

from run_eval import _window_protocol_meta, main

from src.config import RunConfig, add_run_args, config_from_args
from tests.conftest import make_case


def _config(*argv: str):
    parser = argparse.ArgumentParser()
    add_run_args(parser)
    return config_from_args(parser.parse_args(["--suite", "smoke", *argv]))


def _write_dataset(tmp_path: Path, ids, name: str = "suite.jsonl") -> Path:
    path = tmp_path / name
    lines = []
    for case_id in ids:
        # input 必须逐条不同：`load_cases(dedupe=True)` 会按 input 哈希去重，
        # 共用同一句话会让后两条直接消失，测到的就是“去重”而不是“窗口裁剪”。
        case = make_case(
            id=case_id,
            input=f"[{case_id}] 请读取 users.csv 并计算 age 的平均值。",
            expected_behavior=f"[{case_id}] 读取文件并给出结果。",
        )
        lines.append(json.dumps(case.model_dump(), ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_manifest(tmp_path: Path, corpus, windows) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "protocol": "aqg-cross-window/1",
                "corpus": str(corpus),
                "metric": "strict_pass_rate",
                "verified": True,
                "windows": [
                    {"window": number, "cutoff": cutoff, "declared": list(ids)}
                    for number, cutoff, ids in windows
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _main_dry(tmp_path: Path, argv) -> int:
    """在 tmp_path 下以 dry-run 调 main()：绝对路径 + 临时 cwd，双重保证零落盘污染。"""
    argv = [
        *argv,
        "--dry-run",
        "--no-publish",
        "--output-root",
        str(tmp_path / "runs"),
        "--workspace-root",
        str(tmp_path / "ws"),
    ]
    saved_argv, saved_cwd = sys.argv, os.getcwd()
    os.chdir(tmp_path)
    sys.argv = ["run_eval.py", *argv]
    try:
        return main()
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


# --------------------------------------------------------------------------
# 参数交叉校验
# --------------------------------------------------------------------------


def test_window_without_manifest_is_rejected():
    """只给窗口号没有 manifest，等于"我要切片但没定义切片"——不许猜。"""
    with pytest.raises(SystemExit, match="窗口"):
        _config("--window", "1")


def test_manifest_without_window_is_rejected():
    with pytest.raises(SystemExit, match="窗口"):
        _config("--window-manifest", "cases/windows/manifest.json")


def test_negative_window_number_is_rejected():
    """窗口号从 0 起；传负数又给了 manifest 属于"想启用但没指定哪一轮"。"""
    with pytest.raises(SystemExit, match="窗口"):
        _config("--window-manifest", "cases/windows/manifest.json", "--window", "-1")


def test_window_keys_land_in_metadata_snapshot(tmp_path):
    """两个键必须进 config.json 快照，否则事后无法还原本轮究竟看了哪些题。"""
    corpus = _write_dataset(tmp_path, ["a1"])
    manifest = _write_manifest(tmp_path, corpus, [(0, "2026-10-22", ["a1"])])
    config = RunConfig(
        dataset=str(corpus), window_manifest=str(manifest), window=0
    )
    metadata = config.metadata()
    assert metadata["window"] == 0
    assert metadata["window_manifest"] == str(manifest)


def test_protocol_defaults_off_when_not_declared():
    """不传就是没启用：默认值不能是"启用但窗口 0"，那会让存量批次全部走协议分支。"""
    config = _config()
    assert config.window == -1 and config.window_manifest == ""


# --------------------------------------------------------------------------
# 裁剪先于抽样
# --------------------------------------------------------------------------


def test_future_cases_in_request_fail_closed_even_with_tiny_limit(tmp_path, capsys):
    """`--limit 1` 下若先抽样，很可能抽不到未来题从而静默通过；这里要求它仍然报错。

    这条断言的意义是**顺序**，不是"少跑几道题"。
    """
    corpus = _write_dataset(tmp_path, ["a1", "a2", "b1"])
    manifest = _write_manifest(
        tmp_path, corpus, [(0, "2026-10-22", ["a1", "a2"]), (1, "2026-11-06", ["b1"])]
    )
    code = _main_dry(
        tmp_path,
        [
            "--dataset", str(corpus),
            "--window-manifest", str(manifest),
            "--window", "0",
            "--limit", "1",
        ],
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "跨窗口协议不成立" in out
    assert "未来" in out


def test_visible_only_request_passes_and_reports_isolation(tmp_path, capsys):
    """校验面是 manifest 声明的语料，本轮数据集视图只含已可见题 → 协议放行并打印隔离数。

    这一条同时护住 smoke 子集视图跑协议的场景：视图可以比语料小，但不能比可见集大。
    """
    corpus = _write_dataset(tmp_path, ["a1", "a2", "b1"], name="corpus.jsonl")
    view = _write_dataset(tmp_path, ["a1", "a2"])
    manifest = _write_manifest(
        tmp_path, corpus, [(0, "2026-10-22", ["a1", "a2"]), (1, "2026-11-06", ["b1"])]
    )
    code = _main_dry(
        tmp_path,
        ["--dataset", str(view), "--window-manifest", str(manifest), "--window", "0"],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "跨窗口协议: 窗口 0" in out
    assert "被隔离的未来题 0 条" in out


def test_broken_manifest_fails_closed_before_any_model_call(tmp_path, capsys):
    """manifest 引用了语料外的题：宁可本轮不跑，也不给不属于本代际的题背书。"""
    corpus = _write_dataset(tmp_path, ["a1"])
    manifest = _write_manifest(tmp_path, corpus, [(0, "2026-10-22", ["a1", "ghost"])])
    code = _main_dry(
        tmp_path,
        ["--dataset", str(corpus), "--window-manifest", str(manifest), "--window", "0"],
    )
    assert code == 1
    assert "跨窗口协议不成立" in capsys.readouterr().out


def test_missing_manifest_path_is_reported_not_tracebacked(tmp_path, capsys):
    corpus = _write_dataset(tmp_path, ["a1"])
    code = _main_dry(
        tmp_path,
        [
            "--dataset", str(corpus),
            "--window-manifest", str(tmp_path / "nope.json"),
            "--window", "0",
        ],
    )
    assert code == 1
    assert "跨窗口协议不成立" in capsys.readouterr().out


def test_unknown_protocol_version_fails_closed_in_run_path(tmp_path, capsys):
    """run_eval 不能因为"自己会再校验一遍"就跳过协议版本检查。"""
    corpus = _write_dataset(tmp_path, ["a1"])
    manifest = _write_manifest(tmp_path, corpus, [(0, "2026-10-22", ["a1"])])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["protocol"] = "aqg-cross-window/2"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    code = _main_dry(
        tmp_path,
        ["--dataset", str(corpus), "--window-manifest", str(manifest), "--window", "0"],
    )
    assert code == 1
    assert "协议版本" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 元数据
# --------------------------------------------------------------------------


def test_window_protocol_meta_carries_manifest_fingerprint(tmp_path):
    manifest = _write_manifest(tmp_path, tmp_path / "c.jsonl", [(0, "2026-10-22", ["a1"])])
    config = RunConfig(window_manifest=str(manifest), window=0)
    audit = {"window": 0, "asked": 3, "selected": 1, "dropped": 2}
    meta = _window_protocol_meta(config, audit)
    assert re.fullmatch(r"[0-9a-f]{16}", meta["manifest_sha256"])
    assert meta["dropped"] == 2 and meta["asked"] == 3
    # 改一个字节，指纹必须变——否则"事后能核对是哪版切片"就是空话。
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert _window_protocol_meta(config, audit)["manifest_sha256"] != meta["manifest_sha256"]
