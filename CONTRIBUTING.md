# 参与开发

## 环境

Python 3.11+（CI 双矩阵 3.11 / 3.13）。依赖以 `requirements.lock` 为准，`requirements*.txt` 是声明层。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
make verify        # 提交前基线：单测 + 六项能力 + 数据集 + dry-run，全程 0 成本
```

## 分支纪律

- 日常开发在工作分支（如 `feat/*`）进行，改动**先推 `hzg`**；
- `main` 仅在维护者明确指令后合入，不直接推送；
- 远端只认 GitHub `origin`，不向其他镜像仓库推送。

## 提交前检查（三项全过才允许提交）

1. `python -m pytest -q` 全绿；含业务逻辑的改动必须配套单元测试（表驱动风格，见 `tests/`）；
2. 敏感扫描：diff 中不得出现 API Key、内网 `base_url`、真实用户数据；`models.json`、`.env`、`batch-*.log` 已被 `.gitignore` 拦截，但不要依赖它兜底，检查暂存区内容；
3. 文档内 mermaid 图须通过校验（版本 ≤ 8.8.3 语法，`classDef` 配色，禁 ASCII 图），禁止未经解析器实跑就宣称"图没问题"。

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
