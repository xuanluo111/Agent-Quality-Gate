"""C10 软件供应链完整性校验（离线、可单测、不联网）。

给 ISACA「C10 模型·供应商·软件供应链」提供一个真正可判定的框架侧锚点，而不是
停留在「我们有 requirements.txt」这种概念名词。它核验三件事：

1. 锁定覆盖：requirements.txt 里声明的每个直接依赖，都必须在 requirements.lock 以
   `==` 精确出现——只有区间约束等于没锁，传递依赖仍会随上游发版漂移。
2. 锁定纯净：requirements.lock 里不允许残留 `>=`/`~=`/无版本 之类非精确条目，
   否则 pip-audit / SBOM 的口径与实装口径对不上。
3. 版本一致：锁定版本必须满足 requirements.txt 声明的下界（`>=x.y.z`），防止有人
   把 lock 手改到低于代码所依赖的版本。

CI 里先跑本脚本、再跑 pip-audit、最后 cyclonedx 从同一份 lock 出 SBOM，三步同源。
本脚本自身完全离线：只做文本解析与版本比较，不访问网络、不安装任何包。

用法：
    python scripts/check_supply_chain.py                 # 用默认路径
    python scripts/check_supply_chain.py --lock requirements.lock --requirements requirements.txt
退出码 0=通过，1=发现问题（打印明细）。
"""
import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# requirements.txt / lock 里一行依赖的解析：包名 + 可选的版本约束。
# 允许行尾注释（`# ...`）、环境标记（`; python_version ...`，本项目未用但容错）与空行。
_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*(.*)$")
# 约束串里抽出第一个比较符与版本号，例如 ">=1.30.0" -> (">=", "1.30.0")。
_SPEC_RE = re.compile(r"(>=|==|~=|<=|>|<)\s*([0-9][A-Za-z0-9_.\-]*)")


def normalize(name: str) -> str:
    """PEP 503 规范化包名：小写，`_`/`.`/连续 `-` 归一，便于跨大小写比对。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _release_tuple(version: str) -> Tuple[int, ...]:
    """把版本号的数字前缀取成可比较的整数元组（"2.9.0.post0" -> (2,9,0)）。

    只做前缀数值比较，够用于「锁定版本不低于声明下界」这类完整性判断；刻意不实现
    完整 PEP 440（post/dev/local 段不参与下界比较），避免引入 packaging 依赖。
    """
    parts: List[int] = []
    for chunk in re.findall(r"\d+", version):
        parts.append(int(chunk))
        if len(parts) >= 3:
            break
    return tuple(parts or [0])


def parse_requirements(text: str) -> List[Tuple[str, str, str]]:
    """解析区间/固定声明，返回 [(规范化名, 原始名, 下界版本或空)]。"""
    entries: List[Tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # 跳过空行、注释、-r/-e 等选项行
            continue
        match = _REQ_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        spec = _SPEC_RE.search(match.group(3) or "")
        lower = spec.group(2) if spec and spec.group(1) in {">=", "==", "~="} else ""
        entries.append((normalize(name), name, lower))
    return entries


def _is_full_pin(constraint: str) -> bool:
    """确认整段约束就是一个裸 `==` 精确值（排除 `===` 宽松固定或多约束叠加）。"""
    return re.fullmatch(r"\s*==\s*[0-9][A-Za-z0-9_.\-]*\s*", constraint) is not None


def parse_lock_entries(text: str) -> Tuple[Dict[str, str], List[str]]:
    """返回 ({规范化名: 版本}, 非精确条目列表)。锁定清单必须全是 `pkg==version`。"""
    pinned: Dict[str, str] = {}
    unpinned: List[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_RE.match(line)
        if not match:
            unpinned.append(line)
            continue
        name, constraint = match.group(1), (match.group(3) or "")
        spec = _SPEC_RE.search(constraint)
        if spec and spec.group(1) == "==" and _is_full_pin(constraint):
            pinned[normalize(name)] = spec.group(2)
        else:
            unpinned.append(line)
    return pinned, unpinned


def check(requirements_text: str, lock_text: str) -> dict:
    """核心校验：把两份文本喂进来，出一份结构化报告（纯函数，便于单测）。"""
    requirements = parse_requirements(requirements_text)
    pinned, unpinned = parse_lock_entries(lock_text)

    missing = [orig for norm, orig, _lower in requirements if norm not in pinned]
    unpinned_reqs = [line for line in unpinned]
    bound_violations = []
    for norm, orig, lower in requirements:
        if not lower or norm not in pinned:
            continue
        if _release_tuple(pinned[norm]) < _release_tuple(lower):
            bound_violations.append(
                {"package": orig, "locked": pinned[norm], "requires_at_least": lower}
            )

    return {
        "ok": not (missing or unpinned_reqs or bound_violations),
        "direct_requirements": len(requirements),
        "pinned_in_lock": len(pinned),
        "missing_from_lock": missing,
        "unpinned_lines": unpinned_reqs,
        "bound_violations": bound_violations,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 C10 供应链锁定清单完整性（离线）")
    parser.add_argument("--requirements", default="requirements.txt")
    parser.add_argument("--lock", default="requirements.lock")
    args = parser.parse_args(argv)

    req_path, lock_path = Path(args.requirements), Path(args.lock)
    if not lock_path.is_file():
        print(f"FAIL: 锁定清单不存在：{lock_path}（先 pip freeze > {lock_path}）")
        return 1
    report = check(req_path.read_text(encoding="utf-8"), lock_path.read_text(encoding="utf-8"))

    if report["ok"]:
        print(
            f"OK: 供应链锁定一致——{report['direct_requirements']} 个直接依赖全部精确锁定，"
            f"lock 共 {report['pinned_in_lock']} 项且无非精确条目。"
        )
        return 0

    print("FAIL: 供应链锁定存在问题")
    for name in report["missing_from_lock"]:
        print(f"  - 直接依赖未在 lock 精确锁定：{name}")
    for line in report["unpinned_lines"]:
        print(f"  - lock 含非精确条目（应形如 pkg==version）：{line}")
    for item in report["bound_violations"]:
        print(
            f"  - 锁定版本低于声明下界：{item['package']} "
            f"locked={item['locked']} 需 >= {item['requires_at_least']}"
        )
    return 1


if __name__ == "__main__":
    if str(Path(__file__).resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
