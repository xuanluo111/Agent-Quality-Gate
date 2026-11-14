"""C14′ fixture_ready：跑批前判定「用例声明的 fixture 到底能不能落盘」。纯离线、零 API、不写盘。

为什么需要它——`Sandbox.__init__` 写 fixture 失败会抛 `SandboxViolation`，`runner` 把
它包成 `error_message="沙箱初始化失败: ..."`。两个后果：

1. **归因错了**。那条 trace 的 `precondition_failed` 是默认值 `False`，`gate` 于是把
   它算进分母、记成模型答错——模型一句话都还没说。`SandboxViolation` 属于评测环境
   自己的准备动作失败，与 `initial_state` 写盘失败同类，天然该剔出分母。
2. **原因丢了**。裸 `str(exc)` 只有一句话，说不出是哪个 fixture、哪类失败。而
   `failure_class.is_precondition_error` 靠 marker 匹配，`fixture 超过大小限制` 根本
   不在 marker 表里，于是连"前置条件不满足"都识别不出来。

判据七类，前四类与 `Sandbox._resolve` 同源（复用 `sandbox_path_reason`，规则只有一份）：

| reason | 含义 | 谁会在跑批时炸 |
|---|---|---|
| `empty_path` | 路径为空 | `SandboxViolation` |
| `absolute_path` | 绝对路径，会写出工作区 | `SandboxViolation` |
| `escape` | `..` 归一化后越出工作区 | `SandboxViolation` |
| `not_a_file` | 归一化后是 `.`（工作区根目录本身） | `IsADirectoryError` |
| `too_large` | 单文件超 `MAX_FIXTURE_BYTES` | `SandboxViolation` |
| `duplicate_target` | 两条 fixture 归一化到同一路径 | 静默覆盖，后写赢 |
| `case_conflict` | 两条只差大小写 | macOS/Windows 上静默覆盖 |

后两类不报错、只丢数据，比报错更难查：用例少了一个文件，模型照着题面读不到，
失败最后被归到模型头上。

实测口径（`core_suite.jsonl` 207 条）：165 条不带 fixture，42 条各带 **1 个**文件，
共 42 个文件、其中 6 个在子目录里。所以 `duplicate_target` 与 `case_conflict` 目前
**0 命中**——它们是给多文件用例准备的防御性判据，不是凑数。把这两个藏在“测试
覆盖了”里不提，下一个人会以为它们已经验过真实数据。

设计底线：**不改 case 数据**。这里只读用例、只出结论；真有落不了盘的 fixture，
修题必须走评审（改 `core_suite.jsonl` 会动 `dataset_sha256` 切代）。所以本模块的作用
是让 `verify.py` 在跑批前把问题拦下来，而不是自己动手修。
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional

from src.engine.tools import MAX_FIXTURE_BYTES, sandbox_path_reason

# 报告与门禁的固定顺序：先"根本写不进去"，再"写进去了但覆盖了别人"。
DIAGNOSIS_ORDER = (
    "empty_path",
    "absolute_path",
    "escape",
    "not_a_file",
    "too_large",
    "duplicate_target",
    "case_conflict",
)

REASON_LABELS: Dict[str, str] = {
    "empty_path": "空路径",
    "absolute_path": "绝对路径",
    "escape": "越出工作区",
    "not_a_file": "指向工作区根目录",
    "too_large": "超过单文件上限",
    "duplicate_target": "与另一条 fixture 指向同一路径",
    "case_conflict": "与另一条 fixture 仅差大小写",
}

# 报错文案是写给人看的，reason 码是写给统计和门禁看的。靠 `"绝对路径" in msg` 反推
# 等于把文案当接口——改一个字上游就静默失效，所以映射只做一次且集中在这里。
_MESSAGE_PREFIX_TO_REASON = {
    "路径不能为空": "empty_path",
    "拒绝绝对路径": "absolute_path",
    "拒绝访问工作目录之外的路径": "escape",
}


def _reason_from_message(message: str) -> str:
    for prefix, reason in _MESSAGE_PREFIX_TO_REASON.items():
        if message.startswith(prefix):
            return reason
    # 兜底不该被走到；真走到了说明两边规则又分了叉，宁可报成越界让人看，
    # 也不要静默放行。
    return "escape"


@dataclass(frozen=True)
class FixtureIssue:
    """一条 fixture 的落盘问题。`path` 保留用例里写的原文，便于直接定位到 jsonl。"""

    path: str
    reason: str
    detail: str

    @property
    def label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)

    def render(self) -> str:
        return f"{self.path or '<空路径>'}({self.label}: {self.detail})"


def check_fixtures(fixtures: Optional[Mapping[str, str]]) -> List[FixtureIssue]:
    """返回按声明顺序排列的问题清单；空列表表示这批 fixture 可以落盘。

    纯函数：不建目录、不写文件、不读文件系统。同一份 fixtures 在跑批前与跑批中
    结论必须一致，否则预检就成了安慰剂。

    先到先占：重复路径报在**后声明**的那条上——它是会被静默丢弃的那条，先声明的
    文件内容不会变。
    """
    issues: List[FixtureIssue] = []
    seen: Dict[str, str] = {}
    for raw_path, content in (fixtures or {}).items():
        raw = str(raw_path or "")
        message = sandbox_path_reason(raw)
        if message:
            issues.append(FixtureIssue(raw, _reason_from_message(message), message))
            continue
        normalized = posixpath.normpath(raw)
        if normalized == ".":
            issues.append(
                FixtureIssue(raw, "not_a_file", "归一化后指向工作区根目录，不能作为文件写入")
            )
            continue
        size = len(str(content if content is not None else "").encode("utf-8"))
        if size > MAX_FIXTURE_BYTES:
            issues.append(
                FixtureIssue(raw, "too_large", f"{size} bytes 超过上限 {MAX_FIXTURE_BYTES}")
            )
            continue
        # 大小写不敏感文件系统上 `Notes.md` 与 `notes.md` 是同一个文件：同一个
        # 工作区里这两种写法只能留一个，否则谁后写谁赢。
        folded = normalized.casefold()
        first = seen.get(folded)
        if first is not None:
            reason = "duplicate_target" if first == normalized else "case_conflict"
            issues.append(FixtureIssue(raw, reason, f"与 `{first}` 是同一个文件"))
            continue
        seen[folded] = normalized
    return issues


def fixture_summary(cases: Iterable) -> dict:
    """给 `verify.py` 的汇总视图：用例口径 + 问题口径各一份。"""
    total = 0
    with_fixtures = 0
    file_count = 0
    by_reason: Dict[str, int] = {reason: 0 for reason in DIAGNOSIS_ORDER}
    offenders: List[dict] = []
    for case in cases:
        total += 1
        fixtures = getattr(case, "fixtures", None) or {}
        if not fixtures:
            continue
        with_fixtures += 1
        file_count += len(fixtures)
        for issue in check_fixtures(fixtures):
            by_reason[issue.reason] = by_reason.get(issue.reason, 0) + 1
            offenders.append(
                {
                    "case_id": str(getattr(case, "id", "") or ""),
                    "path": issue.path,
                    "reason": issue.reason,
                    "detail": issue.detail,
                }
            )
    return {
        "total": total,
        "with_fixtures": with_fixtures,
        "files": file_count,
        "issues": offenders,
        "by_reason": by_reason,
    }


def render_issues(issues: Iterable[FixtureIssue], limit: int = 5) -> str:
    """压成一行给 trace 的 `error_message` 用：留前几条，剩下的折成计数。"""
    items = list(issues)
    preview = "; ".join(issue.render() for issue in items[:limit])
    if len(items) > limit:
        preview += f"; ...共 {len(items)} 条"
    return preview


__all__ = [
    "DIAGNOSIS_ORDER",
    "REASON_LABELS",
    "FixtureIssue",
    "check_fixtures",
    "fixture_summary",
    "render_issues",
]
