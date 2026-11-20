# 夜跑队列（每次开工按 06 重算；本文件是「已核过落点」的快查表，不是进度源）

进度只认 `docs/priority-2026q4/06-统一待做与排期-2619.md`。本表列的是**已实测过靶子**、满足 SKILL.md §3 准入判据的自动队列，以及刻意不自动做的 🔒 项。落点行号是 2026-11-20 实测值，执行时**必须重新 grep 取证**，不得直接引用。

## A. 可自动执行（按顺序，风险递增）

| # | T 号 | 任务 | 实测落点 | 验收（必须贴实际输出） |
|---|---|---|---|---|
| A1 | — | ✅ **11-20 已完成**（见 08）。06 就地更正：①T19 靶子行号已从 `177/191` 漂到 **`tests/test_build_label_template.py:200/214`**（`--only-valid` 提交后漂移），`test_pack_originality.py:53` 未变；②T02 补第三个卡点：`66b004d` 被 01/02/06 当复现锚引用，但 `git branch -r --contains 66b004d` = **空**（尚未 push，克隆者拿不到）；③T17 见 A2 的实况修正 | `docs/priority-2026q4/06…md` | `git grep -n "/Users/$(whoami)" -- '*.py'` 的行号与 06 所写一致 |
| A2 | T17 | ✅ **11-20 已完成**。480 秒单位与对外边界句。**实况**：`src/config.py:81` `case_time_budget: int = 480` 的**上一行注释已含「总耗时上限（秒）」**→ 原验收「加 `# 单位：秒` 注释」已达成，本项只剩 README 补一句「短程门禁（单例 480 秒），不覆盖长程任务」 | `src/config.py:79-81`、`README.md` ＋ `README.en.md` | `grep -n "480" src/config.py README.md` 命中且中英对齐 |
| A3 | T15 | ✅ **11-20 已完成**，并连带把「13 项」这类写死的自检项数从 7 处文档去掉。`verify.py` 加「声明数 == collect 数」自检，防条数漂移再犯 | `scripts/verify.py`（`step_test_count_claim`，已注册）；测试已落 `tests/test_verify.py`（7→15 例） | 把 README 那条 blockquote 的数字临时改成假值 → verify 必须红；改回 → 绿。**README 只允许「测试与 CI」一处有数字**，解析不到或多处即判红 |
| A4 | T21 | ✅ **11-20 夜跑已完成**（见 08）。脱敏边界三段契约 + `.env` 整段检测。**实况**：落点只有 `tests/test_privacy.py`（+3 例，6→9），`test_egress.py` 是网络出口策略不是脱敏归属、未动；`runner.py` 已有出口脱敏，无需改源码。**副产品**：测出 S2 出口侧不脱非 `sk-` 前缀凭证赋值→ 登记为 06 的 **T47**，以 `xfail(strict=True)` 钉桩 | `tests/test_privacy.py` | 反例验证已跑：去掉 `runner.py:372` 脱敏 → 1 failed；短路 `privacy.py` 的 `CREDENTIAL_RE.finditer` → 1 failed；均字节还原 |
| A5 | T14 | ~~待做~~ ✅ **11-20 夜跑完成**：Holm–Bonferroni 校正 ＋ 效应量（百分点差 + Cohen's h）＋ A/B/C 置信等级 ＋「差异未证实」措辞，四件套同批落地。**实况：真实落点是 `scripts/run_batch.py` 的 `_rank_block`，不是本行原写的 `report.py`**（`report.py` 只用 `wilson_interval` 做单 run 展示，无跨模型比较）。副产品：删掉 `run_batch._two_prop_p` 与 `dataset.two_proportion_z_test` 的同源重复实现 | `src/engine/dataset.py`、`scripts/run_batch.py` ＋ `tests/test_dataset.py`/`test_run_batch.py` | ✅ 已达成：「显著」只出现在校正后 p 旁（单测断言）；原始 p 与校正后 p **同列**＝对照表；旧 `reports/**` 一个字节未变（`baseline.sh check` ✅，只影响将来渲染）。**反例验证**：4 个源码变异全被抓。遗留→T32（实测噪声带）／T37（`drift_check.py` 判据链，本轮刻意不动） |
| A6 | T16 | ~~待做~~ ✅ **11-20 夜跑完成**：裁判 prompt 版本进结果与报告。**实况与原行的三处差异**：① 字段名落为 `judge_prompt_version`（不是 `prompt_version`，避开模型侧 `model_version`）；② **存的是内容指纹 `sha256:<12hex>` 而非手写版本号**，理由见 06/08；③ 真实落点是 `judge.py` ＋ `schema.py` ＋ **`report.py`**（失败明细逐条渲染），`scripts/rejudge.py` **零改动**即达成（重判整体替换 `EvalResult`），只补了端到端单测防回归 | `src/engine/judge.py`（`judge_prompt_fingerprint()`/`_base`）、`src/models/schema.py`（`judge_prompt_version` ＋ `PROMPT_BEARING_LAYERS`）、`src/engine/report.py` ＋ 四份测试 | ✅ 已达成：结果与报告带 `judge_prompt_version`；`test_rejudge.py` 断言重判后字段不丢且不沿用旧值；**历史缺键回落 `""` 而非当前指纹**（`test_schema.py` 钉住，且≠当前指纹）；`rule`/`harness` 层不披裁判外衣。**反例验证**：4 个源码变异全被抓。**遗留**：双语事实漂移无机器自检→已登记 **T48**（本轮顺带补齐 EN 漏镜像的「排名统计」） |
| A7 | T19（半截） | ~~待做~~ ✅ **11-20 收工后补做**：已入库测试样例里的真实用户名改占位符 `someuser`，**断言同批改**（只换样例不换断言＝假修）。**实况**：本行原写「B 段 T19 的用户名那半可自动做」——落点只有两份测试，`src/**`／`scripts/**` 零改动；删除族（`deliverables/`／`stress_backup`／`recalib-2611-*`）与 charts／白名单仍在 B 段 | `tests/test_build_label_template.py`、`tests/test_pack_originality.py` | ✅ 已达成：`git grep -c "$(whoami)" -- '*.py' '*.md' '*.json' '*.yml'` = 0（用 `$(whoami)` 防自指）。**反例验证**：`_mask_local_paths` 原样返回 → 3 failed；本地绝对路径正则改永不匹配 → 1 failed；均字节还原。**边界如实登记**：本机 gitignore 掉的归档件（`workspace/`／`batch-full.log`／`.coverage`／`.private_backup_*/`／`.reports-archive-*/`）仍有残留，push 不带出去，外发归档会 |
| A8 | T19（机械化） | ~~待做~~ ✅ **11-20 收工后补做**：把 A7 的验收式从「一次性 grep」升级成机器门禁——`verify.py` 新增第 15 项「本机身份不入库」（`step_local_identity_hygiene` 已注册进 `main()`）。动机不是洁癖：A7 当晚我就为了在 CHANGELOG 写「已清干净」而把真名抄进验收命令，自己造出一处新命中，靠人工复核才抓回来 | `scripts/verify.py`（`identity_tokens`／`current_identity_env`／`parse_grep_hits`／`step_local_identity_hygiene`）、`tests/test_verify.py`（15→25 例）、`CONTRIBUTING.md` 第 2 条、README 双语自检清单与条数 715→725 | ✅ 已达成：`verify` **15 项全 [OK]**（`扫描 2 个身份字面量 × 全部已跟踪文件 \| 0 命中`）；全量 **724 passed, 1 xfailed**／collect **725**；`baseline.sh check` ✅。**反例验证 6 个源码变异全被抓**（5/1/1/1/1/1 red，`finally` 字节还原）＋ 1 次端到端植入真名（点名单个文件 → 还原转绿且 `git diff` 空）。**跳过与失败都不得伪装成干净**：通用账户明写「跳过」，`git grep` 退出码非 0/1 判红 |

## B. 🔒 刻意不自动做（登记进 08「待拍板」，不动手）

| T 号 | 卡在哪 | 需要用户给什么 |
|---|---|---|
| T02① | `reports/batch-*/` 被 `.gitignore:21` 整目录排除，`git ls-files reports/batch-*` = 0 | 定白名单粒度（建议 `summary.json`/`summary.md`/`*.report.md`，`.log` 不放） |
| T02② | `reports/charts/dashboard.html` 实测仍是 7 个 `20261106-night-*`，`20261118-131428` 0 命中 | 授权改写**已入库的对外图表**（`scripts/visualize_reports.py`，本地零成本） |
| T19 | 含删除与移动：`stress_backup`（3 个入库文件）、`recalib-2611-*` 死产物（4 文件 1424 行）、`deliverables/` 私料移出 | 逐项确认删除/保留；用户名改假路径那部分（3 处）可自动做 |
| T20 | 验收「GitHub 仓库页显示 MIT」需远端可见 | push 授权（当前 ahead 9，远端 HEAD 停在 `6558080`） |
| T18 | spark-4b 定向重跑 ≈2M token | 是否今晚排 + token 池确认 |
| T38 | κ 扩标需 **5×20=100 条人工裁决**（mimo 已标 κ=0.90，其余 5 份 `human_passed` 全 null） | 只能用户本人标，机器代标即污染真值 |
| T22 | CI PR 评论需远端与 `gh` 权限 | push + 授权造失败 PR |
| W4/W5 全部 | 展示口径与切代窗口（T23–T43） | 逐项决策；切代必须打包同窗口 |

## C. 每次夜跑的回归基线（三件套，缺一不可）

```bash
.venv/bin/python -m pytest -q                 # 期望：0 failed，条数 ≥ 起始值
.venv/bin/python scripts/verify.py            # 期望：离线项全 [OK]（项数以输出为准，不再写死）
bash .qoder/skills/aqg-nightshift/scripts/baseline.sh check   # 期望：✅ 零污染
```

起始参考值（**2026-11-20 夜跑 A8 后**）：pytest **724 passed, 1 xfailed**（collect **725**，README 两处声明已对齐）；verify **15 项全 OK**（含「声明条数自检」与 A8 新增的「本机身份不入库」）；`core_suite` 207 条、代际 `deff7501007cff61`。**新增/删除用例的同一批必须同步中英 README 两处条数声明**，否则第 2 条直接红；**文档里写验收命令一律用 `$(whoami)`／占位路径**，写真名会被第 15 项直接判红（A7 那晚我就这么自伤过一次）。**注意口径**：首行期望是「0 failed」，`xfail` 不计入失败，但 `strict=True` 的例一旦 XPASS 会报 failed——这是故意的。
