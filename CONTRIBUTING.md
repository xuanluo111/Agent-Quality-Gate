# 参与开发

## 环境

Python 3.11+（CI 双矩阵 3.11 / 3.13）。依赖以 `requirements.lock` 为准，`requirements*.txt` 是声明层。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
make verify        # 提交前基线：离线层全部项（单测/条数声明/能力/数据集/题面地基/台账/dry-run），全程 0 成本
```

## 分支纪律

- 日常开发在工作分支（如 `feat/*`）进行，改动**先推 `hzg`**；
- `main` 仅在维护者明确指令后合入，不直接推送；
- 远端只认 GitHub `origin`，不向其他镜像仓库推送。

## 提交前检查（三项全过才允许提交）

1. `python -m pytest -q` 全绿；含业务逻辑的改动必须配套单元测试（表驱动风格，见 `tests/`）；**动了用例数就要同步 `README.md`／`README.en.md`「测试与 CI」两处的条数声明**——`make verify` 的「声明条数自检」会拿它与 `--collect-only` 实测数比对，忘了改或只改中英其中一份都会直接判红；
2. 敏感扫描：diff 中不得出现 API Key、内网 `base_url`、真实用户数据；`models.json`、`.env`、`batch-*.log` 已被 `.gitignore` 拦截，但不要依赖它兜底，检查暂存区内容。**本机身份另有机器门禁**：`make verify` 的「本机身份不入库」会扫全部已跟踪文件里的操作者用户名与家目录绝对路径，命中即红——测试样例请用占位符（如 `/Users/someuser/…`）而不是自己机器的真路径；文档里要写验收命令时用 `$(whoami)`，别把真名贴进去（11-20 就发生过：为了在 CHANGELOG 里写明「已清干净」，把用户名原样抄进了验收命令，自己造出一处新命中，是同一趟 grep 复核抓回来的——这条自检就是为了不必再靠人记得复核）。注意它只覆盖入库件：本机被忽略的归档（`workspace/`、`.reports-archive-*/`、`.private_backup_*/`）仍需人工判断，外发前另查。
3. 文档内 mermaid 图须通过校验（版本 ≤ 8.8.3 语法，`classDef` 配色，禁 ASCII 图），禁止未经解析器实跑就宣称"图没问题"。

## 双语文档

`README.md`（中文，权威版）与 `README.en.md`（英文对译）同步维护：改动 README.md 的**事实性内容**（数字、能力、判据、限制）时同提交更新英文版；确实来不及更新时，在英文版顶部声明下补一句滞后说明，不允许静默漂移。

## 评测用例与切代

- 数据集指纹（`dataset_epoch`）变化即"切代"，风险台账会跨代重新基线——**不要用数据集变更冒充模型退化**；
- 修改题面/断言是重大决定：走人工评审，禁止为了让门禁或判据自检"变绿"而放宽裁判与断言；
- 用 `python scripts/build_suite.py --dedupe-inputs` 清理回归集重复行；构建后 `id` 仍有重复会直接非零退出；
- 新能力用例必须进 smoke（`--must-include feature:<name>`），`select_smoke` 按 tag 轮转且每家族最多占 3 槽位。

## 判据自检（C01）

改裁判闸门、断言语义或用例结构后，跑 `make oracle-selfcheck`：

- `DEAD_CASE`：完美执行也拿不到分——题面断言互斥或超纲，须修题（人工评审后）；
- `LEAKY`：破坏确定性断言仍过闸门——断言没有闸门牙齿，须补机制而不是放宽自检；
- 自检只报告、不修改用例；`NOT_DETERMINISTIC` 是诚实分母，不是失败。

## 提交信息

沿用仓库现有风格：`type(scope): 一句话说清改了什么`，正文写**为什么**与**验证方式**（跑了什么、结果数字）。实测数字必须可复现，禁止"看起来正常"式描述。
