"""示例路由：借用契约端点一览。

真实业务路由（计量/账本/需求单）随切片落地；这条路由示范从
config 读取 env 可重指端点的形态，兼作部署冒烟。
"""

from fastapi import APIRouter

from ..config import settings

router = APIRouter(prefix="/api/v1", tags=["example"])


@router.get("/contracts")
def list_borrowed_contracts() -> dict:
    """列出当前生效的借用契约端点（env 可重指）。"""
    return {"contracts": settings.borrowed_contracts()}
