"""ISACA C4 出口策略（默认拒绝 + 白名单）的单元覆盖。"""
from src.engine.egress import (
    DEST_EXTERNAL,
    DEST_INTERNAL,
    DEST_LOOPBACK,
    DEST_METADATA,
    check_egress,
    classify_host,
    host_from_url,
)


def test_classify_host_buckets_destinations():
    assert classify_host("169.254.169.254") == DEST_METADATA
    assert classify_host("metadata.google.internal") == DEST_METADATA
    assert classify_host("192.168.1.1") == DEST_INTERNAL
    assert classify_host("127.0.0.1") == DEST_LOOPBACK
    assert classify_host("db.svc.cluster.local") == DEST_INTERNAL
    assert classify_host("example.com") == DEST_EXTERNAL
    # 识别不出的裸主机名保守归 internal，交给默认拒绝，绝不放松。
    assert classify_host("") == DEST_EXTERNAL
    assert classify_host("intranet-host") == DEST_INTERNAL


def test_host_from_url_handles_missing_scheme():
    assert host_from_url("https://API.OpenAI.com/v1?x=1") == "api.openai.com"
    assert host_from_url("169.254.169.254/latest") == "169.254.169.254"
    assert host_from_url("") == ""


def test_default_denies_everything_without_allowlist():
    allowed, reason = check_egress("https://api.openai.com/v1", ())
    assert allowed is False
    assert "not_allowlisted" in reason


def test_metadata_internal_loopback_denied_even_empty_allowlist():
    for url, kind in [
        ("http://169.254.169.254/latest/meta-data", "metadata"),
        ("http://10.0.0.5/x", "internal"),
        ("http://127.0.0.1", "loopback"),
        ("http://localhost/admin", "internal"),
    ]:
        allowed, reason = check_egress(url, ())
        assert allowed is False, url
        assert reason.startswith(f"egress_denied:{kind}:"), (url, reason)


def test_allowlisted_public_host_is_allowed():
    allowed, reason = check_egress("https://api.openai.com/v1", ("api.openai.com",))
    assert allowed is True
    assert reason == "egress_allowed:external:api.openai.com"


def test_suffix_allowlist_matches_subdomains():
    allowed, _ = check_egress("https://api.openai.com/v1", (".openai.com",))
    assert allowed is True
    # 域后缀不能顺手放行同前缀的别的域。
    allowed2, _ = check_egress("https://openai.com.evil.io/", (".openai.com",))
    assert allowed2 is False


def test_non_allowlisted_external_denied():
    allowed, reason = check_egress("https://evil.example.com", ("api.openai.com",))
    assert allowed is False
    assert "not_allowlisted" in reason


def test_metadata_denied_even_if_allowlisted():
    """白名单不能把元数据/内网洗白——这是防用例作者手滑放行的底线。"""
    allowed, reason = check_egress("http://169.254.169.254/", ("169.254.169.254",))
    assert allowed is False
    assert "metadata" in reason


def test_unparseable_target_denied():
    allowed, reason = check_egress("", ())
    assert allowed is False
    assert "无法解析" in reason
