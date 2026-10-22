"""签名分享链接业务逻辑（wire-dashboard-share）：无表 HMAC 令牌。

令牌格式（URL ``t`` 参数，三段点号分隔，后两段明文随行供服务端重算）::

    t = "{version}.{exp}.{mac}"
    mac = HMAC-SHA256(FD_WIRE_SHARE_SECRET, "{artifact_id}|{version}|{exp}")

- **钉死版本**：version 在签名消息内——链接固定生成时刻的版本，改版后
  需重新生成（公开端点按 version 取 ``artifact_versions`` 历史行 spec，
  不看当前指针）；
- **到期**：exp 为 Unix 秒；默认 30 天（``DEFAULT_TTL_SECONDS``，可经
  ``FD_WIRE_SHARE_TTL_SECONDS`` 覆盖）；
- **比较**：mac 校验用 ``hmac.compare_digest``（常数时间，防时序侧信道）；
- **secret 惰性读**：逐请求 ``os.getenv``（照 downloads 先例——测试可逐
  用例注入，配置变更无需重启即可生效/禁用）。

任一校验不满足（格式/版本不一致/篡改/过期/产物不再是 published）公开端点
一律 404 且 detail 统一——不向未认证方泄露失败原因差异。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..artifacts.models import Artifact, ArtifactStatus, ArtifactVersion

#: 分享链接默认有效期：30 天（env FD_WIRE_SHARE_TTL_SECONDS 可覆盖）。
DEFAULT_TTL_SECONDS = 30 * 24 * 3600


class ShareDisabled(Exception):
    """FD_WIRE_SHARE_SECRET 未配置——分享功能整体禁用（路由层转 503 带原因）。"""


class ShareLinkInvalid(Exception):
    """令牌任一校验不满足——路由层转统一 404（不泄露原因差异）。"""


def share_secret() -> str | None:
    """FD_WIRE_SHARE_SECRET（请求时读 env，测试可逐用例注入）；未配置 → None。"""
    raw = os.getenv("FD_WIRE_SHARE_SECRET", "").strip()
    return raw or None


def require_share_secret() -> str:
    """取分享签名密钥；未配置抛 ShareDisabled（两端点共用——503 诚实禁用）。"""
    secret = share_secret()
    if secret is None:
        raise ShareDisabled(
            "分享功能未配置（FD_WIRE_SHARE_SECRET 为空）：本部署未启用"
            "分享链接，请按部署清单配置该环境项后重试。"
        )
    return secret


def ttl_seconds() -> int:
    """链接有效期秒数：env 覆盖（非法值回退默认，防御位）。"""
    raw = os.getenv("FD_WIRE_SHARE_TTL_SECONDS", "").strip()
    try:
        return int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:  # pragma: no cover - 配置错误兜底，不整端点炸
        return DEFAULT_TTL_SECONDS


def _mac(secret: str, *, artifact_id: int, version: int, exp: int) -> str:
    """HMAC-SHA256(secret, "{artifact_id}|{version}|{exp}") 的 hex 摘要。"""
    message = f"{artifact_id}|{version}|{exp}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_token(
    secret: str, *, artifact_id: int, version: int, ttl: int, now: int | None = None
) -> tuple[str, int]:
    """生成分享令牌：返回 (token, exp)。

    token = "{version}.{exp}.{mac}"——version/exp 明文随行（服务端重算 mac
    用），mac 覆盖三要素防篡改。

    ``ttl == 0`` 是长效档（wire-record-view design D4）：exp 置 0（非
    ``now+0``——0 是「无到期」哨兵，verify_token 对 exp==0 跳过过期校验）；
    其余 ttl 照 ``now+ttl``。
    """
    if ttl == 0:
        exp = 0
    else:
        exp = int(time.time()) + ttl if now is None else now + ttl
    return f"{version}.{exp}.{_mac(secret, artifact_id=artifact_id, version=version, exp=exp)}", exp


def verify_token(
    secret: str, *, artifact_id: int, token: str, v_param: int | None
) -> tuple[int, int]:
    """验签 + v 参数与令牌内版本一致 + 未过期；返回 (version, exp)。

    任一不满足抛 ShareLinkInvalid（路由层统一 404）：
    - 结构：恰好三段点号分隔，前两段可解析为整数；
    - 版本：查询参数 v 必须在且与令牌明文 version 一致（URL 里的 v 与
      令牌绑定的版本不许分家）；
    - 签名：重算 mac 与令牌段常数时间比较（artifact_id 来自路径——
      换产物重放即失配）；
    - 到期：exp 已过即失效；**exp==0 是长效档哨兵**（wire-record-view
      design D4——生成时选 0 档），跳过过期校验（mac 已覆盖 exp=0，
      无令牌能伪造该哨兵；其余校验零变化）。
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ShareLinkInvalid("令牌结构不合法")
    try:
        version, exp = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ShareLinkInvalid("令牌 version/exp 非整数") from exc
    if v_param is None or v_param != version:
        raise ShareLinkInvalid("v 参数与令牌内版本不一致")
    if not hmac.compare_digest(_mac(secret, artifact_id=artifact_id, version=version, exp=exp), parts[2]):
        raise ShareLinkInvalid("签名不匹配")
    if exp != 0 and int(time.time()) > exp:
        raise ShareLinkInvalid("令牌已过期")
    return version, exp


def get_published_artifact(db: Session, artifact_id: int) -> Artifact | None:
    """公开端点取产物：仅存在且当前仍 published 才放行（归档即失效的兜底）。"""
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.status != ArtifactStatus.published.value:
        return None
    return artifact


def get_pinned_version(db: Session, artifact_id: int, version: int) -> ArtifactVersion | None:
    """取钉死版本的 spec 快照行（artifact_versions 历史行，非当前指针——
    编辑出新版本后旧链接仍看旧版本的关键）。"""
    return db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.version == version,
        )
    )


def expires_at_iso(exp: int) -> str:
    """exp Unix 秒 → UTC ISO-8601（响应里的到期时间口径）。"""
    return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
