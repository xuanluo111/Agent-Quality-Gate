# docs 索引

> **本目录不是用户手册**。用户手册是根目录 [`README.md`](../README.md)。这里放四类用途完全不同的东西，改之前先看「谁读它」那一列——第一类改错会直接改变门禁结论。

## 零、单一事实源（说法冲突时按这张表判）

| 问题 | 唯一来源 | 其他文件不做什么 |
|---|---|---|
| 现在做到哪了 | [`priority-2026q4/01-每日排期.md`](priority-2026q4/01-每日排期.md) §二 | 不在清单里另记进度 |
| 先做哪一个 | [`priority-2026q4/00-待做优先级.md`](priority-2026q4/00-待做优先级.md) P0–P4 | 不各自排序 |
| 这个数字能不能对外说 | [`priority-2026q4/02-11-19对比批口径声明.md`](priority-2026q4/02-11-19对比批口径声明.md) §二 | 不单引半张表 |
| 这条待办从哪来、依据什么 | §三 的五源清单 | 不记状态、不记进度 |
| 门禁自己怎么验收 | [`quality/01-自验收与规约五层.md`](quality/01-自验收与规约五层.md) | — |
| 台账重复失败怎么处置 | [`quality/02-台账消费与SDD迭代纪律.md`](quality/02-台账消费与SDD迭代纪律.md) | — |

## 一、被代码或 CI 读取（改错会坏）

| 文件 | 谁读它 | 改动后果 / 注意 |
|---|---|---|
| `quality/case-lint-exemptions.json` | `src/engine/case_lint.py:17`、`scripts/lint_cases.py`、`scripts/verify.py:465`、`.github/workflows/aqg-ci.yml` | 新增豁免＝允许一条已知缺陷题留在库里进入分母。豁免之外多一条 error／warning，`verify` 直接失败 |
| `c02-judge-calibration/labels.template.jsonl` | `scripts/build_label_template.py` 生成 | 从真实批次抽 40 条平衡样本。改它等于换标注集，必须重标 |
| `c02-judge-calibration/labels.filled.jsonl` | `build_label_template.py --check` 校验 | 人工标注真值，`--check` 会拒空标签与重复 |
| `c02-judge-calibration/review-sheet.md`（1053 行） | 人读（标注时用） | 脚本重新生成即覆盖，**不要手改** |
| `c02-judge-calibration/calibration.json` | 人读（κ 的唯一证据） | **不是运行时输入**：`config.py` 的 `judge_kappa` 默认 `None`，跑批须用 `--judge-kappa 0.8412` 显式传。重跑校准后必须同步批命令，否则门禁还在用旧 κ |
| `isaca-securing-ai-agents/mindmap.json` | `src/engine/controls.py:34`（id／label 同步基准） | 控制项编号是 `controls.py` 的对照物；改编号会连带改覆盖矩阵与 `verify` 的「控制覆盖」项 |
| `isaca-securing-ai-agents/build_mindmap.py` | 人工执行，生成 `mindmap.html`／`.json`／`.png` | 生成物不必手改；改源头是这份脚本 |

## 二、实现设计与偏差记录

| 文件 | 记什么 | 为什么值得留 |
|---|---|---|
| [`c01-oracle-selfcheck/00-设计.md`](c01-oracle-selfcheck/00-设计.md) | C01 判据自检的设计 ＋ **锚点更正** ＋ **实现回执** | 设计稿原写「`positive_reference`／`negative_reference` 已有」，实际**全仓零命中**；「走确定性闸门链还是整条 `judge.evaluate`」也是实现时才定的。这种设计与实现的不一致，只有留档能复原 |
| [`x-agi-2026/02-下批次打包设计-G7抽象扩容G6.md`](x-agi-2026/02-下批次打包设计-G7抽象扩容G6.md) | 下批次怎么打包、为什么必须同批切代 | 解释「绝不为单项单独开切代窗口」这条纪律的来源 |
| [`x-agi-2026/03-设计质量维度G9-benchmark构建方案.md`](x-agi-2026/03-设计质量维度G9-benchmark构建方案.md) | G9 设计稿（297 行，**未实施**） | P4 项，等「有可判对象」才开工 |

## 三、外部材料 → 待办（只记来源与依据，不记进度）

| 文件 | 来源 | 条数 |
|---|---|---|
| [`ccf-2026-chinasoft/00-待做清单.md`](ccf-2026-chinasoft/00-待做清单.md) | CCF-2026 ChinaSoft 评测技术论坛 6 场 | C01–C26 |
| [`kuaishou-tech-talk-06/00-待做清单.md`](kuaishou-tech-talk-06/00-待做清单.md) | 快手第六期技术沙龙 3 篇 | K01–K20 |
| [`isaca-securing-ai-agents/00-差距与优化路线.md`](isaca-securing-ai-agents/00-差距与优化路线.md) ＋ [`prompts.md`](isaca-securing-ai-agents/prompts.md) | ISACA《Securing AI Agents》(2026) | ISACA-C1–C11 ＋ 附录 A；`prompts.md` 把控制项翻译成**可喂 `generate_cases.py` 的用例生成提示词**，是资产不是笔记 |
| [`x-agi-2026/01-可落地缺口清单.md`](x-agi-2026/01-可落地缺口清单.md) | X-AGI 2026 讲者分享 8 篇 | G1–G9（按已实现／真缺口／越界三档） |
| [`qwen-eval-2610/00-待做清单.md`](qwen-eval-2610/00-待做清单.md) | 千问 Qwen4-Max-Preview 专业评测活动规则（截止 2026-11-18 23:59） | Q01–Q16；**唯一带硬截止的一份**，过期整份失效 |

**这几份不维护状态**。它们的价值是「每条待办的出处与依据可回溯」，状态在 §四。历史教训：快手清单正文写 K01–K18、脚注写 K01–K20，两周无人发现。千问这份是例外中的例外：截止日早于 11/19 批日、**每人仅一次有效提交**，所以它的时间点也写进了 `priority-2026q4/01-每日排期.md`（进度仍只认那一处，本文不另记）。

## 四、项目治理

| 文件 | 用途 | 更新时机 |
|---|---|---|
| [`priority-2026q4/00-待做优先级.md`](priority-2026q4/00-待做优先级.md) | 四源去重后的统一排序（P0–P4，共 48 项）＋ §九 更正记录 | 增删项、改排序、发现前提错误时 |
| [`priority-2026q4/01-每日排期.md`](priority-2026q4/01-每日排期.md) | **唯一进度源**（§二 进度总览） | 每天 |
| [`priority-2026q4/02-11-19对比批口径声明.md`](priority-2026q4/02-11-19对比批口径声明.md) | 对外引用本批数字必须同时给什么；四种不得引用 | 每批跑完回填 §二／§六／§八 |

> **`qwen-eval-2610` 的 Q01–Q16 不在上面这 48 项里**。不是因为它不重要，而是它进不了 P0–P4 的窗口划分：截止日 11-18 早于 11/19 批日，属于「时间窗倒逼的并行轨道」，到期即失效。它与 11/19 批是两件事——那批是 7 模型横评，这批只要 1 个被测模型 ＋ 1 个基线模型。

## 五、不对外：面试叙事支撑

| 文件 | 定位 |
|---|---|
| [`x-agi-2026/00-四框架知识映射.md`](x-agi-2026/00-四框架知识映射.md) | 自述用途是「**供面试/简历引用**」——把已落地能力对齐 ISACA／X-AGI／iRTE／AWS 四套框架，每条附 `file:line` 锚点。它**不属于 AQG 产品文档**，引用时不得与本目录其他文件混谈 |

## 体量与失衡（自查）

本目录 25 个文件（17 份 md 合计 3883 行，另 8 份 json／py／html／png），是根级文档（`README.md` ＋ `README.en.md` ＋ `CHANGELOG.md` ＋ `AGENTS.md` ＋ `SECURITY.md` ＋ `CONTRIBUTING.md`，合计 1720 行）的两倍多；其中「外部材料转化 ＋ 项目治理」约 2000 行，是体量主项。这份索引存在的意义，就是让后来者不必读完 3883 行才知道该读哪一份。

## 维护约定

1. 新增文件必须在本表登记——否则等于没人知道它存在（根 `README.md` 的目录树只指向本索引一行）。
2. 改 §一 的文件前，先读消费方代码：豁免清单与 κ 数值都会改变门禁结论，改完要跑 `verify.py`。
3. **同一事实只写一处**。多记一次进度，改期时就变成两个数字。已有代价：K08 曾在 P0（决策）与 P2（实现）各计一次待办导致总数多算 1；`models.json` 的 `limit: 150` 与排期「207 条」长期冲突。
