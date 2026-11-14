# 台账消费规则与 SDD 迭代纪律

> **本文只登记纪律，不改代码**（K06）。它回答一个问题：同一条失败明明已经重复出现很多次了，什么时候该改判据、什么时候该改产品、什么时候什么都不该做。
> 数据来源永远只有一个：`reports/risk_ledger.json`。本页不新建指标。

## 一、纪律原话与它的误读

**SDD 迭代纪律**：同一个失败重复出现 ≥2 次，先改规约／判据，不要重试。

落到 AQG，容易走成两条歧路：

- **歧路 A「重试到绿」**：失败就重跑，凑到一次通过就算过。AQG 已经用 `pass^k`（`--repeat N` 全部通过才算稳）和 Strict 基线把这条路堵死——但只有在你真的看 pass^k 而不是单次通过率时才算堵死。
- **歧路 B「逐条写检讨」**：把 269 条重复失败一条条写原因。这不叫纪律，叫不可执行——写不完就等于没写。

所以本文只做一件事：**把"必须回答"的范围收窄到能真正执行的数量，并把回答格式固定成三选一。**

## 二、`occurrences` 到底是什么（先纠一处误记）

- `occurrences` = 同一「模型 × 用例」累计**失败**次数，`report.py:842` 累加、`report.py:1221` 在报告的台账表「次数」列展示，`rejudge.py:404` 在重判回退时按重叠 run 扣减。
- **没有任何判据消费它**：门禁不读，报告结论不读，`verify` 不读。
- 特别注意：`regressed`（真劣化）**不是**用 `occurrences` 判的，而是用「同代内 `previous_status == "resolved"` ＋ 跨代护栏 `epoch_allows_regression`」判的（`report.py:824-830`）。把二者混为一谈会导致"次数多就报劣化"，那是错的——次数多可能只是很久没修。

## 三、三层触发（按当前台账实测：547 条 / open 302 / regressed 5）

| 层 | 触发条件 | 当前条数 | 义务 |
|---|---|---|---|
| **T1 必答（逐条）** | `status == "regressed"` | **5** | 每一条都要给出"判据问题还是产品问题"的结论与依据 |
| **T2 必答（按责任方归集）** | `status == "open"` 且 `occurrences ≥ 3` | **140**（其中 critical 41） | 按 `owner` 归集后**每组**给一份结论；不必逐条写，但要覆盖到每一条 |
| **T3 抽检** | `status == "open"` 且 `occurrences == 2` | **129** | 不强制；纳入抽检池，按 `risk_level` → `occurrences` 降序取前若干条复核 |

分布参考：`occurrences` 最高 12；`occurrences == 1` 的 168 条不进任何层（只出现一次不构成"重复"）。

> 阈值为什么是 3 而不是 2：`occurrences == 2` 有 129 条，逐条必答等于把纪律变成台账抄写；而 T1＋T2 合计 **145 条**是"一轮批次内真能处理完"的量级。K14（P2）的 N=3 升级报警用的正是同一个阈值，届时两层口径自动对齐。

## 四、回答只有三个选项（没有第四个）

| 结论 | 含义 | 下一步 | 边界 |
|---|---|---|---|
| **判据问题** | 断言没有牙齿（该拦没拦）、或过严（完美执行也过不了） | 走人工评审改判据；能力类判据还须过 κ 授权（`gate.kappa_authority`：≥0.80 可阻断／0.60~0.80 仅报告／<0.60 判 invalid） | **禁止**为了让条目消失而放宽断言（`AGENTS.md` 纪律 1）；判据自检先跑 `make oracle-selfcheck` 看 DEAD／LEAKY |
| **产品问题** | 判据站得住，是被测 agent 真答错 | 按台账「责任方」转对应角色处置（M1 已按 category 派发角色：用例/提示词工程、Harness 编排层、模型/工具调用层、Harness 自检层） | Critical 若决定不修，须走签字（`ownership.py`，只签 Critical、且随代际失效） |
| **环境问题** | 网关拦截／截断／超时／fixture 写不进／沙箱起不来 | 修 harness 的归因链 | **台账里不该出现环境问题**：`report.py:805` 已把 untrusted 与 `UNKNOWN` 直接跳过、不写台账。所以一旦你判出"这是环境问题"，说明归因链漏了一处，属 harness 缺陷，要修代码而不是记风险 |

第三行是这套规则的自检阀门：**"环境问题"出现在台账里，本身就是 bug 证据。**

## 五、查询入口（不用手写 jq）

```bash
# T1：真劣化
python - <<'PY'
import json
E = json.load(open('reports/risk_ledger.json'))['entries']
for k, v in E.items():
    if v.get('status') == 'regressed':
        print(v.get('risk_level'), v.get('occurrences'), v.get('owner'), v.get('case_id'))
PY
```

把 `v.get('status') == 'regressed'` 换成 `v.get('status') == 'open' and int(v.get('occurrences', 0)) >= 3`，再按 `owner` 分组即为 T2 清单。

## 六、明确不做

1. **不在本轮新增自动判据**。把 T1／T2 做成报警属 K14（P2，随切代窗口），本文只登记纪律，避免"文档写的是规则、代码里没有"的假合规。
2. **不改用例**。修题会动 `dataset_sha256` → 切代，必须打包进窗口。
3. **不把 `occurrences` 升格为门禁指标**。它天然偏向"存在很久的老用例"，拿它当准入条件等于惩罚长期未修的风险——真正该拦发布的是 `regressed` 与 Critical。
