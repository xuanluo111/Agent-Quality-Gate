"""数据集代际指纹与劣化护栏的纯函数单测。

这三条函数是整个跨代保护的大脑，必须先把边界钉死：
- 未知代（空串）一律退化成接入前的旧行为，绝不静默抑制真实劣化；
- 只有两代都已知且不同才算断点 / 才允许把再次失败降级成 open。
"""
from src.engine.lineage import dataset_epoch, epoch_allows_regression, is_breakpoint


def test_dataset_epoch_uses_short_prefix():
    full = "299ef92e66b9bd65abcdef0123456789"
    assert dataset_epoch(full) == full[:16]


def test_dataset_epoch_normalizes_empty_to_unknown():
    # 空 / None 都归一为 ""：表示"未知代"，而不是某个具体代。
    assert dataset_epoch("") == ""
    assert dataset_epoch(None) == ""


def test_breakpoint_requires_both_known_and_different():
    assert is_breakpoint("aabbccddeeff0011", "1122334455667788") is True
    # 同代不是断点。
    assert is_breakpoint("aabbccddeeff0011", "aabbccddeeff0011") is False
    # 首代（prev 未知）不是断点。
    assert is_breakpoint("", "aabbccddeeff0011") is False
    # 任一未知都无法判定，保守地不算断点。
    assert is_breakpoint("aabbccddeeff0011", "") is False
    assert is_breakpoint("", "") is False


def test_regression_allowed_within_same_or_unknown_epoch():
    # 同代：允许判劣化。
    assert epoch_allows_regression("e1", "e1") is True
    # 明确跨代：不允许，交由调用方重新基线。
    assert epoch_allows_regression("e1", "e2") is False
    # 任一代未知：退化成旧行为，允许劣化（绝不因缺指纹把真实劣化藏起来）。
    assert epoch_allows_regression("", "e2") is True
    assert epoch_allows_regression("e1", "") is True
    assert epoch_allows_regression("", "") is True
