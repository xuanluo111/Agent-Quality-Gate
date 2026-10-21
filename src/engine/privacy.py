import re
from typing import List, Tuple

MASK_RULES = (
    ("email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"), "[EMAIL_REDACTED]"),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[ID_REDACTED]"),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE_REDACTED]"),
    ("api_key", re.compile(r"\b(?:sk|ak|pk)-[A-Za-z0-9_-]{16,}\b"), "[API_KEY_REDACTED]"),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[BANK_CARD_REDACTED]"),
)


def mask_sensitive_data(text: str) -> Tuple[str, List[str]]:
    masked = str(text)
    applied: List[str] = []
    for name, pattern, replacement in MASK_RULES:
        masked, count = pattern.subn(replacement, masked)
        if count:
            applied.append(f"{name}:{count}")
    return masked, applied


def expects_local_or_refusal(expected_behavior: str) -> bool:
    text = str(expected_behavior or "")
    return any(keyword in text for keyword in ("拒绝", "本地", "不上云", "不得上云", "仅本地", "禁止上云"))
