"""数据集代际指纹与跨代劣化护栏。

为什么需要这一层
----------------
抽样走 `stratified_sample(cases, limit, seed)`：对总体分层抽样。一旦 `dataset_hash`
变了（加用例、或改了某条用例的断言），120 条子集会整体洗牌，历史 run 与风险台账血缘
随之不可比——这是 ISACA 对齐工作里最该防的假信号。

但"不可比"不该等于"信息丢失"。这里把每一代数据集钉成一个稳定 `epoch`，让风险台账
只在**同一 epoch 内**判定 `resolved -> open` 为劣化（regressed）；跨 epoch 的再次失败
重新记为 `open`（新一轮基线），并在报告里标出断点位置。这样加用例不再是"污染历史"，
而是一次**有标记的代际切换**：跨代只比同类、不判模型劣化。

边界
----
不改数据集、不碰抽样、不花 token。只做"标识 + 判定护栏 + 断点呈现"。
`epoch` 直接取 `dataset_hash` 前 16 位（与报告展示的短指纹一致），空值表示未知代，
按旧行为处理（不阻断劣化判定），避免历史台账被误清空。
"""
from __future__ import annotations

# 与报告展示的指纹长度保持一致：报告里 `dataset_hash[:16]`，这里也用 16。
EPOCH_PREFIX_LEN = 16


def dataset_epoch(dataset_hash: str) -> str:
    """把完整 dataset_hash 收敛成稳定的代际标识（前 16 位）。

    空/None 一律归一为 `""`：表示"这一代未知"，而不是"第 0 代"。未知代不参与断点判定，
    也不会抑制劣化，以保持与未接入本能力时完全一致的行为。
    """
    return str(dataset_hash or "")[:EPOCH_PREFIX_LEN]


def is_breakpoint(prev_epoch: str, cur_epoch: str) -> bool:
    """本轮相对台账上一代是否发生了数据集代际切换。

    只有两代都已知且不同才算断点：首次运行（prev 为空）不是断点，未知代（任一为空）
    也无法判定，都不能据此催"重新基线"。
    """
    return bool(cur_epoch) and bool(prev_epoch) and prev_epoch != cur_epoch


def epoch_allows_regression(entry_epoch: str, cur_epoch: str) -> bool:
    """劣化（resolved 再次失败 -> regressed）是否可以在两代之间成立。

    任一代未知时返回 True：退化成接入本能力之前的旧行为，绝不因为缺指纹就把真实劣化
    误藏成 open。只有明确跨代（两代已知且不同）才返回 False，交由调用方重新基线。
    """
    if not entry_epoch or not cur_epoch:
        return True
    return entry_epoch == cur_epoch
