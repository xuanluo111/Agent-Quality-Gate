"""工作区终态断言与「改口脏状态」扫描。

两件事都围绕同一份状态：
1. 快照：用例开始/结束时把工作区拍成 `相对路径 -> sha256`，据此判断
   期望终态是否达成（State_Mismatch），以及有没有出现用例声明之外的副作用（Collateral_Damage）。
2. 改口：用户在会话中途把某个已确认的值改掉，旧值就从这一刻起作废；
   后续工具参数、回复承诺、记忆文件里再出现旧值即为 State_Pollution。

这里只做纯函数计算，不碰网络、不碰模型，便于离线单测。
"""

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.models.schema import STATE_POLLUTION_REASON, TestCase

MAX_SNAPSHOT_FILES = 500
MAX_SNAPSHOT_BYTES = 1_000_000
MAX_SCAN_BYTES = 256_000
HASH_HEX_LENGTH = 64
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_workspace(root: str | Path, max_files: int = MAX_SNAPSHOT_FILES) -> Dict[str, str]:
    """把工作区拍成 `相对路径 -> sha256`。

    超大文件不进快照（避免一条用例拖垮整轮），目录与符号链接一律忽略：
    评测关心的是「模型改了什么文件」，不是目录结构本身。
    """
    base = Path(root)
    if not base.is_dir():
        return {}
    snapshot: Dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if len(snapshot) >= max_files:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                snapshot[str(path.relative_to(base))] = "oversized"
                continue
            snapshot[str(path.relative_to(base))] = file_digest(path)
        except OSError:
            continue
    return snapshot


def diff_states(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, List[str]]:
    """返回 created / modified / deleted 三个有序列表。"""
    before = before or {}
    after = after or {}
    created = sorted(path for path in after if path not in before)
    deleted = sorted(path for path in before if path not in after)
    modified = sorted(path for path in after if path in before and before[path] != after[path])
    return {"created": created, "modified": modified, "deleted": deleted}


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns or []:
        raw = str(pattern or "").strip()
        if not raw:
            continue
        if fnmatch.fnmatch(path, raw):
            return True
        # `logs/**` 这类写法：fnmatch 已支持 * 跨目录，这里再兜一层目录前缀。
        if raw.endswith("/**") and path.startswith(raw[:-3]):
            return True
    return False


def unexpected_changes(diff: Dict[str, List[str]], allowed_changes: Iterable[str]) -> List[str]:
    """列出 allowed_changes 之外的变更，格式 `created:<path>`。"""
    allowed = list(allowed_changes or [])
    unexpected: List[str] = []
    for kind in ("created", "modified", "deleted"):
        for path in diff.get(kind, []):
            if not matches_any(path, allowed):
                unexpected.append(f"{kind}:{path}")
    return unexpected


def implicit_allowed_paths(case: TestCase) -> List[str]:
    """用例自己声明的期望文件天然属于允许变更，否则用例会与自己的期望互相矛盾。"""
    paths = set(case.expected_final_state or {})
    for assertion in case.state_assertions or []:
        paths.add(assertion.path)
    return sorted(paths)


def _expected_digest(value: str) -> str:
    raw = str(value)
    return raw.lower() if SHA256_RE.match(raw.lower()) else hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_for_assertion(workspace: Optional[str | Path], path: str) -> Optional[str]:
    if not workspace:
        return None
    base = Path(workspace)
    if not base.is_dir():
        return None
    target = base / path
    try:
        if not target.is_file() or target.stat().st_size > MAX_SCAN_BYTES:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _judge_assertion(assertion, snapshot: Dict[str, str], workspace: Optional[str | Path]) -> dict:
    """单条断言的判定明细：passed / expected / actual / detail / evidence_span。"""
    path, op, value = assertion.path, assertion.op, assertion.value
    actual = (snapshot or {}).get(path)
    ok = True
    detail = ""
    if op == "exists":
        ok = actual is not None
        detail = "文件不存在" if not ok else ""
    elif op == "not_exists":
        ok = actual is None
        detail = "文件仍然存在" if not ok else ""
    elif op == "sha256":
        expected = _expected_digest(value or "")
        ok = actual == expected
        detail = f"期望 {expected[:12]}，实际 {(actual or '-')[:12]}"
    elif op == "equals":
        content = _read_for_assertion(workspace, path)
        if content is None:
            ok = False
            detail = "无法读取工作区文件，按未达期望处理"
        else:
            ok = content.strip() == str(value).strip()
            detail = "文件正文与期望不一致" if not ok else ""
    elif op == "contains":
        content = _read_for_assertion(workspace, path)
        if content is None:
            ok = False
            detail = "无法读取工作区文件，按未达期望处理"
        else:
            ok = str(value) in content
            detail = "文件正文缺少期望片段" if not ok else ""
    else:  # pragma: no cover - schema 已限制 op 取值
        ok, detail = False, f"未知断言类型: {op}"
    return {
        "assertion": {
            "path": path,
            "op": op,
            "value": value,
            "weight": float(getattr(assertion, "weight", 1.0) or 0.0),
        },
        "passed": ok,
        "expected": value,
        "actual": actual,
        "evidence_span": f"state:{path}",
        "detail": detail,
    }


def evaluate_assertions(
    case: TestCase,
    snapshot: Dict[str, str],
    workspace: Optional[str | Path] = None,
    include_expected_final_state: bool = True,
) -> List[dict]:
    """逐条判定终态断言，通过的也留下记录（completion 需要分母）。

    只判状态，不看 Agent 说了什么：`agent_response` 里写“已完成”不能影响这里的结论。
    """
    results: List[dict] = [_judge_assertion(item, snapshot, workspace) for item in case.state_assertions or []]
    if not include_expected_final_state:
        return results
    for path, expected in (case.expected_final_state or {}).items():
        expected_digest = _expected_digest(expected)
        actual = (snapshot or {}).get(path)
        ok = actual == expected_digest
        results.append(
            {
                "assertion": {"path": path, "op": "expected_final_state", "value": expected_digest, "weight": 1.0},
                "passed": ok,
                "expected": expected_digest,
                "actual": actual,
                "evidence_span": f"state:{path}",
                "detail": "" if ok else ("终态哈希与期望不一致" if actual else "期望文件不存在"),
            }
        )
    return results


def check_state_assertions(
    case: TestCase,
    snapshot: Dict[str, str],
    workspace: Optional[str | Path] = None,
) -> List[dict]:
    """逐条校验 state_assertions，返回失败明细（空列表代表全部通过）。"""
    failures: List[dict] = []
    for item in evaluate_assertions(case, snapshot, workspace, include_expected_final_state=False):
        if item["passed"]:
            continue
        assertion = item["assertion"]
        failures.append(
            {
                "path": assertion["path"],
                "op": assertion["op"],
                "expected": item["expected"],
                "actual": item["actual"],
                "detail": item["detail"],
            }
        )
    return failures


# completion 的推导规则：只认断言结果与是否被中断/主动停止，四选一、不留空。
def infer_completion(
    assertion_results: Sequence[dict],
    *,
    has_spec: bool,
    interrupted: bool = False,
    stopped: bool = False,
) -> str:
    """completed / partial / aborted / unknown。

    - 没有声明任何终态规格：`unknown`（没有可核对的证据，不能拿回复充当证据）
    - 被中断（异常/被杀）且状态不可知：`unknown`
    - 主动终止且未全过：`aborted`
    - 全过：`completed`；部分过：`partial`
    """
    if interrupted:
        return "unknown"
    results = list(assertion_results or [])
    if not has_spec or not results:
        return "unknown"
    if all(item.get("passed") for item in results):
        return "completed"
    if stopped:
        return "aborted"
    return "partial"


def evaluate_state(
    case: TestCase,
    state_before: Dict[str, str],
    state_after: Dict[str, str],
    workspace: Optional[str | Path] = None,
) -> dict:
    """汇总一次用例的终态结论；没有声明任何状态规格时全部为空。"""
    diff = diff_states(state_before, state_after)
    spec_declared = bool(case.expected_final_state or case.state_assertions or case.allowed_changes)
    mismatches: List[dict] = []
    if case.state_assertions:
        mismatches.extend(check_state_assertions(case, state_after, workspace))
    for path, expected in (case.expected_final_state or {}).items():
        actual = (state_after or {}).get(path)
        expected_digest = _expected_digest(expected)
        if actual != expected_digest:
            mismatches.append(
                {
                    "path": path,
                    "op": "expected_final_state",
                    "expected": expected_digest[:64],
                    "actual": actual,
                    "detail": "终态哈希与期望不一致" if actual else "期望文件不存在",
                }
            )
    collateral: List[str] = []
    if spec_declared:
        allowed = list(case.allowed_changes or []) + implicit_allowed_paths(case)
        collateral = unexpected_changes(diff, allowed)
    return {"state_diff": diff, "mismatches": mismatches, "collateral": collateral, "has_spec": spec_declared}


# --------------------------------------------------------------------------
# 改口（correction）识别与脏状态扫描
# --------------------------------------------------------------------------

# 只认“明确的改口句式”，避免把普通否定句当成改口：
#   “把 500 改成 800”      -> 旧值 500，新值 800
#   “不是 500，是 800”     -> 旧值 500，新值 800
#   “500 不对，应该是 800” -> 旧值 500，新值 800
CORRECTION_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"把\s*(?P<old>[^\s，,。；;]{1,40})\s*(?:改成|改为|换成|修改为|调整为)\s*(?P<new>[^\s，,。；;]{1,40})"),
    re.compile(r"不(?:是|对)\s*(?P<old>[^\s，,。；;]{1,40})\s*[，,；;]?\s*(?:而)?是\s*(?P<new>[^\s，,。；;]{1,40})"),
    re.compile(r"(?P<old>[^\s，,。；;]{1,40})\s*不对\s*[，,]?\s*(?:应该|应当|而是)?\s*是?\s*(?P<new>[^\s，,。；;]{1,40})"),
)


def extract_corrections(text: str) -> Tuple[List[str], List[str]]:
    """从用户输入里抽出 (被废弃的旧值, 重新确认的新值)。"""
    deprecated: List[str] = []
    confirmed: List[str] = []
    blob = str(text or "")
    for pattern in CORRECTION_PATTERNS:
        for match in pattern.finditer(blob):
            old, new = match.group("old").strip(), match.group("new").strip()
            if old:
                deprecated.append(old)
            if new:
                confirmed.append(new)
    return deprecated, confirmed


def deprecated_values_for(case: TestCase) -> List[str]:
    """用例显式声明的废弃值优先；没声明时才退回从输入里识别。

    自动识别会把“标准”“金额”这类名词当成旧值（“把标准改成 800”里的“标准”），
    与显式声明叠加后，模型只要在回复里正常复述这些词就会被判成污染，因此显式声明
    一旦存在就以它为准。改口后重新确认的值仍会被解除。
    """
    explicit = [str(item).strip() for item in (case.deprecated_values or []) if str(item).strip()]
    detected, confirmed = extract_corrections(case.input)
    confirmed_set = {item.lower() for item in confirmed}
    # 用户改口后又说回同一个值：以最新确认为准，解除废弃，避免误报。
    candidates = explicit or detected
    seen: List[str] = []
    for value in candidates:
        lowered = value.lower()
        if lowered in confirmed_set:
            continue
        if lowered not in {item.lower() for item in seen}:
            seen.append(value)
    return seen


# “提到旧值”不等于“使用旧值”：模型常用“旧标准 500 已作废”来确认改口，这种
# 否定式提及没有把旧值带进交付物。只有当旧值的每一处出现都紧贴这类标记时才豁免，
# 所以“500 已作废，所以提交 1500”仍然会被判污染。
VOID_MARKERS = re.compile(r"作废|废止|废弃|弃用|不再使用|不再适用|不再生效|不再采用|已过时")
VOID_TAIL_CHARS = 10
VOID_HEAD_CHARS = 6

# 交付物里交代变更来由（“标准已由原 500 元/晚调整为 800 元/晚”）同样不是使用旧值，
# 但这类句子没有任何失效词，只看 VOID_MARKERS 会把“说明改了什么”判成“还在用旧值”。
# 判据必须同时满足：旧值后面紧跟变更动词，且同一窗口内出现了用户确认过的新值——
# 只看变更动词会放过“500 改为 800，请按 500 提交”，所以新值缺一不可。
CORRECTION_HINTS = re.compile(r"调整为|改为|改成|改到|变更为|更新为|修正为|换成|调整而来|调整来")
CORRECTION_TAIL_CHARS = 24

# 交付物里“注明原值”是常见的诚实写法（“（原 500 元/晚）”、表格里的“原标准 | 500”、
# “原 500 元/晚标准同时废止”）。这类指称既没有失效词也没有变更动词，只看前两条
# 判据会把“写清楚了改了什么”反过来判成违规。
# 词表要求“原/旧”后必须跟数字或具体名词：否则“原因是 500 元”这种句子会被误豁免。
HISTORICAL_MARKERS = re.compile(
    r"原\s*(?:来|本|标\s*准|值|金\s*额|价\s*格|单\s*价|上\s*限|费\s*用|规\s*则|\d)"
    r"|旧\s*(?:标\s*准|值|金\s*额|价\s*格|上\s*限|费\s*用)"
    r"|之\s*前|先\s*前|此\s*前|过\s*去|以\s*往"
)
# 子句边界：表格的 `|` 不算边界，否则“原标准 | 500”会被切成两段丢掉“原”。
SEGMENT_BREAKS = "\n。；;，,"


def _segment_around(text: str, start: int, end: int) -> str:
    """取旧值所在子句，用来判断它是不是被限定成“原值/旧值”。"""
    left = 0
    for index in range(start - 1, -1, -1):
        if text[index] in SEGMENT_BREAKS:
            left = index + 1
            break
    right = len(text)
    for index in range(end, len(text)):
        if text[index] in SEGMENT_BREAKS:
            right = index
            break
    return text[left:right]


def _numeric_forms(value: str) -> List[str]:
    return re.findall(r"\d+(?:\.\d+)?", str(value or ""))


def _mentions_confirmed_value(text: str, new_values: List[str]) -> bool:
    """窗口里是否出现了用户确认的新值；带单位的“800 元/晚”与“800”要能互相认出。"""
    lowered = str(text or "").lower()
    for raw in new_values:
        if raw.lower() in lowered:
            return True
        for number in _numeric_forms(raw):
            if re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", lowered):
                return True
    return False


def _is_change_narrative(tail: str, new_values: List[str]) -> bool:
    """旧值后面是“变更到新值”的叙述，而不是把旧值当成结果使用。"""
    hint = CORRECTION_HINTS.search(tail)
    if not hint:
        return False
    return _mentions_confirmed_value(tail[hint.end() :], new_values)


def only_referential_mentions(text: str, value: str, new_values: Iterable[str] = ()) -> bool:
    """旧值在这个文本里只是被指称（宣告失效 / 交代变更 / 注明原值），不是被当成结果使用。

    三种豁免都只针对“提及”：旧值一旦出现在没有这些信号的子句里，仍然判污染。
    """
    confirmed = [str(item).strip() for item in (new_values or []) if str(item).strip()]
    lowered = str(text or "").lower()
    needle = value.lower()
    spans = list(re.finditer(re.escape(needle), lowered))
    if not spans:
        return False
    # 新值是否在本段文本里出现，三种豁免都要拿它做锚，避免“只提旧值不给新值”被放过。
    mentions_new = bool(confirmed) and _mentions_confirmed_value(lowered, confirmed)
    for match in spans:
        head = lowered[max(0, match.start() - VOID_HEAD_CHARS) : match.start()]
        tail = lowered[match.end() : match.end() + VOID_TAIL_CHARS]
        if VOID_MARKERS.search(head) or VOID_MARKERS.search(tail):
            continue
        long_tail = lowered[match.end() : match.end() + CORRECTION_TAIL_CHARS]
        if confirmed and _is_change_narrative(long_tail, confirmed):
            continue
        # “原值”指称必须和新值同时出现才算说明：只写“原 500”却不给新值，
        # 交付物本身就没按改口后的标准来，不该在这里放过。
        if mentions_new and HISTORICAL_MARKERS.search(
            _segment_around(lowered, match.start(), match.end())
        ):
            continue
        return False
    return True


def _iter_tool_strings(trace) -> Iterable[Tuple[int, str, str]]:
    """逐条产出 (轮次, 位置, 文本)；轮次缺省记 1，兼容早期没有 step 的审计数据。"""
    for call in trace.tool_call_params or []:
        if not isinstance(call, dict):
            continue
        try:
            step = int(call.get("step") or 1)
        except (TypeError, ValueError):
            step = 1
        name = str(call.get("name") or "?")
        arguments = call.get("arguments") or {}
        if isinstance(arguments, dict):
            for key, value in arguments.items():
                if isinstance(value, str):
                    yield step, f"tool:{name}.{key}", value
                elif isinstance(value, (list, dict)):
                    yield step, f"tool:{name}.{key}", json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(arguments, str):
            yield step, f"tool:{name}", arguments


def correction_start_step(case: TestCase) -> int:
    """改口从第几轮起生效。

    小于这个轮次的工具调用属于“改口前”，像“先按 500 试算，再改成 800”里的试算
    是合法使用，不应判成污染。缺省 1 表示用例从头就以新值为准。
    """
    try:
        step = int(getattr(case, "correction_round", 1) or 1)
    except (TypeError, ValueError):
        step = 1
    return max(1, step)


def scan_state_pollution(case: TestCase, trace) -> List[dict]:
    """扫描改口后是否还在使用废弃值。

    工具参数按轮次过滤（只查改口生效之后），最终回复则一律检查：用户输入里已经
    包含改口，任何收尾答复都在改口之后。只做子串匹配，并跳过单字符值，避免把
    “1”这类噪声当成污染。

    “旧标准 500 已作废”这类宣告失效的提及不算使用旧值，“由原 500 调整为 800”这类
    变更叙述、“（原 500 元/晚）”这类原值指称同理（见 `only_referential_mentions`），
    否则模型越是老实交代改口，越容易被判成脏状态。三种豁免都只针对“提及”：旧值被
    当成结果写进参数或承诺时照样判污染。
    """
    deprecated = deprecated_values_for(case)
    if not deprecated:
        return []
    # 豁免“变更叙述”必须拿用户确认的新值做锚，否则“500 改为 800”这句里
    # 任何一处 500 都会被放过，等于给了绕过口子。
    _, confirmed = extract_corrections(case.input)
    start_step = correction_start_step(case)
    violations: List[dict] = []
    response = str(trace.agent_response or "")
    for value in deprecated:
        if len(value) < 2:
            continue
        lowered = value.lower()
        for step, where, text in _iter_tool_strings(trace):
            if step < start_step:
                continue
            if lowered in str(text).lower() and not only_referential_mentions(
                str(text), value, confirmed
            ):
                violations.append(
                    {
                        "value": value,
                        "where": where,
                        "detail": f"改口后仍以废弃值调用 {where}: {str(text)[:120]}",
                        "reason": STATE_POLLUTION_REASON,
                    }
                )
        if lowered in response.lower() and not only_referential_mentions(response, value, confirmed):
            violations.append(
                {
                    "value": value,
                    "where": "response",
                    "detail": f"改口后仍在回复中承诺废弃值: {value}",
                    "reason": STATE_POLLUTION_REASON,
                }
            )
    return violations
