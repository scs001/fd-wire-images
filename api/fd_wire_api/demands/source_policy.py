"""禁爬源清单与匹配（wire-crawl-source-blocklist，单一出处）。

用户定规（2026-10-12）：**不允许爬淘宝和拼多多的数据**。代爬服务面按
平台级禁区硬拒——提交口（schemas._public_source_url）与出单闸
（orchestration.export_workorder_service）共用本模块的清单与匹配函数，
两处口径永远一致。

匹配纪律：host 等于清单域名、或以 ``.<域名>`` 结尾（大小写不敏感）——
子域（item.taobao.com）命中；``taobao.com.evil.example`` 不命中（它
的 host 并不以 .taobao.com 结尾，只是前缀伪装）；含端口先剥端口。

维护：新增平台=改 BLOCKED_SOURCE_DOMAINS + 补测试，无需迁移。
清单显式列名，不做通配扩大化（京东/亚马逊等未定规不在清单内）。
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: 平台禁爬域名清单（首版：淘宝系 + 拼多多系；改此表即全链生效）。
BLOCKED_SOURCE_DOMAINS: tuple[str, ...] = (
    # 淘宝系
    "taobao.com",
    "tmall.com",
    "alibaba.com",
    "1688.com",
    "alipay.com",
    # 拼多多系
    "pinduoduo.com",
    "yangkeduo.com",
    "pddpic.com",
    "duoduoyoucai.com",
)

#: 拒单理由（对外文案；两处校验共用，错误定位由调用方补条目索引）。
BLOCKED_SOURCE_REASON = "该平台数据不在代爬服务范围（平台协议限制）"


def blocked_source_domain(url: str) -> str | None:
    """URL 命中禁爬清单时返回命中的清单域名，否则 None。

    host 取 URL 的 netloc 去端口、小写；等于清单域或以 ``.<清单域>``
    结尾即命中。URL 不可解析（无 host）返回 None——形态问题由各入口
    既有校验负责，本函数只判禁爬。
    """
    try:
        netloc = urlsplit(url.strip()).netloc.lower()
    except ValueError:
        return None
    host = netloc.rsplit("@", 1)[-1]  # 防御：账密形态已在入口拒，此处仍剥
    host = host.rsplit(":", 1)[0] if ":" in host else host
    if not host:
        return None
    for domain in BLOCKED_SOURCE_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return None
