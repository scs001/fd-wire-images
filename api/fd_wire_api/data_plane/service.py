"""数据面 key 生命周期与限流（wire-customer-data-engine 5.2/5.4）。

- 铸造：``wdk-`` + 32B urlsafe 随机；sha256 唯一落库，明文仅铸造响应一次
  （连接器 key 同款纪律）。
- 验证：sha256 命中 + status=active 即放行；吊销即时生效（无缓存层）。
- 限流：进程内滑动窗 per key（RPM 可配，缺省 60）。单副本口径——api 为
  单副本部署；多副本需换共享存储（挂账后置，非 v1 形态）。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..datasets.models import DataPlaneKey

KEY_PREFIX = "wdk-"
_DEFAULT_RPM = 60


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def default_rpm() -> int:
    try:
        return max(1, int(os.environ.get("FD_WIRE_DATA_PLANE_RPM", _DEFAULT_RPM)))
    except ValueError:
        return _DEFAULT_RPM


class KeyRevokedError(RuntimeError):
    """key 存在但已吊销（401 语义，与「未知 key」区分如实提示）。"""


def mint(
    db: Session,
    *,
    customer_id: str,
    label: str,
    dataset_ids: list[str] | None = None,
) -> tuple[DataPlaneKey, str]:
    """铸一把 key：返回 (行, 明文)。明文只在此处存在，绝不落库。"""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    row = DataPlaneKey(
        customer_id=customer_id,
        label=label,
        key_hash=digest,
        fingerprint=digest[:12],
        allowlist_json=list(dataset_ids or []),
        status="active",
        created_at=_utcnow_naive(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, plaintext


def revoke(db: Session, *, customer_id: str, key_id: str) -> DataPlaneKey:
    row = db.execute(
        select(DataPlaneKey).where(
            DataPlaneKey.id == key_id, DataPlaneKey.customer_id == customer_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"数据面 key 不存在: {key_id}")
    if row.status != "revoked":
        row.status = "revoked"
        row.revoked_at = _utcnow_naive()
        db.commit()
        db.refresh(row)
    return row


def list_keys(db: Session, *, customer_id: str) -> list[DataPlaneKey]:
    return list(
        db.execute(
            select(DataPlaneKey)
            .where(DataPlaneKey.customer_id == customer_id)
            .order_by(DataPlaneKey.created_at.desc(), DataPlaneKey.id)
        )
        .scalars()
        .all()
    )


def verify(db: Session, plaintext: str) -> DataPlaneKey:
    """明文 → 活跃 key 行；未知/已吊销如实区分抛错。"""
    if not plaintext.startswith(KEY_PREFIX):
        raise HTTPException(status_code=401, detail="数据面凭据形态不符（应为 Bearer wdk-…）")
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    row = db.execute(
        select(DataPlaneKey).where(DataPlaneKey.key_hash == digest)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="数据面 key 不存在或已更换")
    if row.status == "revoked":
        raise KeyRevokedError("数据面 key 已吊销")
    return row


# ── per-key 滑动窗限流（5.4）──────────────────────────────────────────


class _SlidingWindow:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key_id: str, rpm: int) -> bool:
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(key_id, deque())
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= rpm:
                return False
            window.append(now)
            return True


_limiter = _SlidingWindow()


def check_rate_limit(key_id: str, rpm: int | None = None) -> None:
    """超出 per-key RPM → 429（附重试提示；不影响其他 key）。"""
    if not _limiter.allow(key_id, rpm or default_rpm()):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请求频率超出该 key 的限流（{rpm or default_rpm()} 次/分钟），请稍后重试",
            headers={"Retry-After": "60"},
        )


def reset_runtime() -> None:
    """测试隔离：清限流窗（进程内状态不随库重置）。"""
    with _limiter._lock:  # noqa: SLF001 - 测试钩子
        _limiter._hits.clear()  # noqa: SLF001
