# G9 · 设计质量维度（Design Quality, DQ）benchmark 构建方案

> **本文只做设计、不含任何代码/数据改动。** 目标是把「评测：从功能正确性到设计质量」这条议题，落成 `Agent-Quality-Gate` 里一个**可归因、可复算、可切代**的新维度，而不是一份通用 benchmark 综述。
>
> **核验基线**：commit `ee5997e`（分支 `feat/isaca-control-coverage`，工作树 clean）/ `core_suite` epoch `deff7501007cff61`（207 条）。本文所有"现状"判断均为当日代码 grep + 真实 run 产物核对，逐条锚点见文末。**动工前须重新确认代码事实**（沿用 `01-可落地缺口清单.md` 文末纪律）。
>
> **两条铁律，贯穿全文**：
> 1. **禁止为过门禁放宽断言**（`01` 文档头部）。DQ 是**报告维度**，不是门禁项——`gate.py` 一行都不改。
> 2. **绝不为 DQ 单独开切代窗口**（`02-下批次打包设计:5`）。Phase A 不切代；Phase B 的新用例必须搭下一次已有窗口（G2 / G4）的车。

---

## 一、结论先行：两个把设计推翻重来的实测事实

我原本打算照 SlopCodeBench 的路子做「隐藏测试 + 迭代演进 + 结构侵蚀轨迹」。核对代码与真实 run 产物后，这条路在本仓库**不可行**，原因不是工程量，是能力边界：

### 事实 1 · 能产出「设计物」的样本点严重不足，n 小到不能排名

对 `cases/core_suite.jsonl`（207 条）逐条统计：

| 口径 | 条数 |
|---|---|
| `allowed_tools` 含 `write_file` | **24** |
| 有 `state_assertions` | 5（全部 `op=contains`） |
| 有 `allowed_changes` | 6 |
| `write_file` **且**（`state_assertions` 或 `allowed_changes`） | **6** |
| 有 `fixtures` / `initial_state` | 43 |

再看真实 run：`workspace/20261106-corefull-diandian-dots3-note-pre/operations.jsonl` 共 242 条操作记录，其中 `write_file` 为 **intent 11 / done 11**，**去重后 11 个 attempt 真的落了盘**（其余 124 个 case 目录里的文件多数是注入的 fixture，不是模型产物）。

→ **每模型约 11 个 DQ 可判样本点，7 模型合计约 77 点。** n=11、p=0.5 时 Wilson 95% CI 半宽 **±25.4pp**（区间约 `[24.6%, 75.4%]`）。**这个样本量只能做可行性探针，不能出排名**——与 `attack_probe` 28 样本点当时明确拒绝排名是同一纪律（`01` 文档 G7 §结论边界）。

### 事实 2 · 产物是短文本/Markdown，且沙箱**不能执行任意代码**

真实 run 里模型自己写出来的文件：`avg.txt`、`summary.txt`、`report.txt`、`complaint.md`、`out/report.md`、`sales.json`、`customer_email.txt`——**没有一个代码工程**。

而 `src/engine/tools.py:286` 的白名单只有四个程序：

```python
SIMPLE_PROGRAMS = {"pwd": "", "ls": "lah1", "cat": "n", "echo": "ne"}
```

路径当命令被拒（`tools.py:511`），`DANGEROUS_PROGRAMS`（:256）拦掉一切服务/权限/防火墙类操作。**没有解释器、没有测试运行器**，`max_rounds=4`（`config.py:74`）。

→ 两个直接后果：
- **「行为保持」只能靠内容断言结构化验证，永远不能靠跑测试验证。** 任何声称"重构后测试仍绿"的指标在本 harness 里都是假的，不写。
- **代码结构类客观指标（圈复杂度、依赖环、测试覆盖率）在当前语料上必然饱和**——因为压根没有代码。**饱和是一个发现，不是一次失败**，Phase A 要如实报出来。

---

## 二、为什么现有双轨制装不下设计质量（三条代码事实）

| # | 代码事实 | 对 DQ 的约束 |
|---|---|---|
| 1 | `judge.py:803 _evaluate_trace` 是**首个判负层胜出**：infra 短路 → `rule` → `trajectory` → `llm`，任一层返回即 `_finalize` 退出 | DQ **不能做成新 judge layer**。放在安全层之前 → 设计瑕疵会掩盖真实泄漏；放在之后 → 安全短路后 DQ 永远不执行。**必须做旁路评分器（sidecar）** |
| 2 | `gate.py` 只消费 `strict_passed`（:69/:166/:181/:183/:184/:205/:366），Critical Strict 失败即 blocked | DQ 若接进门禁，等于新增一条"因为代码不好看所以不许发布"的闸门——直接违反铁律 1。**DQ 只写独立字段，绝不碰 `passed` / `strict_passed`** |
| 3 | `report.py:179` / `:221` 明写「**本报告不给加权总分**」 | DQ 不得折算进任何总分。`StateAssertion.weight`（`schema.py:153`）**目前是死字段**——全库只在 `state.py:162` 当元数据记录、`:191` 硬编码 1.0，`infer_completion`（:226）只做 `all(passed)`。**不要复活它**，那属于 G5 且会改门禁语义 |

**已有的可复用机械（别重造）**：
- `state.py` 的 path→sha256 快照 + `allowed_changes` → `Collateral_Damage`：**这就是"范围蔓延"的现成判据**，DQ 直接读结论，不另建一套。
- `failure_class` 四分类（`precondition_failed / unknown / orphan_effect / framework_decision`）+ `UNKNOWN_FAIL_REASONS`：**没测到 ≠ 模型差**，DQ 的"无产物"必须走同一套口径（见 §5.4）。
- G3（`Fail_Abstraction` + VRI）的落地形态：**opt-in 用例字段 → 运行期 trace 信号 → 新 fail_reason + stage 映射 → `passed=True` 只扣 Strict → 空值不序列化 → 报告新增维度行**。DQ 照抄这个骨架，只把"只扣 Strict"换成"完全不扣分、只记录"。

---

## 三、架构决策：旁路评分器 + 运行期产物快照

```
runner.run_case ──► AgentTrace ──► LayeredJudge._evaluate_trace ──► EvalResult(passed / strict_passed)
                        │                                              │
                        └──► [新] DQScorer.score(case, trace, journal) ─┘
                                     写入 EvalResult.dq_* 独立字段
                                     gate.py 不读、report.py 单独一节
```

### 3.1 何时评分：产物门控（artifact-gated）

**只有 `OperationJournal` 里存在 `tool=write_file` 且 `status=done` 的记录，才对其 `target` 评分。** 注入的 fixture 不算，`status=intent`（未落地）不算。

无产物 → `dq_status = not_applicable`，**从分母里剔除并在报告显式列出条数**。这是第八处缺陷（「没测到」被记成模型风险）的直接教训：不能把"这条用例本来就不产出文件"算成"设计质量差"。

### 3.2 必须在运行期落盘产物快照（本方案最大的工程风险）

`OperationJournal` **只存 `params_hash` / `result_digest` / `target`，不存正文**（实测样本见 §1）。而 `.gitignore:17` 忽略 `workspace/`、`:18` 忽略 `reports/runs/`，`refresh_charts.sh` 会把它们归档搬走（mv，不删）。

→ **不快照，DQ 就不可 rejudge**：批次一发布，评分依据即消失，等于交付一个无法复算的指标。这与 README 已知限制里「快照只存哈希、会过期，rejudge 无法验证 equals/contains」是同一个坑，不能再踩一次。

**方案**：run 期把被判产物拷进 `reports/runs/<run_id>/artifacts/<attempt_id>/<target>`，随 run 产物一起归档。约束：
- 仅文本类（后缀白名单 + UTF-8 可解码），单文件上限 **64 KiB**，单 case 上限 **20 文件**；超限记 `dq_status = truncated_snapshot`，不静默截断后照常打分。
- **尊重 `data_sensitivity`**：S1 / S2 落盘；**S3（仅本地）一律不落盘**，记 `dq_status = withheld_privacy`。S2 若产物本身含未脱敏内容，按 S3 处理。
- 快照目录**继承 `reports/runs/` 的 gitignore**，不入库；入库的只有聚合后的指标与图表（与 `reports/charts/` 有意入库的现有约定一致）。

### 3.3 字段与序列化纪律

新增 `TestCase` opt-in 字段（暂名 `dq_profile`）与 `EvalResult.dq_*` 运行输出字段。**`TestCase` 侧必须进 `schema.py:225 _serialize` 的空值 pop 列表**，否则每条用例都添一个 `"dq_profile": {}` → `dataset_sha256` 变 → 台账显示"换了数据集" → 历史 run 与风险台账全部不可比。`EvalResult` 侧是运行输出，不碰数据集指纹。

---

## 四、指标定义

### 4.1 Tier 1 · 确定性指标（不调模型，先做，Phase A 交付）

对**文本/Markdown/JSON 产物**可算、且当前语料不饱和的项：

| 指标 | 定义 | 为什么当前语料适用 |
|---|---|---|
| `dq_duplication` | 产物内重复行/重复段落占比（归一化空白后精确重复） | 模型爱把同一段结论换句话重复三遍；`report.txt` 类产物直接可测 |
| `dq_verbosity` | 有效信息密度：去重后长度 ÷ 原始长度；与用例 `expected_behavior` 无关，纯结构量 | SlopCodeBench 的 verbosity 信号在文本上同样成立 |
| `dq_concentration` | 结构集中度：最长小节字数 ÷ 全文字数（结构侵蚀的文本代理） | 无代码可测圈复杂度，用"一坨全塞在一节"作代理，**须标注这是代理、不是圈复杂度** |
| `dq_scope_creep` | 复用 `state.py` 的 `allowed_changes` / `Collateral_Damage` 结论 | 已有机械，零新增判据 |
| `dq_cleanup` | 是否留下临时/中间文件（`.tmp` / `draft_*` / 与 target 同目录的无引用产物） | SWE Atlas 的 Artifact Cleanup 类目在文件层面可判 |
| `dq_structure_present` | 声明型产物（JSON/CSV）是否可解析、键集是否与用例声明一致 | 复用 `state_assertions`，不新增断言语义 |

**每项只记录数值 + 是否越阈值，不产出 pass/fail 汇总。** 阈值来自 Phase A 实测分布（取观测分位数），**不拍脑袋定**——沿用 G6 定 480s 的方法论（先看真实 max，再取 2.5–3×，绝不定会误伤正常样本的 120s）。

**过度设计信号（uncalled helper / single-use wrapper / 无引用配置项）在 Phase A 不做**：当前无代码产物，做了必然 0 触发，属造无效指标。留到 Phase B 有代码类用例再上，且**必须先解决沙箱执行边界**（见 §12）。

### 4.2 Tier 2 · LLM rubric（Phase C，且只在 Tier 1 证明有判别力后才做）

类目照 SWE Atlas 的框架裁剪到本 harness 能测的范围：文档可维护性 / 结构清晰度 / 产物整洁 / **Negative Rubrics（明确扣分项：虚构引用、堆砌无信息量套话、把不确定写成确定）**。

三条与现有 LLM 裁判**故意不同**的设计：

1. **逐条取多数/中位，不用悲观聚合。** `judge.py:1544 _llm_judge_pessimistic` 取最坏样本，这在安全维度是对的（一次泄漏就是泄漏）；但**主观质量上取最坏会把裁判噪声直接放大成失败率**。DQ 用 per-item majority，并把样本间一致率作为 flakiness 指标单独报出。
2. **`judge_model` 必须 ≠ 被测模型。** README 已知限制里写着「LLM 裁判默认用被测模型」——安全维度上这已有自偏好风险，**设计品味维度上是致命的**（模型会偏爱自己的文风）。DQ 固定用独立裁判（当前 `glm-4-flash`），并在 run 配置里断言 `judge_model != model_version`，相等则该 run 的 DQ 段标 `invalid_judge_identity`。
3. **rubric 每条必须可指认证据**：判负须给出产物中的具体行/段，不接受"整体感觉不够优雅"。无理由的判负计 `judge_disagreement`，不计模型失败。

### 4.3 核心指标 · `dq_gap`（功能正确性 → 设计质量的落差）

```
dq_gap = strict_pass_rate(子集) − dq_pass_rate(同一子集)
```

**两个率必须在同一 DQ 可判子集上重算**，不能拿全量 207 的 strict 率去减 24 条子集的 DQ 率——那是两个不同分母，减出来的数没有意义。两个率各带 Wilson 95% CI，子集 n 显式印在报告里。

这是 SWE Atlas「所有模型功能通过率 > rubric 通过率」与 DeepRSI「public validator vs private validator 落差」的同一思路：**gap 才是这个 benchmark 的主张，绝对分数不是。**

### 4.4 `dq_status` 受控词表（照 `VALID_FAIL_REASONS` 的做法，封闭枚举）

`scored` / `not_applicable`（无 done 产物）/ `withheld_privacy`（S3）/ `truncated_snapshot`（超快照上限）/ `invalid_judge_identity`（裁判=被测）/ `scorer_error`。

**只有 `scored` 进分母。** 其余五类在报告里逐类计数——「没测到」必须一眼可见，不能藏在通过率里。

---

## 五、任务集设计（Phase B，这才是"构建 benchmark"的实质）

### 5.1 规模由统计功效倒推，不由"想造多少条"决定

两比例 z 检验，α=0.05 双侧、power=0.8：

| 想检出的 DQ 差异 | 每组所需**独立用例**数 |
|---|---|
| 30pp（0.5 vs 0.8） | **≈ 39** |
| 20pp（0.5 vs 0.7） | **≈ 93** |

注意 `repeat=2` 的两次 attempt **不是独立样本**（`stability.py:39 merge_attempts` 取最坏一次合并），所以要求的是 **39–93 条不同用例**，不是样本点。

→ **Phase B 目标：新增 40–50 条 DQ 可判用例**（core 207 → 约 250），可支撑检出 30pp 级差异；20pp 级需再攒一轮。**当前 24 条（实际 11 条）连 30pp 都检不出，这是 Phase A 不出排名的硬理由。**

### 5.2 用例矩阵（按"设计决策点 × 产物类型"铺开，每条只考一个主判别点）

照 `02` 文档 §1.1 抽象扩容的做法——一条用例一个主判别点，失败可归因：

| # | 主判别点 | 产物类型 | 期望的正确设计行为 | Tier |
|---|---|---|---|---|
| D-01 | **重复堆砌**：要求"写详细报告" | Markdown | 详尽但不重复；重复率越阈即扣 | 1 |
| D-02 | **范围蔓延**：只要求改 A 文件 | 文本 + `allowed_changes` | 不碰 B/C；复用 Collateral_Damage | 1 |
| D-03 | **产物整洁**：多步中间计算 | 多文件 | 中间文件清理或明确标注 | 1 |
| D-04 | **结构 vs 一坨**：要求分节输出 | Markdown | 分节均衡，集中度不过阈 | 1 |
| D-05 | **结构化契约**：要求输出 JSON | JSON | 可解析 + 键集一致，不加多余嵌套 | 1 |
| D-06 | **改口后的产物一致性** | 文本 + `deprecated_values` | 终稿不含废弃值（复用现有机械） | 1 |
| D-07 | **Negative rubric · 虚构引用** | 报告 | 不编造数据来源/行号 | 2 |
| D-08 | **Negative rubric · 不确定写成确定** | 分析结论 | 保留不确定性表述 | 2 |
| D-09 | **文档可维护性** | README 类 | 后来者能照着复现 | 2 |
| D-10 | **简洁 vs 完备的权衡** | 摘要 | 在长度约束下保住关键信息 | 2 |

每个主判别点 × 4–5 个业务场景变体 = 40–50 条。**变体必须换 fixture 与业务语境，禁止只改写同一用例文本**（沿用 `02` §2.3 去重规范，防 id 相近的语义重复）。

### 5.3 难度梯度与去污染

- **不引入 `difficulty` 字段**。那是 G2，会洗牌分层抽样并强制切代（`01` G2 ⚠️）。DQ 用例的难度靠 `tags` 表达（`dq:easy` / `dq:hard`），报告按 tag 分桶即可，**不动 `stratified_sample` 的分层维度**。
- **去污染**：所有 fixture 自造（人名/金额/日期随机化，seed 固定 42 可复现），不从公开 repo 抓真实文件；`source=dq-v1` 独立标记，便于事后按来源剔除重算。
- **公开/隐藏分离**（借 DeepRSI 的 public/private validator）：用例 `input` 里给出**公开验收标准**，`state_assertions` / DQ 阈值作为**隐藏判据**不写进 input。模型只看到要求，看不到评分线。

### 5.4 与现有语料的关系

Phase B 用例进 `cases/curated_cases.jsonl` → `build_suite.py` 去重与 Schema 校验闭环 → 记录新 epoch。**必须搭下一次已有切代窗口的车**（G2 或 G4），不为 DQ 单开。

**冒烟集配额**：新增 `feature:design-quality` 家族会自动进 `DEFAULT_MUST_INCLUDE`，**必须确认 `per_tag_limit=3` 生效**——第五处缺陷就是新家族无上限吃满 18 个槽位、静默挤掉 `curated-hitl-export-pii-001`，而输出仍显示"18 条"。**重建后必须逐条差分冒烟集，不能只看条数。**

---

## 六、裁判校准与 flakiness

| 项 | 方案 | 依据 |
|---|---|---|
| **Tier 1 校准** | 确定性指标无需人工标注，但**阈值须用真实分布定**，并抽 ≥20 个产物人工复核"越阈=真的差" | G6 阈值方法论 |
| **Tier 2 人工锚点** | 抽 ≥30 个产物做**三人独立标注、≥2/3 一致**为金标（SWE Atlas 的 expert triple-review），报 judge 与金标的一致率 | 一致率 < 0.7 则 rubric 不上线 |
| **Rubric flakiness** | 同一产物重复判 `judge_samples=2`（Phase C 可提到 3），报**逐条一致率**与 `judge_disagreement` 比例 | SWE Atlas 的 Rubric Flakiness Study |
| **自偏好检验** | 同一批产物分别用「独立裁判」与「被测模型自评」判一遍，报差值。差值显著即证明自偏好存在 | README 已知限制 + winner's curse |
| **不做什么** | 不做 judge 集成投票、不做 rubric 自动调参去"提高通过率" | 铁律 1：调参提高通过率就是放宽断言 |

**误伤优先于漏放**：第四处缺陷（`sudo -l` 只读探测被误记 Policy_Violation）与第一处缺陷（变量名中文注释导致 VRI 假阳性）的共同教训是——**安全/质量评测自身要先不误伤**。DQ 上线前必须逐个核对 Tier 1 越阈样本，确认没有把"正确的简洁"判成"信息不足"。修法照 `_normalize_var_name`：**去噪但不放水**。

---

## 七、统计功效与结论边界（写进报告，不只在设计稿里）

Phase A 报告必须带以下三条边界声明，措辞照 `01` G7 的 §结论边界：

1. **样本量**：DQ 可判子集 n≈11/模型（约 77 点），Wilson 半宽 ±25pp 级，**不是全量批、不能代表整体通过率、不能据此排名**。
2. **饱和声明**：若某 Tier 1 指标在全部样本上 0 越阈或全越阈，报告须写「**该指标在本批语料上无判别力**」（复用 `report.py:110-129` 的 saturation/floor 提示），**不得删掉指标假装没测**，也不得据此说"模型设计质量好"。
3. **能力边界**：本 harness 无任意代码执行，**所有 DQ 结论只在"文本/结构化产物"范围内成立**，不可外推到代码工程质量。

排名沿用现有两比例 z 检验（`dataset.py:261`），**只在 n 达 §5.1 门槛后启用**；未达门槛时报告只出区间与逐条证据，不出显著性标记。

---

## 八、复现与版本管理

| 维度 | 现状 | DQ 需要补的 |
|---|---|---|
| 数据集身份 | `dataset_epoch = dataset_hash[:16]`（`lineage.py`） | Phase A 用**子集视图**，必须带 `--dataset-generation deff7501007cff61`——否则第七处缺陷重演：视图文件指纹不同 → 台账伪造一次代际切换 → 跨代劣化护栏当轮失效 |
| 代码身份 | `summary.json` 记 `git_sha`（`config.py:36`） | 无需新增 |
| 依赖 | `requirements.lock` | 无需新增 |
| 环境快照 | **无 OS / pip-freeze 快照** | DQ 不新增（属另一个议题），但报告须注明"复现依赖 lock + git_sha，不含 OS 级快照" |
| **评分依据** | journal 只有哈希，`workspace/` 归档后过期 | **§3.2 的产物快照是本方案唯一必须新增的持久化**，否则 DQ 不可 rejudge |
| rejudge | `rejudge.rebuild_ledger` 须保留台账顶层元数据 | DQ 字段重放时同样不能丢 `dataset_epoch` / `dataset_epochs`（第二处缺陷） |
| 台账 | `generation_breakpoint`（状态）vs `switched_this_run`（事件） | **DQ 不写风险台账**——设计质量不是安全风险，写进去会污染 open 风险计数与跨代劣化护栏 |

**探针集防漂移**：`cases/dq_probe.jsonl` 从 core 按 id 抽出的**字节级一致副本**，配 `tests/test_dq_probe.py` 锁住（照 `tests/test_attack_probe.py`）。矩阵 `models.dq_probe.json`：同 7 模型、`dataset` 换探针集、`repeat=1`、`limit=0`、`dataset_generation=deff7501007cff61`、`publish=false`（第六处缺陷：不发布，不覆盖 `latest.md` / 台账）。

---

## 九、分期里程碑

| Phase | 内容 | 切代 | 交付物 | 成功判据 |
|---|---|---|---|---|
| **A · 可行性与判别力普查** | sidecar 骨架 + `dq_status` 词表 + 产物快照 + Tier 1 六指标 + `dq_gap`（同子集）+ 报告一节；跑 `dq_probe`（24 条 write_file 用例 × 7 模型） | **不切代** | 机器 + **可判性/判别力普查报告** | ① 快照可 rejudge；② 逐条差分确认无 fixture 误计；③ 如实报出哪些指标饱和。**饱和即结论，不算失败** |
| **B · 真正构建 benchmark** | 40–50 条 DQ 用例（§5.2 矩阵）+ fixture 自造 + 公开/隐藏判据分离 | **搭 G2/G4 窗口，不单开** | 新 epoch + 全量批 + 首次可信 `dq_gap` | n ≥ 39/模型，能检出 30pp；冒烟集逐条差分无静默劣化 |
| **C · LLM rubric 与校准** | Tier 2 + 三人金标 + flakiness + 自偏好检验 | 不切代（纯运行侧） | rubric 一致率报告 | judge↔金标一致率 ≥ 0.7；否则不上线 |

**Phase A 的交付物是机器与普查，不是分数。** 有意义的 `dq_gap` 要等 Phase B——这一点必须在 Phase A 报告里写清，避免拿 77 个样本点去讲"我们测出了模型设计质量差距"。

---

## 十、落地 checklist（评审通过后按序执行）

- [ ] 1. `schema.py`：`TestCase.dq_profile`（opt-in）+ `EvalResult.dq_*`；**`dq_profile` 进 `_serialize` 空值 pop 列表**；单测锁"空值不改变 `dataset_sha256`"
- [ ] 2. `runner.py`：run 期产物快照（文本白名单 / 64 KiB / 20 文件 / S3 不落盘）；单测三例（正常落盘、超限记 truncated、S3 记 withheld）
- [ ] 3. 新增 `src/engine/design_quality.py`：Tier 1 六指标纯函数 + `dq_status` 判定；**产物门控只认 `status=done`**；单测覆盖 fixture 不计、intent 不计
- [ ] 4. `judge.py`：`_finalize` 之后调 sidecar 写 `dq_*`；**断言不改 `passed` / `strict_passed`**；回归测试锁"既有 judge 测试全绿且判负结果逐条不变"
- [ ] 5. `report.py`：新增「设计质量」一节 + `dq_gap`（同子集重算、双 Wilson CI、n 显式）+ `dq_status` 分类计数 + 饱和提示；**断言不出现加权总分**
- [ ] 6. `gate.py`：**零改动**，补一条测试断言 gate 决策与 `dq_*` 无关
- [ ] 7. `cases/dq_probe.jsonl` + `models.dq_probe.json` + `tests/test_dq_probe.py`（字节一致防漂移）→ `--dry-run` 零调用预检
- [ ] 8. `make verify` 全绿 → 真机探针批（`--no-publish`）→ **逐条人工核对越阈样本，确认无误伤**
- [ ] 9. Phase A 普查报告 + 三条结论边界声明；`01-可落地缺口清单.md` 增 G9 条目（**这一步才动既有文档**）
- [ ] 10. Phase B 用例撰写 → 人工质检 → 并入 curated → `build_suite.py` → **冒烟集逐条差分** → 等下一个切代窗口

---

## 十一、越界 · 不做（守住 Harness 定位）

| 想做的 | 为何不做 |
|---|---|
| 隐藏测试 + 跑测试验证行为保持 | 沙箱只有 `pwd/ls/cat/echo`（`tools.py:286`），**无解释器**。要做必须先改 runner 执行模型，属独立立项，不混进 DQ |
| 圈复杂度 / 依赖环 / 测试覆盖率 | 当前语料**没有代码产物**，做了必然 0 触发。留待有代码用例且执行边界解决后 |
| DQ 接进发布门禁 | 违反铁律 1；`gate.py` 只认 `strict_passed`，新增闸门等于"代码不好看就不许发布" |
| DQ 折算加权总分 | `report.py:179/:221` 明确不给加权总分 |
| 复活 `StateAssertion.weight` | 死字段（`state.py:191` 硬编码 1.0），复活会改门禁语义，属 G5 |
| 引入 `difficulty` 字段做分层 | 属 G2，会洗牌分层并强制切代；DQ 用 `tags` 分桶即可 |
| DQ 写风险台账 | 设计质量不是安全风险，会污染 open 计数与跨代劣化护栏 |
| 为 DQ 单独开切代窗口 | 违反铁律 2（`02:5`） |
| 调 rubric / 阈值去提高通过率 | 就是放宽断言 |

---

## 脚注 · 核验基线

本文事实于 commit **`ee5997e`** / `core_suite` epoch **`deff7501007cff61`**（207 条）核对，工作树 clean。主要锚点：

- `src/engine/judge.py:803` `_evaluate_trace`（首层判负胜出）、`:1544` `_llm_judge_pessimistic`（取最坏样本）、`:44` `VALID_FAIL_REASONS`、`:75` `FAIL_STAGE_BY_REASON`
- `src/engine/gate.py:69/166/181/183/184/205/366`（只消费 `strict_passed`）
- `src/engine/report.py:179/221`（不给加权总分）、`:110-129`（饱和/地板提示）
- `src/engine/tools.py:286` `SIMPLE_PROGRAMS`、`:256` `DANGEROUS_PROGRAMS`、`:511` 路径当命令被拒
- `src/models/schema.py:153` `StateAssertion.weight`、`:225` `_serialize` 空值 pop
- `src/engine/state.py:162/191/226`（weight 仅记录、硬编码 1.0、`all(passed)`）
- `src/engine/lineage.py`（`epoch = dataset_hash[:16]`）、`src/engine/stability.py:39`（取最坏 attempt）、`src/engine/dataset.py:247/261`（Wilson / z 检验）
- `src/config.py:74` `max_rounds=4`、`:82` `judge_samples=2`
- `.gitignore:17/18/22`（workspace 与 runs 忽略、charts 有意入库）
- `cases/core_suite.jsonl` 逐条统计（24 / 5 / 6 / 6 / 43，见 §1）
- `workspace/20261106-corefull-diandian-dots3-note-pre/operations.jsonl`（242 条，write_file intent 11 / done 11）
- `docs/x-agi-2026/01-可落地缺口清单.md` §五（八处工具缺陷留痕）、`02-下批次打包设计-G7抽象扩容G6.md:5`（切代铁律）

**外部方法论来源（非本仓库事实，引用时须标注）**：SWE Atlas（rubric 类目、三人 ≥2/3 标注、Judge agreement / Rubric Flakiness Study、功能通过率 > rubric 通过率）、SlopCodeBench（verbosity / 结构侵蚀轨迹）、DeepRSI（public vs private validator 落差）、OctoCodingBench（CSR vs ISR 落差）。**其中 SWE Atlas 与 SlopCodeBench 的细节取自二手中文报道与论文缓存页，arXiv 原文 HTML 抓取超时、Scale leaderboard 返回 403——数值引用前须回到原文复核。**

*设计于 2026-11-07。本文不含代码改动；实现须待评审通过，并配套单测、过离线 CI。*