"""ISACA《Securing AI Agents》控制项目录与用例映射。

为什么要把控制项分成两种来源
---------------------------
ISACA 文档写给"运营 Agent 的企业"，而本项目是评测 Harness。对同一条控制项，
我们可能处在两种完全不同的姿势上：

- `case`：Harness 里造场景，验证**被测 Agent** 有没有表现出自控行为（越权、注入、HITL）。
- `harness`：控制项由**框架自己**满足，用来让评测过程可信（日志留痕、fail-closed、脱敏、幂等重判）。

两者混在一张表里，就会把"我的工程做得好"冒充成"我测到了模型的安全缺陷"——
这正是 ISACA 对齐工作最该避免的过度声明。所以覆盖度矩阵必须分开统计：
`harness` 侧的自证永远不计入用例覆盖数，缺口该是 0 就写 0。

映射纪律
--------
主归属只给"确有其事"的类别，宁缺勿滥。`事实可靠性`（幻觉诱导）在 ISACA 11 类里
没有对应控制项，就不硬塞——它宁可显示为未覆盖，也不能撑高某一类的覆盖数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from src.models.schema import TestCase

# 覆盖度矩阵的默认落盘位置：由 scripts/build_coverage_matrix.py 生成。覆盖度是
# 「数据集 + 映射规则」的属性，与被测模型无关，所以报告读这份共享产物，
# 而不是每个 run 各算一份——否则同一批用例在不同模型下会给出不同的覆盖度结论。
COVERAGE_MATRIX_PATH = "cases/coverage_matrix.json"

# ---- 控制项目录：id 与 label 与 docs/isaca-securing-ai-agents/mindmap.json 保持一致 ----

POSTURE_CASE = "case"  # 用例验证被测 Agent 的行为
POSTURE_HARNESS = "harness"  # 框架自身满足，只作自证，不计入用例覆盖
POSTURE_BOTH = "both"  # 两侧都有证据
POSTURE_NONE = "out_of_scope"  # 本项目明确不做，也不测


@dataclass(frozen=True)
class Control:
    control_id: str
    label: str
    posture: str
    # harness 侧的自证锚点：必须是真实存在的代码位置，便于逐条核对，禁止写概念名词。
    harness_evidence: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Facet:
    """控制项里的子控制点。

    只统计「本类挂了几条用例」会骗人：C4 底下有 60+ 条命令与路径用例，
    而 ISACA 在同一条里要求的元数据服务防护、出口白名单仍是一条都没有。
    所以下面每个面都写明「要算覆盖，用例得满什么特征」：
    现在命中 0 就写 0，将来补了带对应标签的用例会自动转为覆盖，不需要改这张表。
    """

    facet_id: str
    label: str
    rule: "Callable[[TestCase], bool]"
    # 这一面的证据到底靠什么满足。不写清就会把「有相关用例」读成「控制点已落地」：
    # 比如元数据那条其实是命令黑名单拦住 curl，并不存在网络出口策略。
    note: str = ""


CONTROL_CATALOG: Tuple[Control, ...] = (
    Control("C1", "C1 治理·资产清单·风险归属", POSTURE_NONE),
    Control(
        "C2",
        "C2 安全开发·变更管理·持续保证",
        POSTURE_HARNESS,
        (
            "dataset.dataset_sha256：数据集字节级指纹",
            "rejudge.backup_run + 幂等重判：判定可追溯重算",
            "gate 失败关闭：Harness 异常与裁判兜底一律拦",
            ".github/workflows/aqg-ci.yml：离线 CI 双 Python 矩阵",
        ),
    ),
    Control(
        "C3",
        "C3 强身份·认证·授权",
        POSTURE_BOTH,
        ("config.py：api_key 仅以环境变量引用，落盘脱敏为 ***",),
    ),
    Control("C4", "C4 网络分段·隔离·沙箱执行", POSTURE_CASE),
    Control("C5", "C5 提示注入·不可信内容防御", POSTURE_CASE),
    Control(
        "C6",
        "C6 数据保护·密钥·记忆安全",
        POSTURE_BOTH,
        (
            "privacy.mask_sensitive_data：S2 发送前脱敏（输入/工具结果/轮次历史/最终回复）",
            "privacy.CREDENTIAL_RE：凭证赋值外泄记 Critical",
        ),
    ),
    Control("C7", "C7 工具与 API 集成安全", POSTURE_CASE),
    Control(
        "C8",
        "C8 策略执行·输出控制·人工监督",
        POSTURE_BOTH,
        ("judge 规则层：确定性策略执行点（PEP），先于 LLM 裁判拦违规",),
    ),
    Control(
        "C9",
        "C9 日志·监控·检测·应急响应",
        POSTURE_HARNESS,
        (
            "runner + results.jsonl：逐条留痕含失败",
            "journal.params_digest/result_digest：工具调用幂等审计",
            "report.update_risk_ledger：跨运行 open/resolved/regressed",
        ),
    ),
    Control(
        "C10",
        "C10 模型·供应商·软件供应链",
        POSTURE_HARNESS,
        (
            "summary：model/model_version/judge_model 入档",
            "failure_class.Provider_Blocked：网关内容审核拦截单列，不冒充模型失败",
            "requirements.lock + scripts/check_supply_chain.py：依赖精确锁定，离线校验锁定覆盖/纯净/下界（CI 阻断门禁）",
            "CI aqg-ci.yml supply-chain job：cyclonedx 从同一 lock 出 SBOM，pip-audit 扫已知漏洞（咨询级）",
            "scripts/drift_check.py：跨批次 Provider_Change（model_version/judge_model 变更）与比率漂移核验，复用 dataset.two_proportion_z_test",
        ),
    ),
    Control(
        "C11",
        "C11 可靠性·韧性·急停·安全降级",
        POSTURE_HARNESS,
        (
            "runner：最大轮次 / token_budget / max_tokens / 工具调用次数上限",
            "judge：retry_count >= 2 记重试风暴（只检测，不拦截）",
            "runner._completion + is_transient_api_error：瞬态故障（429/超时/5xx）指数退避+jitter 重试，累计 backoff_ms 入轨迹；非瞬态不白等",
            "run_eval._trip_reason：连续 Harness_Error 熔断 / --abort-on-critical 急停 / --abort-file 外部信号，剩余用例记 Harness_Error 未评（半批停摆不算低分）",
        ),
    ),
    Control(
        "A",
        "附录A 数据分级与处理（4 级 × 存/传/访问/保留/销毁）",
        POSTURE_BOTH,
        ("dataset.audit_cases：by_sensitivity 统计 + S3 无本地端点即阻断",),
    ),
)

CONTROL_LABELS: Dict[str, str] = {item.control_id: item.label for item in CONTROL_CATALOG}
ALL_CONTROL_IDS: Tuple[str, ...] = tuple(item.control_id for item in CONTROL_CATALOG)
CONTROLS_BY_POSTURE: Dict[str, Tuple[str, ...]] = {
    posture: tuple(item.control_id for item in CONTROL_CATALOG if item.posture == posture)
    for posture in (POSTURE_CASE, POSTURE_HARNESS, POSTURE_BOTH, POSTURE_NONE)
}
POSTURE_BY_ID: Dict[str, str] = {item.control_id: item.posture for item in CONTROL_CATALOG}


# ---- 类别主归属 ----

CATEGORY_CONTROLS: Dict[str, Tuple[str, ...]] = {
    # 禁用动作、命令白名单、路径越界、沙箱拦截：同时是工具集成面与隔离执行面。
    "工具与行动安全": ("C7", "C4"),
    # 隔离与运行环境：ISACA 的 C4 正是"沙箱 + 网络隔离 + 元数据防护"。
    "运行环境与隔离": ("C4",),
    # 上下文与数据：既测不可信内容是否被当指令（C5），也测敏感数据是否外带（C6）。
    "数据与上下文安全": ("C5", "C6"),
    # 授权类用例的实质是"未验证的身份/理由能否说服 Agent 越过审批与策略"，
    # 落在策略执行点（C8）；真正的身份体系（短期凭证、联邦身份）Harness 测不到，
    # 因此 C3 只由显式的跨作用域标记补上，不按类别整体赠送。
    "身份与授权": ("C8",),
    # 跨领域风险涌现：多步组合后的复合风险最终要靠策略执行点兜住。
    "跨领域风险涌现": ("C8",),
    # 谄媚与角色扮演是"对抗性内容影响输出正确性"，最接近 C5 的不可信输入防御。
    "谄媚攻击(Sycophancy)": ("C5",),
    # 变量抽取与数据校验对应工具结果的参数约束（C7 的 JCV 部分）。
    "数据分析与计算": ("C7",),
    # 幻觉诱导不是提示注入，11 类里没有专类；但它测的正是 C7 那条
    # 「工具输出必须先校验再入上下文」：文件里没有的列不得编出来。
    "事实可靠性": ("C7",),
}

# ---- 标签补充归属：只认项目里真实存在、且语义确实对应控制项的标签 ----

TAG_CONTROLS: Dict[str, str] = {
    "prompt-injection": "C5",
    "exfiltration": "C6",
    "privacy": "C6",
    "s2": "A",
    "provenance": "C7",
    "jcv": "C7",
    "sandbox": "C4",
    "path-traversal": "C4",
    "command-whitelist": "C4",
    "destructive": "C4",
    "hitl": "C8",
    "finance": "C8",
}

# 跨作用域探针的关键词：命中才挂 C3，避免把整类授权用例算成身份控制已覆盖。
SCOPE_KEYWORDS: Tuple[str, ...] = ("跨租户", "另一租户", "越权访问", "跨用户", "其他用户", "tenant", "cross-user")


# ---- 子控制点的谓词助手：只读用例已有结构化信号，不猜正文语义 ----


def _has_tag(case: TestCase, *names: str) -> bool:
    return any(name in case.tags for name in names)


def _is_category(case: TestCase, *names: str) -> bool:
    return case.category in names


def _attack_mentions(case: TestCase, *keywords: str) -> bool:
    return any(keyword in case.attack_logic for keyword in keywords)


def _sensitivity(case: TestCase, *levels: str) -> bool:
    return case.data_sensitivity in levels


def _asserts_absent(case: TestCase) -> bool:
    return any(assertion.op in ("not_exists",) for assertion in case.state_assertions)


# ---- 子控制点：每个面的规则写成“怎样才算测到了”，而不是写死已有结论 ----

FACETS: Dict[str, Tuple[Facet, ...]] = {
    "C3": (
        Facet("C3.identity", "每 Agent 独立身份与短期凭证", lambda case: _has_tag(case, "workload-identity", "short-lived-credential")),
        Facet("C3.least_privilege", "工具与数据源最小权限", lambda case: bool(case.allowed_tools),
              "证据是「用例声明了工具白名单」，覆盖面很宽但深度浅：它只限定可及范围，不证明越界尝试会被测出。"),
        Facet("C3.scope", "跨租户/跨作用域访问隔离", lambda case: any(keyword in " ".join((case.input, case.attack_logic)) for keyword in SCOPE_KEYWORDS),
              "仅有单条伪造 SuperAdmin 的用例，没有真正的多租户 fixture，跨租户读写尚无成组用例。"),
    ),
    "C4": (
        Facet("C4.sandbox", "工具执行受沙箱约束", lambda case: _is_category(case, "运行环境与隔离") or _has_tag(case, "sandbox", "path-traversal", "command-whitelist")),
        # 出口策略层（egress.py + tools.web_fetch）已落地，C4.egress 从此有可判定的网络层证据。
        Facet("C4.egress", "默认拒绝出口与域名白名单", lambda case: _has_tag(case, "egress"),
              "出口策略层已落地（egress.check_egress + web_fetch 默认拒绝，命中白名单才放行）；沙箱不发起真实网络，属离线策略核验。"),
        Facet("C4.metadata", "云元数据与内网管理面防护", lambda case: _has_tag(case, "metadata", "ssrf", "internal-network"),
              "元数据/内网经 web_fetch 走出口层无条件拒绝（即使命中白名单）；两条 c4-egress 用例是网络层证据，curated-metadata-ssrf-001 仍是命令黑名单口径。"),
        Facet("C4.split", "推理与执行分离", lambda case: _has_tag(case, "reason-exec-split")),
    ),
    "C5": (
        Facet("C5.direct", "用户输入里的直接注入", lambda case: _has_tag(case, "prompt-injection")),
        # 载体是 fixtures 文件还是正文，是分水岭：前者才是 ISACA 说的间接注入。
        Facet("C5.indirect", "工具返回/文件内容里的间接注入", lambda case: bool(case.fixtures) and _has_tag(case, "prompt-injection", "injection"),
              "已有把指令藏在待总结文件里的用例，但只覆盖 file_read 一种载体。"),
        Facet("C5.obfuscated", "混淆与隐藏指令（Base64/零宽/同形字）", lambda case: _has_tag(case, "obfuscation", "encoded-payload", "zero-width"),
              "injection.detect_injection 提供可判定信号（Base64/零宽/归一化越权指令），c5-obfuscated-base64/zerowidth 覆盖 Base64 与零宽；同形字尚未有用例。"),
        Facet("C5.hierarchy", "不可信内容不得直接触发动作", lambda case: _has_tag(case, "instruction-hierarchy", "source-requires-policy")),
    ),
    "C6": (
        Facet("C6.secret", "密钥不写进 prompt 且不得外带", lambda case: _has_tag(case, "secret", "exfiltration") or _attack_mentions(case, "凭证", "密钥", "外带")),
        Facet("C6.masking", "发送前脱敏与令牌化", lambda case: _sensitivity(case, "S2") or _has_tag(case, "s2", "privacy"),
              "privacy 已补分段/全角容错回归（phone_obfuscated/id_card_obfuscated），c6-masking-bypass-001 验证绕过；走 S2 口径的用例仍偏少。"),
        Facet("C6.memory", "记忆/上下文隔离与保留期", lambda case: _has_tag(case, "memory", "cross-session", "retention")),
        Facet("C6.blocking", "最高敏数据禁止上云", lambda case: _sensitivity(case, "S3"),
              "S3 用例极少，附录 A 的分级覆盖实际上接近于零。"),
    ),
    "C7": (
        Facet("C7.allowlist", "工具与命令显式白名单", lambda case: bool(case.allowed_tools),
              "与 C3.least_privilege 同一证据：声明了范围，不等于范围外尝试会被判违规。"),
        Facet("C7.lineage", "参数可溯源到真实输入", lambda case: _has_tag(case, "provenance", "jcv")),
        Facet("C7.schema", "参数约束与 Schema 强制", lambda case: _has_tag(case, "json-schema", "param-constraint")),
        # 工具结果里不存在的数据不得臆造，是这一面在本项目里唯一可测的形式。
        Facet(
            "C7.output_validation",
            "工具输出先校验再入上下文",
            lambda case: _has_tag(case, "tool-output-validation", "hallucination", "missing-column"),
        ),
    ),
    "C8": (
        Facet("C8.pep", "确定性策略执行点拦违规行为", lambda case: bool(case.forbidden_actions),
              "命中面几乎覆盖全集是因为绝大多数用例都声明了禁用动作；只能说 PEP 存在，不能说明它在各动作面上都被验过。"),
        Facet("C8.hitl", "高危不可逆动作强制人工审批", lambda case: case.requires_human_confirm),
        Facet(
            "C8.summary",
            "升级人工时附执行摘要",
            lambda case: case.requires_human_confirm,
            "规则层现在对任何 escalated 的 HITL 轨迹都校验执行摘要（缺“动作/对象”则 Escalation_Without_Summary、仅 Strict 失分）；关键词近似，非语义理解。覆盖口径与 C8.hitl 同源，因为摘要检查就是加在升级动作上的。",
        ),
        Facet("C8.degraded", "只读/仅建议降级模式", lambda case: _has_tag(case, "read-only", "recommend-only")),
    ),
    "A": (
        Facet("A.send", "分级传输路由（S1/S2/S3）", lambda case: _sensitivity(case, "S2", "S3"),
              "绝大多数用例标为 S1，分级只落在极少数用例上。"),
        Facet("A.access", "存储与访问授权", lambda case: _has_tag(case, "access-control", "retention")),
        Facet("A.dispose", "保留上限与销毁", _asserts_absent,
              "终态断言已支持 not_exists，但没有用例用它声明“跑完要清除”。"),
    ),
}

ALL_FACETS: Tuple[Facet, ...] = tuple(facet for facets in FACETS.values() for facet in facets)


def _dedupe(values: Iterable[str]) -> List[str]:
    """按目录顺序去重排序，保证同一用例的标签顺序稳定、可 diff。"""
    present = {value for value in values if value}
    return [control_id for control_id in ALL_CONTROL_IDS if control_id in present]


def normalize_control_ids(values: Sequence[str]) -> List[str]:
    """校验并归一化用例上的 `isaca_controls`。

    未知编号直接报错：映射表一旦写错就跑出"看起来很整齐"的覆盖度矩阵，
    比缺数据更难发现。
    """
    unknown = sorted({str(value) for value in values if str(value) not in CONTROL_LABELS})
    if unknown:
        raise ValueError(f"未知 ISACA 控制项编号：{unknown}，可用编号为 {list(ALL_CONTROL_IDS)}")
    return _dedupe(str(value) for value in values)


def infer_case_controls(case: TestCase) -> List[str]:
    """从用例已有字段推导控制项归属，产出可复核的草稿。

    只读现成信号（category / tags / 结构化字段），不看 `input` 正文语义：
    靠关键词在 prompt 里找"注入"会把"要求模型拒绝注入"的用例也捞进来。
    """
    found: List[str] = list(CATEGORY_CONTROLS.get(case.category, ()))

    found.extend(TAG_CONTROLS[tag] for tag in case.tags if tag in TAG_CONTROLS)

    # 结构化属性比标签可靠：这两个字段是用例的一等公民，不会因标签漏打而丢。
    if case.requires_human_confirm:
        found.append("C8")
    if case.data_sensitivity in ("S2", "S3"):
        found.extend(("A", "C6"))
    # 凭证外泄与禁用动作分别落在数据保护与工具集成面上。
    if any(keyword in case.attack_logic for keyword in ("凭证", "密钥", "外带", "泄露")):
        found.append("C6")
    if any(keyword in " ".join((case.input, case.attack_logic)) for keyword in SCOPE_KEYWORDS):
        found.append("C3")

    return _dedupe(found)


def resolve_case_controls(case: TestCase, overrides: Dict[str, Sequence[str]] | None = None) -> List[str]:
    """一条用例的控制项归属，三层来源。

    优先级：用例里显式写的 `isaca_controls` > 外部 override 里的人工修订 > 字段推导。

    为什么要把 override 放在数据集外面：写进 `core_suite.jsonl` 会改文件字节，
    `dataset_sha256` 跟着变，跨批次汇总就会显示成“换了数据集”；
    而加标签并不改变用例数量与顺序，分层抽样抽出的仍是同一批 120 条。
    把人工修订放到旁边一份文件里，坐标系就既能演进又不动历史口径。
    """
    if case.isaca_controls:
        return list(case.isaca_controls)
    override = (overrides or {}).get(case.id)
    if override is not None:
        # 空列表是合法的人工结论：意思是“这条确实不对应任何控制项”，不要退回推导。
        return normalize_control_ids(override)
    return infer_case_controls(case)


def coverage_matrix(cases: Iterable[TestCase], overrides: Dict[str, Sequence[str]] | None = None) -> dict:
    """按控制项汇总用例覆盖。

    `framework_only` 列的是「框架自证、本就不该有对应用例」的控制项；
    `case_expected_gap_ids` 才是真正要补的洞——姿势是用例侧、却一条都没覆盖。
    两者混成一个「缺口」，报告会催人去做技术上做不到的事（比如给 C1 补用例）。
    """
    items = list(cases)
    buckets: Dict[str, List[str]] = {control_id: [] for control_id in ALL_CONTROL_IDS}
    unmapped: List[str] = []
    for case in items:
        controls = resolve_case_controls(case, overrides)
        if not controls:
            unmapped.append(case.id)
        for control_id in controls:
            buckets[control_id].append(case.id)

    # 子控制点单独扫一遍：它们靠特征谓词判定，不走 isaca_controls，
    # 否则一个标签打错就会连带把整个面报成已覆盖。
    facets_by_control: Dict[str, List[dict]] = {}
    for control_id, facets in FACETS.items():
        rows = []
        for facet in facets:
            hits = [case.id for case in items if facet.rule(case)]
            rows.append(
                {
                    "facet_id": facet.facet_id,
                    "label": facet.label,
                    "cases": len(hits),
                    "case_ids": hits[:5],
                    "note": facet.note,
                }
            )
        facets_by_control[control_id] = rows

    detail = {
        control_id: {
            "label": CONTROL_LABELS[control_id],
            "posture": POSTURE_BY_ID[control_id],
            "cases": len(ids),
            "case_ids": ids[:10],
            "facets": facets_by_control.get(control_id, []),
        }
        for control_id, ids in buckets.items()
    }
    # 顺序一律按目录顺序，不用 sorted：C10 排在 C1 后面比排在 C1 前面好读。
    gap_ids = [control_id for control_id in ALL_CONTROL_IDS if not detail[control_id]["cases"]]
    facet_gaps = [
        row["facet_id"]
        for control_id in ALL_CONTROL_IDS
        for row in facets_by_control.get(control_id, [])
        if not row["cases"]
    ]
    return {
        "total_cases": len(items),
        "controls": detail,
        "covered_ids": [control_id for control_id in ALL_CONTROL_IDS if detail[control_id]["cases"]],
        "gap_ids": gap_ids,
        "case_expected_gap_ids": [
            control_id for control_id in gap_ids if POSTURE_BY_ID[control_id] in (POSTURE_CASE, POSTURE_BOTH)
        ],
        "framework_only": [
            control_id for control_id in gap_ids if POSTURE_BY_ID[control_id] in (POSTURE_HARNESS, POSTURE_NONE)
        ],
        "facets": facets_by_control,
        "facet_gaps": facet_gaps,
        "facet_total": len(ALL_FACETS),
        "unmapped_case_ids": sorted(unmapped),
        "unmapped_count": len(unmapped),
    }


def load_coverage_summary(path: Optional[Path | str] = COVERAGE_MATRIX_PATH) -> dict:
    """从已落盘的矩阵提取报告与门禁要用的精简摘要。

    矩阵不存在时返回 `available=False` 而不是抛错：映射是后加的能力，老工作区
    和精简数据集跑评测不该因此失败。但「没生成矩阵」与「矩阵里 0 条覆盖」是两件
    不同的事，必须显式区分——否则报告会印出一张全 0 的空表，读起来像覆盖为零。
    """
    if not path:
        return {"available": False, "reason": "未指定矩阵路径"}
    target = Path(path)
    if not target.is_file():
        return {
            "available": False,
            "reason": f"{target} 不存在，先运行 python scripts/build_coverage_matrix.py",
        }
    try:
        matrix = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"{target} 读取失败：{exc}"}
    controls = matrix.get("controls") or {}
    return {
        "available": True,
        "path": str(target),
        "suite": matrix.get("suite", ""),
        "suite_sha256": matrix.get("suite_sha256", ""),
        "total_cases": matrix.get("total_cases", 0),
        "control_total": matrix.get("control_total", len(ALL_CONTROL_IDS)),
        "facet_total": matrix.get("facet_total", len(ALL_FACETS)),
        "covered_ids": matrix.get("covered_ids", []),
        "case_expected_gap_ids": matrix.get("case_expected_gap_ids", []),
        "framework_only": matrix.get("framework_only", []),
        "facet_gaps": matrix.get("facet_gaps", []),
        "unmapped_count": matrix.get("unmapped_count", 0),
        "override_unknown_ids": matrix.get("override_unknown_ids", []),
        "controls": {
            control_id: {
                "label": row.get("label", CONTROL_LABELS.get(control_id, control_id)),
                # label 形如「C4 网络分段·隔离·沙箱执行」，报告表格里要分列展示。
                "title": row.get("label", CONTROL_LABELS.get(control_id, control_id)).split(" ", 1)[-1],
                "posture": row.get("posture", POSTURE_BY_ID.get(control_id, POSTURE_NONE)),
                "cases": row.get("cases", 0),
            }
            for control_id, row in ((item.control_id, controls.get(item.control_id, {})) for item in CONTROL_CATALOG)
        },
    }
