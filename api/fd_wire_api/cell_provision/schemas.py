"""cell_provision 内部端点的请求/响应模型（add-self-serve-cell-provision）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimOut(BaseModel):
    """领任务响应（队头任务的最小投影视——runner 只需要 id/customer_id）。"""

    id: int
    customer_id: str
    state: str


class ResultIn(BaseModel):
    """runner 回写体：done 必带 endpoint_url；failed 必带 error；steps 选填。"""

    status: str = Field(description="done | failed")
    endpoint_url: str | None = Field(
        default=None, description="done 时的 registry 入口 URL（落 endpoints 表）"
    )
    error: str | None = Field(
        default=None, description="failed 时的失败原因（留痕回退人工链）"
    )
    steps: list[dict] | None = Field(
        default=None, description="执行步骤留痕（JSON；failed 时承载 step 痕）"
    )


class ResultOut(BaseModel):
    """回写确认（回显最终状态）。"""

    id: int
    customer_id: str
    state: str
