"""连接器分发的公开下载面（wire-connector-distribution 任务 1）。

静态目录服务：manifest 读取 + 文件在场校验 + 二进制流式返回；
无鉴权（公开取件）、无状态（目录即事实源，manifest 由 CI/手动晋升生成）。
"""

from .routes import router

__all__ = ["router"]
