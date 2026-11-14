"""case lint（快手 K13）：用例的四问齐备性检查，纯离线、零 API、只读。

四问（朱少民原话）：**做什么／背景／不能做什么／怎样算成功**。落到本项目字段时
**不能按字段存在性判定**——实测 `expected_final_state` 0/207、`initial_state` 1/207，
按存在性会直接报 202 条红，lint 立刻沦为噪音源。所以四问各自按语义判定：

| 四问 | 判据 | 存量实测 |
|---|---|---|
| 做什么 | `input` 非空、够长 | 0 条 |
| 背景 | 题面「读语境」引用的文件必须真的在 `fixtures` 里（引用自足性） | 0 条 |
| 不能做什么 | `forbidden_actions` 与 `allowed_tools` 至少一个非空 | 2 条 error |
| 怎样算成功 | `expected_behavior` 非空、够具体、含可观测锚点 | 6 条 warning |

三条设计底线：

1. **不改 case 数据**：lint 只读不写。存量缺陷走豁免清单
   （`docs/quality/case-lint-exemptions.json`），修题必须另立评审——否则
   `dataset_sha256` 一变就切代，11/19 基线与历史批不可比，直接违反 P0 的
   「不切代」硬约束。
2. **只认可判定的证据**：R4 不搜「合理／专业／准确」这类形容词——实测 8 条命中
   全是误报（「正确计算并给出 4751」「建议咨询专业医生」），形容词在描述**内容**
   而不是**判据**。改判「有没有可观测锚点」（拒绝／不得／写入／文件名／数字／
   工具名）。加否定式约束（不执行／不泄露）也算锚点：注入类用例的判据天然是否定式。
3. **warning 起步**：CI 里 `continue-on-error` 可见但不卡流水线；而 `verify.py`
   对**豁免外**的任何 finding 判失败——否则这套防退化机制等于没有。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---- 规则目录：编号 → 四问归属，报告里原样带出 ----
RULES: Dict[str, str] = {
    "R1": "做什么：题面非空且能识别任务意图",
    "R2": "背景：题面读语境引用的文件必须在 fixtures 里",
    "R3": "不能做什么：至少一种动作约束（黑名单或白名单）",
    "R4": "怎样算成功：期望行为够具体、含可观测锚点",
}
RULE_ORDER = ("R1", "R2", "R3", "R4")

ERROR = "error"
WARNING = "warning"

# ---- R1：题面最短长度。低于此值几乎不可能表达清任务 ----
MIN_INPUT_CHARS = 8
# ---- R4：期望行为最短长度。「拒绝」两字的老用例落在这里 ----
MIN_BEHAVIOR_CHARS = 15

# R2 读／写语境提示词。两者必须同时定义：只认读动词会把「把分析写进 summary.txt」
# 判成读缺口（「分析」就在窗口里），而写目标本就不该出现在 fixtures 里。
# 「用 X 查」「表在 X 里」这类隐式读先不收（宁可漏报，warning 起步阶段不制造噪音）。
_READ_HINTS = ("读取", "打开", "读一下", "读出", "查看", "分析", "统计", "解析", "载入", "导入", "读")
_WRITE_HINTS = (
    "写进", "写入", "写到", "写出", "输出", "保存", "生成", "存到", "放在",
    "改成", "改为", "记录", "导出", "追加", "覆盖",
)
# 紧邻文件名（前 3 字符）的方向词：命中即写语境，优先于动词远近比较。
# 解决「再写故障分析到 fault.txt」这类名词干扰——「分析」离得更近但不是动词。
# 不收「为」「至」：它们更多出现在「作为输入」「由于」里，会把真读语境误判成写。
_WRITE_DIRECTION = ("到", "进", "入", "成")

# R2 负向语境：题面就在说「这个文件不存在」，fixture 缺席是设计意图。
# 判在**文件名级**而不是整条用例级——很多用例别处写着「无法计算」，与引用是否成立无关。
_NEGATED_RE = re.compile(
    r"不存在|没有|缺失|无法|未提供|not\s+exist|non-?exist|missing|does\s+not|doesn't", re.I
)

# 带扩展名的文件引用。前导 `/` 一并捕获：否则 `/tmp/x.txt` 会从 `tmp/` 开始匹配，
# 绝对路径豁免（`startswith("/")`）永远不生效，实测误报 3 条。
# `(?<![\w.-])` 防止从 `data/notes.txt` 中间截断。
_FILE_REF_RE = re.compile(r"(?<![\w.-])(/?(?:[\w.-]+/)*[\w-]+\.[A-Za-z][A-Za-z0-9]{0,4})\b")
# 语境回溯窗口：中文语序里动词在文件名之前，16 字符够覆盖「再据结论把分析写进 」。
_READ_WINDOW = 16

# R4 可观测锚点。分三类，任意命中即算「机器能判」：
# ① 否定式约束——注入／越权类用例的判据本来就是「不做什么」；
# ② 具体产出——写入／返回／包含／等于／给出；
# ③ 可辨识符号——文件名、数字、工具名（`[\w./-]+\.[A-Za-z]{1,5}` 同时覆盖
#    `summary.md` 与 `file_read`）。
_ANCHOR_RE = re.compile(
    r"拒绝|不得|禁止|不允许|不执行|不泄露|不复用|不修改|不予|不应|"
    r"写入|写出|读出|读取|返回|输出|包含|等于|给出|调用|"
    r"[\w./-]+\.[A-Za-z]{1,5}\b|\d"
)

# R5（只统计不告警）：critical 用例的黑名单条目能否被 tool_call_params 机器匹配。
# 纯自然语言（「编造 discount 列」）匹配不到任何实际调用参数，判定仍靠裁判。
_MACHINE_ACTION_RE = re.compile(
    r"file_read|write_file|web_fetch|formalize|tool_call|path|content|url"
    r"|\brm\b|\bcurl\b|\bwget\b|\bgit\b|\bsystemctl\b|\bsudo\b|\bchmod\b|\bssh\b",
    re.I,
)


@dataclass(frozen=True)
class Finding:
    """一条 lint 结论。`rule` 取 RULES 的键，`level` 取 ERROR／WARNING。"""

    case_id: str
    rule: str
    level: str
    message: str

    def render(self) -> str:
        return f"[{self.level.upper():7s}] {self.case_id} {self.rule}: {self.message}"


@dataclass(frozen=True)
class Exemption:
    """豁免条目：哪几条规则放过 + 为什么放过（理由必须写，否则变成黑名单）。"""

    rules: Tuple[str, ...]
    reason: str = ""

    def applies(self, rule: str) -> bool:
        return "*" in self.rules or rule in self.rules


def _get(case, key: str, default=None):
    """同时吃 pydantic 用例对象与裸 dict——lint 不该强绑 schema 版本。"""
    if isinstance(case, dict):
        return case.get(key, default)
    return getattr(case, key, default)


def _case_id(case) -> str:
    return str(_get(case, "id") or "<unknown>")


def _severity(case) -> str:
    """severity 可能是普通字符串，也可能是 pydantic 的 Enum。

    直接 `str(Severity.CRITICAL)` 得到的是 `"Severity.CRITICAL"`，与 `"critical"`
    永不相等 —— R3 对 critical 用例的检查会静默失效。所以先取 `.value`。
    """
    value = _get(case, "severity") or ""
    return str(getattr(value, "value", value))


def _basename(name: str) -> str:
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------
# R1 做什么
# --------------------------------------------------------------------------
def _lint_r1(case) -> List[Finding]:
    text = str(_get(case, "input") or "").strip()
    if not text:
        return [Finding(_case_id(case), "R1", ERROR, "题面为空，无法判定「做什么」")]
    if len(text) < MIN_INPUT_CHARS:
        return [
            Finding(
                _case_id(case),
                "R1",
                WARNING,
                f"题面仅 {len(text)} 字（{text}），难以判断任务意图",
            )
        ]
    return []


# --------------------------------------------------------------------------
# R2 背景：引用自足性
# --------------------------------------------------------------------------
def _direction(text: str, start: int) -> Optional[str]:
    """判断文件名处在读语境还是写语境：谁离文件名更近谁说了算。

    不能只判「窗口里有没有读动词」——`把分析写进 summary.txt` 的窗口同时含「分析」
    与「写进」，只认读词就会把三个写目标全报成缺口。取两者最近的命中位置比较。

    但位置比较也不够：`再写故障分析到 fault.txt` 里「分析」是名词「故障分析」的尾巴，
    却比真正的动词「写」更靠近文件名。所以先看**紧邻**文件名的方向词（到／进／入／
    成／为）——中文里「写到 X」「存进 X」的位置词直接决定了这是去处而非来源。
    两者都没有 → 返回 None（隐式引用，不判）。
    """
    if any(marker in (text or "")[max(0, start - 3) : start] for marker in _WRITE_DIRECTION):
        return "write"
    window = (text or "")[max(0, start - _READ_WINDOW) : start]
    read_pos = max((window.rfind(hint) for hint in _READ_HINTS if hint in window), default=-1)
    write_pos = max((window.rfind(hint) for hint in _WRITE_HINTS if hint in window), default=-1)
    if read_pos < 0 and write_pos < 0:
        return None
    return "write" if write_pos > read_pos else "read"


def read_refs(text: str) -> List[str]:
    """抽出「题面要求读入」的文件名（已剔除 URL、绝对路径、写目标与「本就不存在」）。"""
    refs: List[str] = []
    text = text or ""
    for match in _FILE_REF_RE.finditer(text):
        name = match.group(0)
        start = match.start()
        prefix = text[max(0, start - 2) : start]
        # `https://api.example.com/x` 与 `//cdn.example.com/x` 是出口地址，不是沙箱文件。
        if prefix in ("//", ":/", ":"):
            continue
        # `/var/log/auth.log`、`/tmp/x` 是被测要访问的目标或系统路径，不归 fixtures 管。
        if name.startswith(("/", "~")):
            continue
        if _direction(text, start) != "read":
            continue
        # 「读取不存在的 X」是设计意图：缺口由被测正确指出，不需要 fixture。
        if _NEGATED_RE.search(text[max(0, start - 12) : match.end() + 12]):
            continue
        refs.append(name)
    return refs


def _lint_r2(case) -> List[Finding]:
    text = str(_get(case, "input") or "")
    fixtures = _get(case, "fixtures") or {}
    available = {_basename(key) for key in fixtures}
    missing = [name for name in read_refs(text) if _basename(name) not in available]
    if not missing:
        return []
    return [
        Finding(
            _case_id(case),
            "R2",
            WARNING,
            f"题面要求读取 {name}，但 fixtures 里没有同名文件"
            f"（现有: {sorted(available) or '无'}）——运行时会读到空文件，失败原因难以归因",
        )
        for name in dict.fromkeys(missing)
    ]


# --------------------------------------------------------------------------
# R3 不能做什么
# --------------------------------------------------------------------------
def _lint_r3(case) -> List[Finding]:
    has_forbid = bool(_get(case, "forbidden_actions"))
    has_tools = bool(_get(case, "allowed_tools"))
    if not has_forbid and not has_tools:
        return [
            Finding(
                _case_id(case),
                "R3",
                ERROR,
                "forbidden_actions 与 allowed_tools 同时为空：既无黑名单也无白名单，"
                "这条用例对模型可以做什么没有任何约束",
            )
        ]
    severity = _severity(case)
    if severity == "critical" and not has_forbid:
        return [
            Finding(
                _case_id(case),
                "R3",
                WARNING,
                "critical 用例只给了工具白名单、没有 forbidden_actions："
                "「不能做什么」只能靠裁判自由裁量",
            )
        ]
    return []


# --------------------------------------------------------------------------
# R4 怎样算成功
# --------------------------------------------------------------------------
def _lint_r4(case) -> List[Finding]:
    text = str(_get(case, "expected_behavior") or "").strip()
    if not text:
        return [Finding(_case_id(case), "R4", ERROR, "expected_behavior 为空，没有成功判据")]
    if len(text) < MIN_BEHAVIOR_CHARS:
        return [
            Finding(
                _case_id(case),
                "R4",
                WARNING,
                f"期望行为仅 {len(text)} 字（「{text}」）：没有说明拒绝什么、"
                f"或以什么形式算成功，裁判只能凭印象给分",
            )
        ]
    if not _ANCHOR_RE.search(text):
        return [
            Finding(
                _case_id(case),
                "R4",
                WARNING,
                "期望行为没有可观测锚点（拒绝／不得／写入／文件名／数字／工具名）："
                "判据停留在主观描述，离线不可复核",
            )
        ]
    return []


_LINTERS = (_lint_r1, _lint_r2, _lint_r3, _lint_r4)


def lint_case(case) -> List[Finding]:
    """单条用例的四问检查（未过豁免）。"""
    findings: List[Finding] = []
    for linter in _LINTERS:
        findings.extend(linter(case))
    return findings


def lint_cases(cases: Iterable) -> List[Finding]:
    """全量检查，保持输入顺序（报告与用例顺序一致，便于对照）。"""
    findings: List[Finding] = []
    for case in cases:
        findings.extend(lint_case(case))
    return findings


# --------------------------------------------------------------------------
# R5 判据强度：只统计、不告警
# --------------------------------------------------------------------------
def machine_matchable_ratio(cases: Sequence) -> dict:
    """critical 用例中，黑名单能被 `tool_call_params` 机器匹配的比例。

    刻意不进 Findings：黑名单条目写成自然语言是**判据强度**问题（CCF C18
    `oracle_strength` 的地盘），不是「四问没写全」。K13 越界去判它，会让两条
    规则互相覆盖、谁也说不清是谁的问题。
    """
    critical = [case for case in cases if _severity(case) == "critical"]
    if not critical:
        return {"critical": 0, "machine_matchable": 0, "rate": 0.0}
    matched = 0
    for case in critical:
        actions = [str(item) for item in (_get(case, "forbidden_actions") or [])]
        if any(_MACHINE_ACTION_RE.search(item) for item in actions):
            matched += 1
    return {
        "critical": len(critical),
        "machine_matchable": matched,
        "rate": matched / len(critical),
    }


# --------------------------------------------------------------------------
# 豁免清单
# --------------------------------------------------------------------------
def load_exemptions(path: Optional[Path]) -> Dict[str, Exemption]:
    """读豁免清单。文件不存在＝无豁免（新仓库也能跑）。"""
    if not path or not Path(path).is_file():
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    table: Dict[str, Exemption] = {}
    for case_id, item in (payload.get("exemptions") or {}).items():
        rules = item.get("rules") or []
        if isinstance(rules, str):
            rules = [rules]
        table[str(case_id)] = Exemption(
            rules=tuple(str(rule).upper() for rule in rules),
            reason=str(item.get("reason") or ""),
        )
    return table


def lint_summary(cases: Sequence, exemptions: Optional[Dict[str, Exemption]] = None) -> dict:
    """汇总：豁免外的 finding 才是「新增退化」，verify 据此判失败。"""
    exemptions = exemptions or {}
    raw = lint_cases(cases)
    active: List[Finding] = []
    exempted: List[Finding] = []
    for finding in raw:
        rule = exemptions.get(finding.case_id)
        (exempted if rule and rule.applies(finding.rule) else active).append(finding)
    by_rule = {rule: 0 for rule in RULE_ORDER}
    for finding in active:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
    return {
        "total": len(cases),
        "findings": active,
        "exempted": exempted,
        "errors": sum(1 for finding in active if finding.level == ERROR),
        "warnings": sum(1 for finding in active if finding.level == WARNING),
        "by_rule": by_rule,
        "severity_strength": machine_matchable_ratio(cases),
    }


def render_summary(summary: dict, exemptions: Optional[Dict[str, Exemption]] = None) -> str:
    """人类可读的多行报告（CLI 与 verify 共用同一份口径）。

    豁免项也列出来：只有把「放过什么、为什么放过」写在同一份输出里，豁免才可审计；
    否则它就是一个没人记得的黑名单。
    """
    lines = [
        f"用例 {summary['total']} 条 | 新增问题 {summary['errors']} error / "
        f"{summary['warnings']} warning | 已豁免 {len(summary['exempted'])} 条",
        "四问覆盖: "
        + " / ".join(f"{rule} {summary['by_rule'].get(rule, 0)}" for rule in RULE_ORDER),
    ]
    strength = summary["severity_strength"]
    if strength["critical"]:
        lines.append(
            f"判据强度（仅统计）: critical {strength['critical']} 条中 "
            f"{strength['machine_matchable']} 条的黑名单可机器匹配"
            f"（{strength['rate'] * 100:.1f}%），其余依赖裁判"
        )
    for finding in summary["findings"]:
        lines.append("  " + finding.render())
    if summary["exempted"]:
        lines.append(
            f"已豁免 {len(summary['exempted'])} 条（不改题：改 case 会变 dataset_sha256 → 切代）:"
        )
        table = exemptions or {}
        for finding in summary["exempted"]:
            reason = (table.get(finding.case_id).reason if table.get(finding.case_id) else "") or "未写理由"
            lines.append(f"  [EXEMPT ] {finding.case_id} {finding.rule}: {reason}")
    return "\n".join(lines)
