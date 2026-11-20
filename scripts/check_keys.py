#!/usr/bin/env python3
"""开跑前 key 生效自检：复刻 run_batch 的取 key 逻辑，逐个 provider 打一次极小请求。

为什么需要它：`models.json` 的 `api_key_env` 指向的变量（GC_API_KEY/XHS_API_KEY…）
在 `.env` 里写的是 `${GC_API_KEY}` 形式，由 python-dotenv 从**当前 shell 环境插值**。
在非登录/未 export 的 shell 里这些值会是空串——本脚本用与 run_batch 相同的
`load_dotenv() + os.getenv(api_key_env)` 路径解析，因此这里绿了就代表批一定拿得到真 key。

用法：
    .venv/bin/python scripts/check_keys.py                    # 校验 models.json 全部模型 + 裁判
    .venv/bin/python scripts/check_keys.py --matrix models.attack_probe.json
    .venv/bin/python scripts/check_keys.py --only qwen4-max-preview,zhipu-glm-4.5-flash
退出码：全部可用=0；有任一不可用=1（便于脚本化 gating）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

# 与 run_batch 一致：加载 .env（含 ${VAR} 从 shell 环境插值）
load_dotenv(ROOT / ".env")


def resolve_secret(spec: dict, field: str) -> str:
    """与 run_batch.resolve_secret 同逻辑：直接值 > api_key_env 指向的环境变量。"""
    direct = str(spec.get(field) or "").strip()
    if direct:
        return direct
    env_name = str(spec.get(f"{field}_env") or "").strip()
    if not env_name:
        return ""
    return os.getenv(env_name, "").strip()


def probe(base_url: str, key: str, model: str) -> tuple[bool, str]:
    if not key:
        return False, "key 为空（检查当前 shell 是否已 export 对应变量）"
    if not base_url:
        return False, "base_url 为空"
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
            return True, f"model={data.get('model')} tokens={data.get('usage', {}).get('total_tokens')}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:160].decode('utf-8', 'ignore')}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="开跑前 key 生效自检（复刻 run_batch 取 key 逻辑）")
    ap.add_argument("--matrix", default="models.json", help="模型清单（默认 models.json）")
    ap.add_argument("--only", default="", help="只校验指定模型 name，逗号分隔；空=全部")
    ap.add_argument("--no-judge", action="store_true", help="不校验裁判")
    args = ap.parse_args(argv)

    matrix = json.loads((ROOT / args.matrix).read_text(encoding="utf-8"))
    models = matrix.get("models", [])
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        models = [m for m in models if m.get("name") in want or m.get("model") in want]

    ok_all = True
    print(f"清单：{args.matrix}｜共 {len(models)} 个 SUT\n=== SUT 侧 ===")
    for m in models:
        name = m.get("name") or m.get("model")
        key = resolve_secret(m, "api_key") or os.getenv("AQG_API_KEY", "") or os.getenv("GC_API_KEY", "")
        ok, msg = probe(m.get("base_url", ""), key, m.get("model", ""))
        ok_all &= ok
        print(f"  [{name:26s}] {'✅' if ok else '❌'} {msg}")

    if not args.no_judge:
        # 裁判：per-model 覆盖优先，否则回落 .env 全局
        print("=== 裁判侧 ===")
        seen: set[tuple[str, str]] = set()
        # 全局裁判
        jkey = os.getenv("AQG_JUDGE_API_KEY", "").strip()
        jbase = os.getenv("AQG_JUDGE_BASE_URL", "").strip()
        jmodel = os.getenv("AQG_JUDGE_MODEL", "").strip()
        ok, msg = probe(jbase, jkey, jmodel)
        ok_all &= ok
        print(f"  [{'全局.env裁判':24s}] {'✅' if ok else '❌'} model={jmodel} | {msg}")
        # 有 per-model 覆盖的（如 qwen/deepseek）单独再验一次
        for m in models:
            jm = str(m.get("judge_model") or "").strip()
            if not jm or jm == jmodel:
                continue
            jk = resolve_secret(m, "judge_api_key") or jkey
            jb = str(m.get("judge_base_url") or "").strip() or jbase
            sig = (jb, jm)
            if sig in seen:
                continue
            seen.add(sig)
            ok, msg = probe(jb, jk, jm)
            ok_all &= ok
            print(f"  [{m.get('name'):24s}] {'✅' if ok else '❌'} model={jm} | {msg}")

    print("\n结果：", "全部可用，可开跑 ✅" if ok_all else "存在不可用项，先修复再开跑 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
