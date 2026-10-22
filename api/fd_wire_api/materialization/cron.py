"""轻量五字段 cron 解析与匹配（不引重依赖——croniter 不在依赖里，自实现子集）。

**支持范围（写清楚，超出即解析报错，不猜）**：
- 五字段：分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-7，0 与 7 均为周日)；
- 每字段支持：``*``、精确整数、逗号列表 ``1,15,30``、闭区间 ``5-10``、
  ``*`` 上的步进 ``*/15``（区间步进 ``5-10/2`` 也接受）；
- 不支持：名称（JAN/MON）、L/W/#、秒与第六字段、年、时区偏移——
  出现字母或字段数 != 5 一律 ValueError。

语义约定：
- 匹配在 **naive UTC** 墙钟上做分钟粒度评估（与仓级 naive-UTC 纪律
  一致）。客户时区调度是调用侧的换算职责：控制台把客户墙钟时刻
  换算成 UTC 后再登记 cron（wire-analysis-console「按客户时区调度」
  场景的 v1 落法，后续如需库内时区感知再扩展本模块）。
- 日/周同为受限字段时按标准 cron 语义取 **OR**（两者任一命中即命中），
  仅一方受限则该方必须命中。
- 调度循环按 ``MATERIALIZATION_INTERVAL`` 扫描（默认 60s）：同一命中
  分钟内至多触发一次（见 scheduler.should_run）；扫描间隔大于 1 分钟
  时可能跳过中间的命中分钟——间隔配置应与 cron 粒度匹配。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: 五字段的（名称, 下界, 上界）；周字段 0-7，匹配前把 7 归一为 0（周日）
_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),
)

_FIELD_NAMES = tuple(name for name, _, _ in _FIELDS)

_ERROR_TEMPLATE = (
    "cron 表达式不合支持范围（五字段：分 时 日 月 周；支持 * / 列表 区间 步进，"
    "不支持名称/L/W/#/秒/年）: {expr!r}——{reason}"
)


def _parse_field(field_text: str, name: str, low: int, high: int) -> frozenset[int]:
    """解析单个字段为命中值集合（周字段的 7 归一为 0）。"""
    values: set[int] = set()
    for part in field_text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(_ERROR_TEMPLATE.format(expr=field_text, reason=f"{name} 字段存在空片段"))
        step = 1
        if "/" in part:
            base, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(
                    _ERROR_TEMPLATE.format(expr=field_text, reason=f"{name} 步进需为正整数")
                )
            step = int(step_text)
        else:
            base = part
        if base == "*":
            start, end = low, high
        elif "-" in base and not base.startswith("-"):
            start_text, _, end_text = base.partition("-")
            if not (start_text.isdigit() and end_text.isdigit()):
                raise ValueError(
                    _ERROR_TEMPLATE.format(expr=field_text, reason=f"{name} 区间需为整数")
                )
            start, end = int(start_text), int(end_text)
        elif base.isdigit():
            start = end = int(base)
        else:
            raise ValueError(
                _ERROR_TEMPLATE.format(expr=field_text, reason=f"{name} 字段含不支持的写法 {part!r}")
            )
        if not low <= start <= high or not low <= end <= high:
            raise ValueError(
                _ERROR_TEMPLATE.format(
                    expr=field_text, reason=f"{name} 取值需在 {low}-{high} 内"
                )
            )
        if end < start:
            raise ValueError(_ERROR_TEMPLATE.format(expr=field_text, reason=f"{name} 区间终点小于起点"))
        values.update(range(start, end + 1, step))
    if name == "day_of_week" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


@dataclass(frozen=True)
class CronSchedule:
    """解析后的 cron：五个命中值集合 + 原始表达式。"""

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]

    def matches(self, moment: datetime) -> bool:
        """该 naive UTC 时刻（分钟粒度）是否命中。日/周同受限取 OR（标准语义）。"""
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        dom_restricted = self.days_of_month != _FULL_DOM
        dow_restricted = self.days_of_week != _FULL_DOW
        dom_hit = moment.day in self.days_of_month
        # Python weekday(): Monday=0..Sunday=6 → cron 周日=0 的换算
        cron_dow = (moment.weekday() + 1) % 7
        dow_hit = cron_dow in self.days_of_week
        if dom_restricted and dow_restricted:
            return dom_hit or dow_hit
        return dom_hit and dow_hit


#: 「未受限」的哨兵集合（解析 ``*`` 字段的满集，用于 OR 语义判定）
_FULL_DOM = frozenset(range(1, 32))
_FULL_DOW = frozenset(range(0, 7))


def parse_cron(expr: str) -> CronSchedule:
    """解析五字段 cron；不合支持范围即 ValueError（绝不猜测语义）。"""
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError(_ERROR_TEMPLATE.format(expr=expr, reason="表达式为空"))
    text = expr.strip()
    if any(c.isalpha() for c in text):
        raise ValueError(
            _ERROR_TEMPLATE.format(expr=expr, reason="含字母（名称写法不支持）")
        )
    parts = text.split()
    if len(parts) != 5:
        raise ValueError(
            _ERROR_TEMPLATE.format(expr=expr, reason=f"需恰为 5 个字段，收到 {len(parts)} 个")
        )
    field_values = [
        _parse_field(part, name, low, high)
        for part, (name, low, high) in zip(parts, _FIELDS)
    ]
    return CronSchedule(
        expr=text,
        minutes=field_values[0],
        hours=field_values[1],
        days_of_month=field_values[2],
        months=field_values[3],
        days_of_week=field_values[4],
    )


def validate_cron(expr: str) -> str:
    """校验并原样返回（供路由层 422 前置检查）。"""
    parse_cron(expr)
    return expr
