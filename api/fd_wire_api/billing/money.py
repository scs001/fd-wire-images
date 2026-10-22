"""金额单位换算：账内一律整数微元，展示/外部契约才转元。"""

from __future__ import annotations

from decimal import Decimal

MICRO_PER_YUAN = 1_000_000


def yuan_to_micro(value: str | int | Decimal) -> int:
    """元 → 微元（精确，输入走 Decimal，不吃浮点亏）。"""
    micro = int((Decimal(str(value)) * MICRO_PER_YUAN).to_integral_value())
    return micro


def micro_to_yuan(micro: int) -> Decimal:
    """微元 → 元（Decimal，两位小数量化）。"""
    return (Decimal(micro) / Decimal(MICRO_PER_YUAN)).quantize(Decimal("0.01"))


def micro_to_yuan_str(micro: int) -> str:
    """微元 → 元的字符串（¥xx.xx），用于展示与对账报告。"""
    return f"¥{micro_to_yuan(micro):,}"
