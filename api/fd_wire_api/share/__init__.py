"""签名分享链接服务端（wire-dashboard-share）。

无表设计——分享凭据不落库，全靠 HMAC 令牌自证：

- ``POST /api/v1/artifacts/{id}/share``（客户鉴权）：published 仪表盘 →
  生成钉死当前版本的签名链接；
- ``GET  /api/v1/share/dashboard/{id}?t=…&v=…``（公开无鉴权）：验签 +
  版本一致 + 产物仍 published → 返回该历史版本的 spec 快照。

``FD_WIRE_SHARE_SECRET`` 未配置时两端点 503 诚实禁用（逐请求读 env，
与 downloads 的 ``FD_WIRE_DOWNLOADS_DIR`` 同款惰性口径）。
"""

from .routes import router

__all__ = ["router"]
