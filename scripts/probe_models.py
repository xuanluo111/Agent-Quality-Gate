#!/usr/bin/env python3
"""重跑前的端点健康探针：对矩阵里每个模型发 1 次极小请求，只看是否返回内容。

用途：批次跑完若有模型被判 invalid（环境故障、空返回），重跑前先探一下
该模型端点当前是否恢复，避免又白烧几小时。Key 从矩阵 api_key_env 指向的
环境变量读取（与 run_batch 一致），本脚本不打印任何明文密钥。

用法：
    .venv/bin/python scripts/probe_models.py [matrix.json]   # 默认 models.rerun2.json
退出码：全部有内容返回=0；有任一空返回/失败=1。
"""
import json
import os
import sys
import urllib.request

MATRIX = sys.argv[1] if len(sys.argv) > 1 else "models.rerun2.json"


def load_env_file():
    """尽量加载 .env 作为兜底（key 通常已在 shell 里 export）。"""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass


def probe(name, model, base_url, key):
    if not key:
        return (False, "环境变量未设置该 Key（请在你的终端 export 后再跑）")
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "reply with the single word OK"}],
        "max_tokens": 1024,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.load(r)
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content", "") or ""
        reasoning = msg.get("reasoning_content", "") or ""
        finish = choice.get("finish_reason", "")
        ok = bool(content.strip())
        detail = "HTTP {} | content_len={} | finish_reason={} | reasoning_len={} | 预览={!r}".format(
            r.status, len(content), finish, len(reasoning), content[:30])
        if not ok and reasoning.strip():
            detail += "  (正文空但有思维链)"
        return (ok, detail)
    except Exception as e:  # noqa: BLE001 - 探针要吞掉一切异常并如实报告
        return (False, "{}: {}".format(type(e).__name__, str(e)[:160]))


def main():
    load_env_file()
    with open(MATRIX, encoding="utf-8") as f:
        matrix = json.load(f)
    all_ok = True
    for m in matrix.get("models", []):
        key = os.getenv(m.get("api_key_env", ""), "")
        ok, msg = probe(m["name"], m["model"], m["base_url"], key)
        all_ok = all_ok and ok
        print("[{}] {}  ({})\n      {}".format("健康" if ok else "异常", m["name"], m["model"], msg))
    print("\n=> 全部端点健康，可以重跑" if all_ok else "\n=> 有端点异常，先修复再重跑（否则仍会 invalid）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
