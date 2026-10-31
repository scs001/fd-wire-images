"""查询服务：目录列举/检索、条目详情、claim 级解析。

只读：全部查询走 ``db.readonly_session()``（只读引擎 + 写动词拦截），
与摄取管道在物理上分离。

两个诚实性要点：
- **陈旧标记**随每个响应返回（``snapshot.stale``）。上游拉取失败时旧快照
  继续服务，但消费者必须能看出它不是最新的——绝不静默。
- **claim 锚小节正文**按存储的 body 现算：section 是标题文本，正文是整篇；
  取不到该小节时返回 ``section_found=false`` 而不是给一个空串冒充内容。
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from . import models

ZONE_LABELS = {
    "briefings": "内参",
    "academy": "学院",
    "research": "研究",
}
LOCALES = ("zh", "en")


class NotFound(Exception):
    """条目或 claim 不在快照里（调用方转 404）。"""


class ClaimWithdrawn(Exception):
    """claim 已撤销——必须回显式撤销态，不是 404。"""

    def __init__(self, claim: models.XingyanClaim):
        super().__init__(f"claim {claim.slug}/{claim.claim_id} 已撤销")
        self.claim = claim


def snapshot_meta(session: Session) -> dict:
    """当前快照水位（每个响应都带上：消费者据此判断内容是否最新）。"""
    state = session.get(models.XingyanSyncState, 1)
    if state is None:
        return {
            "source_sha": "",
            "schema_version": "",
            "status": "empty",
            "stale": True,
            "last_success_at": None,
            "last_attempt_at": None,
            "last_error": "尚未成功摄取（无快照）",
            "items": 0,
            "claims": 0,
        }
    stale = state.status != "ok" or state.last_success_at is None
    return {
        "source_sha": state.source_sha or "",
        "schema_version": state.schema_version or "",
        "status": state.status,
        "stale": stale,
        "last_success_at": _iso(state.last_success_at),
        "last_attempt_at": _iso(state.last_attempt_at),
        "last_error": state.last_error,
        "items": state.items_total,
        "claims": state.claims_total,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value is not None else None


def list_items(
    session: Session,
    *,
    zone: str | None = None,
    q: str | None = None,
    status: str = "active",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    stmt = select(models.XingyanItem)
    if zone:
        stmt = stmt.where(models.XingyanItem.zone == zone)
    if status in models.ITEM_STATUSES:
        stmt = stmt.where(models.XingyanItem.status == status)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                models.XingyanItem.title_zh.ilike(needle),
                models.XingyanItem.title_en.ilike(needle),
                models.XingyanItem.summary_zh.ilike(needle),
                models.XingyanItem.summary_en.ilike(needle),
                models.XingyanItem.slug.ilike(needle),
            )
        )
    total = session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0
    # 有日期的在前（研究/内参按发布日倒序），无日期的（学院 uodated 口径）
    # 按 order 后置；``case`` 写法在 SQLite 与 PG 上一致，不依赖 NULLS LAST。
    rows = session.scalars(
        stmt.order_by(
            case((models.XingyanItem.published_at.is_(None), 1), else_=0),
            models.XingyanItem.published_at.desc(),
            models.XingyanItem.item_order.asc(),
            models.XingyanItem.slug,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [item_summary(row) for row in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


def item_summary(row: models.XingyanItem) -> dict:
    return {
        "slug": row.slug,
        "zone": row.zone,
        "type": row.item_type or None,
        "order": row.item_order,
        "title": {"zh": row.title_zh, "en": row.title_en},
        "summary": {"zh": row.summary_zh, "en": row.summary_en},
        "published_at": row.published_at,
        "updated_at": row.updated_at,
        "tags": list(row.tags_json or []),
        "claim_count": row.claim_count,
        "status": row.status,
        "url": dict(row.url_json or {}),
    }


def get_item(session: Session, slug: str) -> dict:
    row = session.scalar(
        select(models.XingyanItem).where(models.XingyanItem.slug == slug)
    )
    if row is None:
        raise NotFound(f"条目 {slug} 不在快照中")
    claims = session.scalars(
        select(models.XingyanClaim)
        .where(models.XingyanClaim.slug == slug)
        .order_by(models.XingyanClaim.claim_id)
    ).all()
    payload = item_summary(row)
    payload.update(
        {
            "body": {"zh": row.body_zh, "en": row.body_en},
            "sources": dict(row.sources_json or {}),
            "claims": [claim_summary(c) for c in claims],
            "fetched_at": _iso(row.fetched_at),
        }
    )
    return payload


def claim_summary(claim: models.XingyanClaim) -> dict:
    return {
        "claim_id": claim.claim_id,
        "global_id": f"{claim.slug}/{claim.claim_id}",
        "slug": claim.slug,
        "type": claim.claim_type,
        "section": {"zh": claim.section_zh, "en": claim.section_en},
        "evidence": list(claim.evidence_json or []),
        "status": claim.status,
    }


def resolve_claim(session: Session, slug: str, claim_id: str, locale: str = "zh") -> dict:
    """全局 ID ``slug/claim-id`` → 骨架 + 锚小节正文 + 可解析的证据来源。"""
    if locale not in LOCALES:
        raise ValueError(f"locale 必须是 {LOCALES} 之一")
    item = session.scalar(
        select(models.XingyanItem).where(models.XingyanItem.slug == slug)
    )
    claim = session.scalar(
        select(models.XingyanClaim).where(
            models.XingyanClaim.slug == slug,
            models.XingyanClaim.claim_id == claim_id,
        )
    )
    if item is None and claim is None:
        raise NotFound(f"{slug}/{claim_id} 不在快照中")
    if claim is None:
        raise NotFound(f"{slug}/{claim_id} 不在快照中")
    if claim.status == "withdrawn":
        raise ClaimWithdrawn(claim)

    heading = claim.section_zh if locale == "zh" else claim.section_en
    body = item.body_zh if locale == "zh" else item.body_en if item else ""
    section_text, found = extract_section(body, heading)
    return {
        "global_id": f"{slug}/{claim_id}",
        "slug": slug,
        "claim_id": claim_id,
        "type": claim.claim_type,
        "section": {"zh": claim.section_zh, "en": claim.section_en},
        "section_text": section_text,
        "section_found": found,
        "locale": locale,
        "evidence": [
            {"key": key, **source_entry_for(item, key, locale)}
            for key in (claim.evidence_json or [])
        ],
        "item": {
            "title": item_summary(item)["title"] if item else {},
            "zone": item.zone if item else "",
            "status": item.status if item else "withdrawn",
            "url": dict(item.url_json or {}) if item else {},
        },
    }


def source_entry_for(item: models.XingyanItem | None, key: str, locale: str) -> dict:
    """把证据 key（src-N）解析为该条目的来源项。

    来源项有两种形状：带 id 的对象（研究区）与纯文本（内参/学院）。按 id
    匹配；匹配不到就如实标 ``resolved=false``，绝不编造标签。
    """
    entries = ((item.sources_json or {}).get(locale) if item else None) or []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == key:
            return {
                "resolved": True,
                "label": entry.get("label") or "",
                "url": entry.get("url") or "",
            }
    # 纯文本来源列表没有 id 时按序号对应（src-1 → 第 1 条）
    index = _src_index(key)
    if index is not None and 0 <= index < len(entries) and not isinstance(entries[index], dict):
        return {"resolved": True, "label": str(entries[index]), "url": ""}
    return {"resolved": False, "label": "", "url": ""}


def _src_index(key: str) -> int | None:
    m = re.fullmatch(r"src-(\d+)", key or "")
    return int(m.group(1)) - 1 if m else None


_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.MULTILINE)


def extract_section(body: str, heading: str) -> tuple[str, bool]:
    """从 markdown 正文里取出以 ``heading`` 开头的那一小节正文。

    只在标题行完全相等时命中（锚点是发布门校验过的确切标题文本）；命中后
    截到同级或更高级的下一个标题为止。找不到就返回 ("", False)。
    """
    if not body or not heading:
        return "", False
    target = heading.strip()
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        if m.group(2).strip() != target:
            continue
        level = len(m.group(1))
        start = m.end()
        end = len(body)
        for nxt in matches[i + 1 :]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        return body[start:end].strip(), True
    return "", False
