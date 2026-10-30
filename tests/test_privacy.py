"""C6 脱敏与混淆绕过回归的单元覆盖（含 R4：归一化不改原文偏移）。"""
from src.engine.privacy import mask_sensitive_data


def test_strict_phone_masked_by_primary_rule():
    masked, applied = mask_sensitive_data("联系 13800138000 咨询")
    assert masked == "联系 [PHONE_REDACTED] 咨询"
    assert applied == ["phone:1"]


def test_separated_phone_bypass_caught():
    # 分段写法绕过严格正则，被容错变体补捕，标注 obfuscated。
    masked, applied = mask_sensitive_data("138-0013-8000")
    assert masked == "[PHONE_REDACTED]"
    assert applied == ["phone_obfuscated:1"]


def test_fullwidth_phone_bypass_caught():
    masked, applied = mask_sensitive_data("１３８００１３８０００")
    assert masked == "[PHONE_REDACTED]"
    assert applied == ["phone_obfuscated:1"]


def test_segmented_id_card_bypass_caught():
    # 逐位插空格、且不含会被手机号规则抢走的 "1[3-9]" 组合。
    masked, applied = mask_sensitive_data("1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 X")
    assert masked == "[ID_REDACTED]"
    assert applied == ["id_card_obfuscated:1"]


def test_no_false_positive_on_plain_numbers():
    # 普通年份/金额不能被误当敏感号，否则脱敏会破坏正常文本。
    masked, applied = mask_sensitive_data("2024年支付100元")
    assert masked == "2024年支付100元"
    assert applied == []


def test_multiple_categories_both_recorded():
    masked, applied = mask_sensitive_data("手机 13800138000 邮箱 a@b.com")
    assert "[PHONE_REDACTED]" in masked
    assert "[EMAIL_REDACTED]" in masked
    assert "phone:1" in applied
    assert "email:1" in applied
