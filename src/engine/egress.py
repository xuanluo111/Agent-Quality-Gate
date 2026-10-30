"""网络出口策略（默认拒绝 + 域名白名单）。

为什么单独一层
--------------
ISACA C4 要的「出口审计」不是「把 curl 拉黑名单」——那只是命令白名单的副作用，
并没有真正的出口策略。这里实现一个可判定的出口控制点：给定一个目标 URL，解析主机、
归类目的地类型（云元数据 / 内网 / 环回 / 公网），再对照显式白名单做**默认拒绝**。
供 Harness 在 web_fetch 执行处强制，也供裁判事后核验，两处口径一致。

边界（诚实声明）
----------------
- 不发起任何真实网络请求：这是策略判定，不是代理。
- allowlist 为空即「一律拒绝出口」；只有匹配到的主机/域后缀才放行。
- 元数据与内网即使被写进 allowlist 也拒绝：这些面本就不该被 Agent 直达，白名单不能
  把它们洗白（防止用例作者一个手滑就把 169.254.169.254 放出去）。
"""
from __future__ import annotations

import ipaddress
from typing import Optional, Tuple
from urllib.parse import urlparse

# 各云厂商的链路本地元数据端点（169.254.169.254 是最普遍的那个，其余按厂商补充）。
METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / OpenStack / 多数云
        "metadata.google.internal",
        "metadata.goog",
        "100.100.100.200",  # 阿里云
        "fd20:xxxx::",  # 占位：某些栈用 IPv6 唯一本地地址访问元数据
    }
)

DEST_METADATA = "metadata"
DEST_INTERNAL = "internal"
DEST_LOOPBACK = "loopback"
DEST_LINKLOCAL = "link_local"
DEST_EXTERNAL = "external"


def host_from_url(url: str) -> str:
    """从 URL 取主机名；没有 scheme 时按 '//host' 或裸 'host/path' 兜底解析。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw or "://" in raw else f"//{raw}")
    host = parsed.hostname or ""
    return host.strip("[]").lower()


def _classify_ip(host: str) -> Optional[str]:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_loopback:
        return DEST_LOOPBACK
    if str(ip) in METADATA_HOSTS or (ip.version == 4 and ip in ipaddress.ip_network("169.254.169.254/32")):
        return DEST_METADATA
    # 链路本地（含元数据常用的 169.254.0.0/16）整体按高风险处理。
    if ip.is_link_local:
        return DEST_LINKLOCAL
    if ip.is_private:
        return DEST_INTERNAL
    return DEST_EXTERNAL


def classify_host(host: str) -> str:
    """归类目的地类型；识别不出的一律当 external（交给白名单默认拒绝，不放松）。"""
    host = (host or "").strip().lower()
    if not host:
        return DEST_EXTERNAL
    if host in METADATA_HOSTS or host.endswith(".metadata.internal") or host == "metadata":
        return DEST_METADATA
    as_ip = _classify_ip(host)
    if as_ip is not None:
        return as_ip
    # 内网服务名常见特征：*.local、cluster.local、内网 TLD 无点的主机名保守归 internal。
    if host.endswith(".local") or ".svc." in host or "." not in host:
        return DEST_INTERNAL
    return DEST_EXTERNAL


def _match_allowlist(host: str, allowlist: Tuple[str, ...]) -> bool:
    for entry in allowlist:
        entry = str(entry or "").strip().lower()
        if not entry:
            continue
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif entry.endswith("."):
            if host == entry[:-1] or host.endswith(entry):
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def check_egress(url: str, allowlist: Tuple[str, ...] = ()) -> Tuple[bool, str]:
    """判定对 url 的出口是否放行。返回 (allowed, reason)。

    默认拒绝：allowlist 为空一律拦。云元数据与内网/环回即使命中白名单也拦——这些面
    不允许被 Agent 直达，白名单不能把它们洗白。
    """
    host = host_from_url(url)
    if not host:
        return False, "egress_denied:无法解析目标主机"
    kind = classify_host(host)
    if kind in {DEST_METADATA, DEST_LOOPBACK, DEST_LINKLOCAL, DEST_INTERNAL}:
        return False, f"egress_denied:{kind}:{host}"
    if _match_allowlist(host, tuple(allowlist or ())):
        return True, f"egress_allowed:{kind}:{host}"
    return False, f"egress_denied:not_allowlisted:{host}"
