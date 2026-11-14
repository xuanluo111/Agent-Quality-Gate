"""M1 责任入账：风险台账的责任角色与签字（risk acceptance）口径。

两个字段回答两个不同问题，不能合并：

- ``owner``：这条风险**该谁修**。取值是角色而非人名——台账按 ``(model, case_id)``
  分区，同一用例在 9 个模型上都可能 open，逐条填人不现实，而且会随模型退役失效
  （历史台账里仍留着 ``glm-4.5-flash``、``dots3-note-prev`` 这类已下线标签）。
  角色词汇直接复用报告里的 ``STAGE_ATTRIBUTION``，避免出现"报告说归编排层、
  台账说归 qa-lead"的两套语言。
- ``risk_accepted_by``：谁决定**不修**并为这个数字被引用承担后果。只有
  Critical 且 open 的条目需要签，High 只派 owner 不签——302 条全签等于免责模板。

"有资格签字"不是权限位（个人项目没有 IAM），而是三件可追溯的事：签字带理由、
签字人必须在名单里、签字人不得等于 owner 自身。缺任一条即判为无效签字。

三条硬约束（写进设计，避免字段沦为注释）：

1. 派发表漏项 → ``unassigned``，``verify`` 判失败。不允许"没人负责"静默过关。
2. 签字缺 ``accept_reason``／签字人不在名单／签字人就是 owner → 无效签字，``verify``
   判失败。签字的唯一价值是可追溯，不可追溯的签字比不签更糟。
3. 签字**随代际失效**（``accepted_epoch`` 与当前 ``dataset_epoch`` 不一致即过期）：
   换了数据集还沿用旧签字，等于用旧判断背书新题面。

未签字的 Critical 只报告不阻断：它是一份待办清单，不是门禁失败——把"还没签"
当失败，会让每轮跑批都被迫批量签字。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Sequence

# ---- 责任角色 ----
# 前五项与 report.STAGE_ATTRIBUTION 的"责任方"同名，第六项（运行预算配置）在台账
# 里没有对应的 category 派发来源，但保留为合法取值：人工可以把某条改派给它（预算类
# 风险确实存在），不因为派发表没用到就判为非法。
OWNER_ROLES = (
    "用例/提示词工程",
    "Harness 编排层",
    "模型/工具调用层",
    "Harness 自检层",
    "运行预算配置",
    "裁判/标注",
)
UNASSIGNED = "unassigned"

# ---- category → 责任角色 ----
# 判据是"这类失败最该在哪一层被拦住"，不是"谁写的这段代码"。
# 三个安全类都归编排层，是因为越权/危险动作本该在工具调用前被白名单拦下；
# 落到模型层会让"该拦没拦"这件事永远没人认领。
# 除实测的 7 个类别外，一并覆盖 IMPACT_MAP 里出现过的历史/英文别名（权限边界、
# tool_misuse 等）——否则重建台账时它们会集体落成 unassigned，把真正的漏报淹没。
CATEGORY_OWNER: Dict[str, str] = {
    "运行环境与隔离": "Harness 编排层",
    "工具与行动安全": "Harness 编排层",
    "工具误用": "Harness 编排层",
    "tool_misuse": "Harness 编排层",
    "身份与授权": "Harness 编排层",
    "权限边界": "Harness 编排层",
    "prompt_injection": "Harness 编排层",
    "数据与上下文安全": "Harness 自检层",
    "跨领域风险涌现": "用例/提示词工程",
    "Cross_Domain": "用例/提示词工程",
    "数据分析与计算": "模型/工具调用层",
    "事实可靠性": "模型/工具调用层",
    "hallucination": "模型/工具调用层",
    "谄媚攻击(Sycophancy)": "模型/工具调用层",
    # 类别缺失本身就是标注欠账，派给标注而不是猜一个业务层，
    # 否则会靠一个错误归属把数据缺陷伪装成已分派。
    "未分类": "裁判/标注",
}

# ---- 签字人名单 ----
# 新增签字人必须同时改 SECURITY.md 的「风险签字人」一节：签字要有据可查，
# 不能靠手改一个 json 字段完成。当前只有项目作者具备"能修 + 会署名"两条件。
ACCEPTOR_ALLOWLIST: Sequence[str] = ("xuanluo111",)

# 需要签字的范围：只有 Critical 的 open/regressed。
# High 只派 owner——全部 open 都签等于把签字变成跑批的例行公事。
ACCEPTANCE_RISK_LEVELS = ("critical",)

ACCEPTANCE_STATES = ("signed", "unsigned", "expired", "missing_reason", "invalid_actor", "self_accept")


def owner_for(category: str) -> str:
    """按类别派发责任角色；未覆盖的类别返回 ``unassigned``（verify 会因此判失败）。"""
    return CATEGORY_OWNER.get(str(category or "").strip(), UNASSIGNED)


def is_valid_owner(value: str) -> bool:
    return value in OWNER_ROLES


def assign_owner(entry: dict) -> dict:
    """就地回填 ``owner``，并标注来源。

    ``owner_source`` 三态，用于区分"机器派的"与"人改的"：
    - ``category_map``：按派发表自动填的；
    - ``manual``：已存在且是合法角色，但不等于派发表结果（人工改派，重跑不得覆盖）；
    - ``unassigned``：派发表没覆盖，需要人工补映射。
    """
    current = str(entry.get("owner") or "").strip()
    mapped = owner_for(entry.get("category", ""))
    if current and is_valid_owner(current):
        entry["owner"] = current
        entry["owner_source"] = "category_map" if current == mapped else "manual"
        return entry
    entry["owner"] = mapped
    entry["owner_source"] = "unassigned" if mapped == UNASSIGNED else "category_map"
    return entry


def needs_acceptance(entry: dict) -> bool:
    """是否必须签字：Critical 且仍在跟进（open/regressed）。"""
    return (
        entry.get("status") in {"open", "regressed"}
        and str(entry.get("risk_level") or "") in ACCEPTANCE_RISK_LEVELS
    )


def acceptance_state(entry: dict, current_epoch: str = "") -> str:
    """判定一条台账条目的签字状态。

    顺序有讲究：先查"有没有签"，再查"签得对不对"，最后查"过没过期"——
    一个过期的无效签字要报 ``invalid_actor`` 而不是 ``expired``，否则会引导人去
    续期一个本来就不该存在的签字。
    """
    acceptor = str(entry.get("risk_accepted_by") or "").strip()
    if not acceptor:
        return "unsigned"
    if acceptor not in ACCEPTOR_ALLOWLIST:
        return "invalid_actor"
    if str(entry.get("owner") or "") == acceptor:
        return "self_accept"
    if not str(entry.get("accept_reason") or "").strip():
        return "missing_reason"
    accepted_epoch = str(entry.get("accepted_epoch") or "")
    if current_epoch and accepted_epoch != current_epoch:
        return "expired"
    return "signed"


def _key(entry: dict) -> str:
    model = str(entry.get("model") or "")
    case_id = str(entry.get("case_id") or "")
    return f"{model}::{case_id}" if model else case_id


def ownership_summary(entries: Iterable[dict], current_epoch: str = "") -> dict:
    """汇总责任与签字现状。

    返回里 ``unassigned``／``invalid_acceptances`` 是 verify 的阻断依据，
    ``unsigned_critical`` 只进报告（待办清单，不是失败）。
    """
    rows = [entry for entry in entries if isinstance(entry, dict)]
    by_owner: Dict[str, int] = {role: 0 for role in OWNER_ROLES}
    by_owner[UNASSIGNED] = 0
    unassigned: List[str] = []
    invalid: List[dict] = []
    unsigned_critical: List[str] = []
    signed = 0

    for entry in rows:
        role = str(entry.get("owner") or "")
        if role in by_owner:
            by_owner[role] += 1
        else:
            by_owner[UNASSIGNED] += 1
            role = UNASSIGNED
        if role == UNASSIGNED:
            unassigned.append(_key(entry))

        state = acceptance_state(entry, current_epoch)
        if state == "signed":
            signed += 1
        elif state != "unsigned":
            invalid.append({"key": _key(entry), "state": state})
        elif needs_acceptance(entry):
            unsigned_critical.append(_key(entry))

    return {
        "total": len(rows),
        "by_owner": by_owner,
        "unassigned": unassigned,
        "signed": signed,
        "invalid_acceptances": invalid,
        "needs_acceptance": sum(1 for e in rows if needs_acceptance(e)),
        "unsigned_critical": unsigned_critical,
        "epoch": current_epoch,
    }


def render_acceptance(entry: dict, current_epoch: str = "") -> str:
    """报告里的一格：``signed``／``unsigned``／``expired``… 便于扫读。"""
    state = acceptance_state(entry, current_epoch)
    if state == "signed":
        return f"signed({entry.get('risk_accepted_by', '')})"
    if state == "unsigned":
        return "unsigned" if needs_acceptance(entry) else "-"
    return state
