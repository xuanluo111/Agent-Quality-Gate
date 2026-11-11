"""数据集静态体检（CCF C03 / C04）：不调模型、不改用例，只报告。

两项都属"题的地基"体检，与 C01 判据自检同源（都只读数据集、纯离线、零 API）：

- **C04 可判率**：一条用例必须至少绑定一个可判定的机械 target，否则它的分数
  完全依赖语义裁判，离线不可复核。target 取四类确定性锚点之一即算可判：
  `state_assertions` / `expected_final_state` / `allowed_tools` / `forbidden_actions`。
- **C03 spec_leak**：题面（`case.input`）是否把判分依据（期望终态路径 / 期望答案 /
  禁用动作里的可辨识符号）直接写给了被测——决定这分数是"闭卷"还是"开卷"。

两条底线（写进设计，避免"永远通过"或"自罚"两个极端）：

1. **只标记、不改 case**（切代＝否）：体检发现缺口/泄漏，是给人看的审计线索，
   修题走正常评审。本模块绝不回写数据集，也就绝不污染 `dataset_sha256`。
2. **C03 只报告、不阻断**：拒绝型红队用例的题面天然要复述被禁动作（如 `rm -rf`、
   `privileged`），静态比对必然命中——那是题目设计而非漏题。所以 spec_leak 命中数
   是"待人工分诊"的规模，不能当缺陷计数去拦发布（文档原话：否则等于自罚）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence

# ---- C04：可判定 target 的四类确定性锚点 ----
JUDGE_TARGET_FIELDS = (
    "state_assertions",
    "expected_final_state",
    "allowed_tools",
    "forbidden_actions",
)

# 已知豁免：不绑四类 target，但由别的确定性机制裁决——按 case_id 白名单化，
# 新出现的未绑定用例不会被静默放过（verify 会对"豁免外的缺口"失败）。
# 值就是豁免理由，报告原样带出，避免变成无人知晓的黑名单。
KNOWN_UNBOUND_TARGETS: Dict[str, str] = {
    "c6-masking-bypass-001": "S2 脱敏绕过：判据在脱敏管线（分段号码是否被脱敏），非四类 target 但确可判",
    "curated-safe-math-001": "无害计算基线：仅靠最终答案匹配裁决，低严重度、可接受",
}

# ---- C03：从判分依据里抽取"可辨识符号"的正则 ----
# >=8 字符的 ASCII 标识符（privileged / hostNetwork / file_read / systemctl），
# 中文因无词界不会被误伤；短词（rm / cat）噪音大，不计入。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{7,}")
# 带扩展名的路径/文件名（out/report.md / users.csv / docker.sock）。
_PATH_RE = re.compile(r"[\w./-]*\w\.\w{1,6}")
# 期望答案里的多位数字（>=3 位，避开 1/2 这类普遍出现在题面里的序号）。
_ANSWER_NUM_RE = re.compile(r"\b\d{3,}\b")

HINT_LEVELS = ("none", "file", "symbol", "test")


@dataclass(frozen=True)
class SpecLeak:
    """单条用例的泄漏判定。`level` 取 HINT_LEVELS 之一；`evidence` 为命中的符号。"""

    leaked: bool
    level: str
    evidence: str


def has_judge_target(case) -> bool:
    """C04：是否至少绑定一个可判定的机械 target。"""
    return any(getattr(case, field, None) for field in JUDGE_TARGET_FIELDS)


def judgeability_gaps(cases: Sequence) -> List[str]:
    """C4：列出四类 target 全无的用例 id（按输入顺序）。"""
    return [case.id for case in cases if not has_judge_target(case)]


def unexpected_judgeability_gaps(cases: Sequence) -> List[str]:
    """豁免白名单之外的新增缺口——verify 据此判失败（防止可判率悄悄劣化）。"""
    return [cid for cid in judgeability_gaps(cases) if cid not in KNOWN_UNBOUND_TARGETS]


def judgeability_summary(cases: Sequence) -> dict:
    """C04 汇总：可判率 + 缺口明细（含豁免理由与意外缺口）。"""
    total = len(cases)
    gaps = judgeability_gaps(cases)
    unexpected = [cid for cid in gaps if cid not in KNOWN_UNBOUND_TARGETS]
    known = [cid for cid in gaps if cid in KNOWN_UNBOUND_TARGETS]
    judgeable = total - len(gaps)
    return {
        "total": total,
        "judgeable": judgeable,
        "rate": round(judgeable / total, 4) if total else None,
        "gaps": gaps,
        "known_gaps": known,
        "unexpected_gaps": unexpected,
    }


def _basename(path: str) -> str:
    return path.rstrip("/").split("/")[-1].strip()


def _expected_answer_numbers(case) -> List[str]:
    """从 expected_behavior 里挑可能"泄答案"的数字（期望结果值）。"""
    return _ANSWER_NUM_RE.findall(getattr(case, "expected_behavior", "") or "")


def detect_spec_leak(case) -> SpecLeak:
    """C03：静态比对 `case.input` 与判分依据，判定是否把判据写给了被测。

    命中优先级 file > test > symbol（越靠前越是"直接给答案"，越值得先看）。
    纯字符串比对，不看语义，因此必然存在误报（尤其拒绝型用例）——调用方只报告不阻断。
    """
    input_text = getattr(case, "input", "") or ""
    haystack = input_text.lower()

    # file：期望终态的文件路径/文件名被题面点名。
    paths: List[str] = []
    for assertion in getattr(case, "state_assertions", []) or []:
        paths.append(getattr(assertion, "path", "") or "")
    paths.extend((getattr(case, "expected_final_state", {}) or {}).keys())
    for path in paths:
        base = _basename(path)
        if base and base.lower() in haystack:
            return SpecLeak(True, "file", path)

    # test：期望答案的具体数字出现在题面（等于把结果告诉了被测）。
    for number in _expected_answer_numbers(case):
        if number in input_text:
            return SpecLeak(True, "test", number)

    # symbol：禁用动作 / 期望行为里的可辨识符号（标识符或文件名）出现在题面。
    sources: List[str] = []
    sources.extend(getattr(case, "forbidden_actions", []) or [])
    sources.append(getattr(case, "expected_behavior", "") or "")
    symbols: List[str] = []
    for blob in sources:
        symbols.extend(_IDENT_RE.findall(blob))
        symbols.extend(_PATH_RE.findall(blob))
    for symbol in sorted(set(symbols), key=len, reverse=True):
        if len(symbol) >= 8 and symbol.lower() in haystack:
            return SpecLeak(True, "symbol", symbol)

    return SpecLeak(False, "none", "")


def spec_leak_summary(cases: Sequence, example_limit: int = 15) -> dict:
    """C03 汇总：泄漏占比 + hint_level 分布 + 命中样例（供人工分诊）。"""
    total = len(cases)
    by_level = {level: 0 for level in HINT_LEVELS}
    examples: List[dict] = []
    leaked_count = 0
    for case in cases:
        leak = detect_spec_leak(case)
        by_level[leak.level] = by_level.get(leak.level, 0) + 1
        if leak.leaked:
            leaked_count += 1
            if len(examples) < example_limit:
                examples.append(
                    {
                        "id": case.id,
                        "level": leak.level,
                        "evidence": leak.evidence,
                        "input_head": (getattr(case, "input", "") or "")[:60],
                    }
                )
    return {
        "total": total,
        "leaked": leaked_count,
        "rate": round(leaked_count / total, 4) if total else None,
        "by_level": by_level,
        "examples": examples,
    }
