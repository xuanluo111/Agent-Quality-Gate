# AGENTS.md — Agent 入职手册

> 仓库即上下文。本文件**只做路由与纪律复述**，不新造规则：任何一条都能在它指向的文档或代码里找到权威出处，两处冲突时**以 `README.md` 与 `src/` 为准**，并回来改本文件。
> 适用范围：所有在本仓库工作的 agent（人或模型）。读完这一页，再去读你任务对应的那一节。

## 一、这是什么

一个**门禁**，不是评测平台：对工具型 AI Agent 做可复现的离在线评测，审计完整执行轨迹（工具调用、数据路由、人工确认、成本预算、分层裁判、双轨门禁、风险台账），并在证据不足时**失败关闭**。

- 权威口径：`README.md`（中文，权威版）／`README.en.md`（对等英文版）
- 参与开发：`CONTRIBUTING.md`（分支、提交前检查、切代约定）
- 安全边界：`SECURITY.md`（沙箱不是安全边界、S2 脱敏口径）

## 二、三条命令起步

```bash
make test              # 离线单测，当前 636 条
make verify            # 离线层全 13 项自检（0 成本，不调模型）
make oracle-selfcheck  # C01 判据自检：题目本身可不可信（0 成本）
```

真实评测会产生费用，须先有 `.env`；见 `README.md`「快速开始」。**不要在没有明确指令的情况下跑真实批次。**

## 三、七条不可协商的纪律

| # | 纪律 | 出处 |
|---|---|---|
| 1 | **禁止为过门禁放宽断言**。新增判据只能让门禁更严或持平；让 `verify` 或判据自检"变绿"不是修改用例的理由 | `CONTRIBUTING.md`「评测用例与切代」 |
| 2 | **绝不为单项改动单独开切代窗口**。改题面/断言会动 `dataset_sha256` → 切代，必须打包进同一窗口 | `CONTRIBUTING.md`；当前代际 `deff7501007cff61` |
| 3 | **环境类故障不是模型失败**。网关拦截、截断、超时、fixture 写不进、沙箱起不来都记 `precondition_failed` / `UNKNOWN`，剔出分母；能力类判据才受 κ 授权约束 | `src/engine/failure_class.py`、`src/engine/gate.py` |
| 4 | **改 README 的事实性内容（数字、能力、判据、限制）必须同提交更新 `README.en.md`**；来不及就显式写滞后声明，不允许静默漂移 | `CONTRIBUTING.md`「双语文档」 |
| 5 | **含业务逻辑的代码必须配套单元测试**；文档里的 mermaid 须过校验（≤8.8.3、`classDef`、禁 ASCII 图），没跑解析器不许说"图没问题" | `CONTRIBUTING.md`「提交前检查」 |
| 6 | **引用外部材料只引定义与结论句，不引数字**；讲者现场数据一律"未独立核验"。项目内实测数字必须可复现，禁止"看起来正常" | `docs/kuaishou-tech-talk-06/00-待做清单.md` 三条铁律 |
| 7 | **门禁只在每台机器上**：没有 pre-commit 钩子、CI 不调模型、`run_batch.py` 只按模型消费门禁结果。发不发得看执行者是否真跑了并读了退出码 | `README.md`「已知限制」 |

## 四、按任务找文档

| 你要做什么 | 先读 |
|---|---|
| 改判据 / 裁判 / 门禁阈值 | `README.md`「裁判语义」「发布门禁」；`src/engine/judge.py`、`gate.py`；改动需 κ 授权口径（`gate.kappa_authority`：≥0.80 可阻断 / 0.60~0.80 仅报告 / <0.60 判 invalid） |
| 动用例、加用例、修题 | `README.md`「数据集」；`CONTRIBUTING.md`「评测用例与切代」；`docs/quality/case-lint-exemptions.json`（存量缺陷登记，改 case 前先看是否在豁免里） |
| 看重复失败该怎么处置 | `docs/quality/02-台账消费与SDD迭代纪律.md` |
| 给 AQG 自己立验收 | `docs/quality/01-自验收与规约五层.md` |
| 查外部意向的落地状态 | `docs/priority-2026q4/00-待做优先级.md`（排序与成本）、`docs/priority-2026q4/01-每日排期.md`（实时进度，唯一进度源） |
| 改报告 / 看板 | `README.md`「运行产物」「可视化」；`src/engine/report.py` |
| 换模型 / 配并发 | `README.md`「配置」「多模型批量评测」；免费账户一律 `--workers 1`~`2` |

## 五、提交前最小清单

1. `make test` 全绿（新增逻辑带测试）；
2. `make verify` 13 项全绿；
3. `git status` 里没有 `.env`、`models.json`、真实日志、`workspace/`、`reports/runs/`；
4. 改了 README 事实 → 英文版同步；
5. 提交信息写**为什么**与**验证方式**（跑什么、结果数字），沿用 `type(scope): 一句话`。

## 六、术语速查

| 术语 | 含义 |
|---|---|
| 代际 / `dataset_epoch` | 数据集文件 SHA256 前 16 位。跨代不判劣化，只重新基线为 `open` 并记 `rebaselined_from_epoch` |
| `regressed` | **同一代内** `resolved → open`，即真劣化；跨代失败不算 |
| `occurrences` | 台账里同一模型×用例累计失败次数，目前**只计数与展示**，无判据消费 |
| `pass^k` | 同一用例 N 次独立重复全部通过；`--repeat N` 时门禁可用它替代单次通过率 |
| 变序采样（K08） | 第 1 次尝试用 `--seed` 原值，之后按质数步长派生新种子；否则 N 次同种子 → 稳定性虚高 |
| `precondition_failed` | 环境故障标记：不计入模型能力分母 |
| Strict / Standard | 双轨判定：Standard 只看有无实际危害，Strict 要求全过程合规；**发布门禁用 Strict** |
