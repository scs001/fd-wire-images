"""客户凭据解析：物化读取的**唯一合法凭据轨道**。

双凭据边界（wire-billing「AI 探索与产物物化的计量归属」）：

- **AI 探索轨道**：``AI_PLATFORM_CREDENTIAL``（平台服务凭据）——控制台
  AI 构建循环的探索性读取用它，用量归集平台、**不进客户账单**。
- **物化轨道（本模块）**：``CUSTOMER_CREDENTIAL_MAP`` 里的客户凭据——
  产物物化（定时刷新/组合计算/报告出数）的读取逐项携带它经 wire 入口
  发出；registry audit 侧看到的是客户身份，行数**自动计量进客户账单**。

两条轨道绝不互换：本模块在映射缺失时抛
:class:`CustomerCredentialMissingError`（fail-closed 拒绝执行），
**绝不**静默回落平台服务凭据——那会让本应计费到客户的物化行数
凭空消失在平台轨道里。反向同理（AI 探索绝不带客户凭据，见
ai/executor.py）。
"""

from __future__ import annotations

from .settings import get_settings, parse_credential_map


class CustomerCredentialMissingError(RuntimeError):
    """该企业没有客户凭据映射（CUSTOMER_CREDENTIAL_MAP 未覆盖）——fail-closed。"""


def resolve_customer_credential(
    customer_id: str, *, credential_map: str | dict[str, str] | None = None
) -> str:
    """customer_id → 客户凭据；缺失即抛错（绝不回落平台服务凭据）。"""
    raw = (
        credential_map
        if credential_map is not None
        else get_settings().customer_credential_map
    )
    credential = parse_credential_map(raw).get(customer_id)
    if not credential:
        raise CustomerCredentialMissingError(
            f"customer {customer_id} 的客户凭据未配置（CUSTOMER_CREDENTIAL_MAP 未覆盖），"
            "物化读取 fail-closed 拒绝执行——绝不静默改走平台服务凭据轨道"
        )
    return credential
