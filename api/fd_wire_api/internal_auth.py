"""内部端点鉴权（``X-Internal-Token``）——叶子模块，供跨子系统共用。

**为什么单独一个文件。** ``cell_provision/routes.py`` 原先
``from ..demands.routes import _require_internal``，而这会踩导入环：

    demands.routes → console.auth → console.db → admin.routes
      → cell_provision.routes → demands.routes（尚未定义到 _require_internal）

后果不是"某个入口慢"而是**整个模块无法导入**：干净检出的 gitee/main 上
``pytest tests/test_wanxing_client.py`` 单独收集就 ImportError（2026-11-13
实测）。叶子模块只依赖 stdlib 与 fastapi，任何入口顺序都能解析，环消失。

还有几处同款闸（datasets / bench_pull / billing / catalog / materialization）
各自持有一份拷贝，它们不在这个环上，也可能比对别的 env（xingyan 就用
``XINGYAN_INTERNAL_TOKEN``），故本次不动——口径一致仍靠各自的测试，不靠注释。
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status

#: 环境变量名。**这里刻意没有缺省值。**
#:
#: 曾经有一个写死的缺省口令常量。生产一直由 deploy/k8s 的
#: Secret 注入真值，所以缺省值在线上从没生效过——但这正是它危险的地方：
#: 任何一次漏配 Secret 的部署都会**静默地**用一个写在源码里的口令跑起来，
#: 而这份源码是公开的，等于把口令公开。漏配必须炸，不能降级。
_ENV = "FD_WIRE_INTERNAL_TOKEN"


def require_internal(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """与 ``FD_WIRE_INTERNAL_TOKEN`` 常量时间比对；缺失/空白一律 401。

    未配置该环境变量时返回 503（配置错误），不是 401——两者的处置不同：
    401 说明调用方没带对令牌，503 说明这个部署本身没配好。
    """
    expected = os.getenv(_ENV)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"内部端点未配置：{_ENV} 必须由部署面 Secret 注入",
        )
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部端点需要有效的 X-Internal-Token",
        )
