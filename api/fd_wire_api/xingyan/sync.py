"""摄取管道：manifest 指纹短路 → 三区全量原子 upsert → 缺席标撤销。

四条契约（spec：快照摄取幂等且失败不冒充空集）：

1. **幂等**：先取 ``content-manifest.json``，指纹与水位一致即跳过（零写入）。
2. **版本断言**：任一区 feed 顶层 ``schema_version`` 不在兼容范围即整体拒摄、
   留痕，上一版继续服务——不猜字段，不部分写入。
3. **失败不冒充空集**：三区必须全部拉取成功，且每区条目数不低于下界，
   才进写事务；任一失败即中止，旧快照原样保留、``status`` 标陈旧。
4. **撤销而非删除**：三区全成后，表内有而 feed 无的 slug 标 ``withdrawn``
   （可逆）；条目内消失的 claim 同样标撤销并保留登记。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from . import models
from .client import ZONES, FeedClient, FeedSchemaMismatch, FeedUnavailable
from .db import get_session
from .settings import get_settings

#: 与上游 feed 记录形状绑定的字段集（版本断言只在 schema_version 上做，
#: 字段缺失按"未声明"处理——生命周期内新增字段不构成破坏性变更）。
_SYNC_ROW_ID = 1


@dataclass
class SyncResult:
    status: str  # ok | skipped | stale | schema_mismatch | upstream_error
    source_sha: str = ""
    schema_version: str = ""
    items: int = 0
    claims: int = 0
    withdrawn_items: int = 0
    withdrawn_claims: int = 0
    detail: str = ""
    zones: dict = field(default_factory=dict)


def fingerprint(manifest: dict) -> str:
    """上游版本指纹：三区 (items, sha) 归一后摘要。

    只看参与供给的三区——manifest 里的其他区（如 updates）不在供给面，
    其变动不应触发一次全量摄取。
    """
    zones = manifest.get("zones") or {}
    canon = {
        zone: {
            "items": (zones.get(zone) or {}).get("items"),
            "sha": (zones.get(zone) or {}).get("sha"),
        }
        for zone in ZONES
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _claim_rows(payload: dict) -> tuple[dict[str, dict], int]:
    """把条目载荷的 claims（按语言分侧）合并成 {claim_id: row}。

    两侧 ID 应相同（发布门已验）；这里取并集，任一语言侧缺的 section 记空串
    ——不因一侧缺而丢 claim（否则撤销态会被误判成"从未存在"）。
    """
    sides = payload.get("claims") or {}
    merged: dict[str, dict] = {}
    for locale in ("zh", "en"):
        for raw in sides.get(locale) or []:
            if not isinstance(raw, dict):
                continue
            cid = raw.get("id")
            if not isinstance(cid, str) or not cid:
                continue
            row = merged.setdefault(
                cid,
                {"type": "", "section_zh": "", "section_en": "", "evidence": []},
            )
            if locale == "zh":
                row["section_zh"] = str(raw.get("section") or "")
            else:
                row["section_en"] = str(raw.get("section") or "")
            # 类型与证据以先见者为准（两侧应一致，发布门已验同构）
            if not row["type"]:
                row["type"] = str(raw.get("type") or "")
            if not row["evidence"]:
                ev = raw.get("evidence")
                if isinstance(ev, list):
                    row["evidence"] = [str(x) for x in ev]
    return merged, len(merged)


class XingyanFeedSync:
    """摄取管道（快照表的**唯一**写入口）。"""

    def __init__(self, client: FeedClient | None = None):
        self._settings = get_settings()
        self._client = client

    # ── 上游拉取 ──────────────────────────────────────────────────────────
    def _collect(self) -> tuple[dict, dict[str, dict], str]:
        """取 manifest + 三区全量；任一环节失败即抛（不写任何东西）。"""
        own = self._client is None
        client = self._client or FeedClient(
            self._settings.feed_base_url, self._settings.http_timeout_seconds
        )
        try:
            manifest = client.fetch_manifest()
            zones: dict[str, dict] = {}
            for zone in ZONES:
                data = client.fetch_zone(zone)
                version = str(data.get("schema_version") or "")
                if version not in self._settings.compatible_schema_versions:
                    raise FeedSchemaMismatch(zone, data.get("schema_version"))
                items = data.get(zone) or []
                if len(items) < self._settings.min_zone_items:
                    raise FeedUnavailable(
                        f"{zone} 只返回 {len(items)} 条（下界 "
                        f"{self._settings.min_zone_items}）——按上游半版本处理"
                    )
                declared = ((manifest.get("zones") or {}).get(zone) or {}).get("items")
                if isinstance(declared, int) and declared != len(items):
                    raise FeedUnavailable(
                        f"{zone} 条目数 {len(items)} 与 manifest 声明 {declared} 不符"
                        f"——按上游半版本处理"
                    )
                zones[zone] = data
            return manifest, zones, str(
                next(iter(zones.values())).get("schema_version") or ""
            )
        finally:
            if own and client is not None:
                client.close()

    # ── 主入口 ────────────────────────────────────────────────────────────
    def run(self, force: bool = False) -> SyncResult:
        try:
            manifest, zones, schema_version = self._collect()
        except FeedSchemaMismatch as exc:
            return self._mark_failure("schema_mismatch", str(exc))
        except FeedUnavailable as exc:
            return self._mark_failure("upstream_error", str(exc))

        sha = fingerprint(manifest)
        state = self._read_state()
        if (
            not force
            and state is not None
            and state.status == "ok"
            and state.source_sha == sha
        ):
            return SyncResult(
                status="skipped",
                source_sha=sha,
                schema_version=state.schema_version,
                items=state.items_total,
                claims=state.claims_total,
                detail="同指纹，零写入",
            )

        try:
            return self._apply(sha, schema_version, zones)
        except Exception as exc:  # noqa: BLE001 —— 任何写失败都要留陈旧标记
            return self._mark_failure("upstream_error", f"写入失败：{exc}")

    # ── 写事务 ────────────────────────────────────────────────────────────
    def _apply(self, sha: str, schema_version: str, zones: dict[str, dict]) -> SyncResult:
        session = get_session()
        now = _now()
        seen_slugs: set[str] = set()
        seen_claims: set[tuple[str, str]] = set()
        zone_counts: dict[str, int] = {}
        try:
            for zone in ZONES:
                items = zones[zone][zone]
                zone_counts[zone] = len(items)
                for payload in items:
                    self._upsert_item(session, zone, payload, sha, now, seen_slugs, seen_claims)
            # 缺席判定：只有三区都成功走到这里才执行（上面的异常即整体回滚）
            withdrawn_items, withdrawn_claims = self._mark_absent(
                session, seen_slugs, seen_claims, sha
            )
            # 会话是 autoflush=False（写路径显式控制刷写时机）：计数前必须先
            # flush，否则刚 add 的待写行不计入，水位会记成 0。
            session.flush()
            items_total, claims_total = self._counts(session)
            state = session.get(models.XingyanSyncState, _SYNC_ROW_ID)
            if state is None:
                state = models.XingyanSyncState(id=_SYNC_ROW_ID)
                session.add(state)
            state.source_sha = sha
            state.schema_version = schema_version
            state.status = "ok"
            state.last_success_at = now
            state.last_attempt_at = now
            state.last_error = None
            state.items_total = items_total
            state.claims_total = claims_total
            session.commit()
            return SyncResult(
                status="ok",
                source_sha=sha,
                schema_version=schema_version,
                items=items_total,
                claims=claims_total,
                withdrawn_items=withdrawn_items,
                withdrawn_claims=withdrawn_claims,
                zones=zone_counts,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _upsert_item(
        self,
        session,
        zone: str,
        payload: dict,
        sha: str,
        now: datetime,
        seen_slugs: set[str],
        seen_claims: set[tuple[str, str]],
    ) -> None:
        slug = payload.get("slug")
        if not isinstance(slug, str) or not slug:
            return
        seen_slugs.add(slug)
        title = payload.get("title") or {}
        summary = payload.get("summary") or {}
        body = payload.get("body") or {}
        url = payload.get("url") or {}
        claims, claim_count = _claim_rows(payload)

        row = session.scalar(
            select(models.XingyanItem).where(models.XingyanItem.slug == slug)
        )
        if row is None:
            row = models.XingyanItem(slug=slug)
            session.add(row)
        row.zone = zone
        row.item_type = str(payload.get("kind") or payload.get("level") or "")
        order = payload.get("order")
        row.item_order = int(order) if isinstance(order, int) else None
        row.title_zh = str(title.get("zh") or "")
        row.title_en = str(title.get("en") or "")
        row.summary_zh = str(summary.get("zh") or "")
        row.summary_en = str(summary.get("en") or "")
        row.published_at = (
            str(payload.get("date")) if payload.get("date") else None
        )
        row.updated_at = (
            str(payload.get("updated")) if payload.get("updated") else None
        )
        row.tags_json = [str(t) for t in (payload.get("tags") or [])]
        row.body_zh = str(body.get("zh") or "")
        row.body_en = str(body.get("en") or "")
        sources = payload.get("sources") or {}
        row.sources_json = {
            "zh": sources.get("zh") or [],
            "en": sources.get("en") or [],
        }
        row.claim_count = claim_count
        row.url_json = {"en": str(url.get("en") or ""), "zh": str(url.get("zh") or "")}
        row.status = "active"
        row.source_sha = sha
        row.fetched_at = now

        existing = {
            c.claim_id: c
            for c in session.scalars(
                select(models.XingyanClaim).where(models.XingyanClaim.slug == slug)
            )
        }
        for cid, c in claims.items():
            seen_claims.add((slug, cid))
            crow = existing.get(cid)
            if crow is None:
                crow = models.XingyanClaim(slug=slug, claim_id=cid)
                session.add(crow)
            crow.claim_type = c["type"]
            crow.section_zh = c["section_zh"]
            crow.section_en = c["section_en"]
            crow.evidence_json = list(c["evidence"])
            crow.status = "active"
            crow.source_sha = sha
            crow.fetched_at = now
        # 条目内消失的 claim：保留登记、标撤销（不物理删除）
        for cid, crow in existing.items():
            if cid not in claims:
                crow.status = "withdrawn"
                crow.source_sha = sha
                crow.fetched_at = now

    def _mark_absent(
        self, session, seen_slugs: set[str], seen_claims: set[tuple[str, str]], sha: str
    ) -> tuple[int, int]:
        now = _now()
        items_w = claims_w = 0
        for row in session.scalars(
            select(models.XingyanItem).where(models.XingyanItem.status == "active")
        ):
            if row.slug not in seen_slugs:
                row.status = "withdrawn"
                row.source_sha = sha
                row.fetched_at = now
                items_w += 1
        for crow in session.scalars(
            select(models.XingyanClaim).where(models.XingyanClaim.status == "active")
        ):
            if (crow.slug, crow.claim_id) not in seen_claims:
                crow.status = "withdrawn"
                crow.source_sha = sha
                crow.fetched_at = now
                claims_w += 1
        return items_w, claims_w

    # ── 水位读写 ──────────────────────────────────────────────────────────
    def _read_state(self) -> models.XingyanSyncState | None:
        session = get_session()
        try:
            return session.get(models.XingyanSyncState, _SYNC_ROW_ID)
        finally:
            session.close()

    def _mark_failure(self, status: str, detail: str) -> SyncResult:
        session = get_session()
        try:
            state = session.get(models.XingyanSyncState, _SYNC_ROW_ID)
            if state is None:
                state = models.XingyanSyncState(id=_SYNC_ROW_ID)
                session.add(state)
            state.status = status
            state.last_attempt_at = _now()
            state.last_error = detail[:500]
            # 旧快照原样保留；只有曾经成功过才叫"陈旧"，从未成功叫"无内容"
            if state.last_success_at is not None:
                state.status = status
            session.commit()
            return SyncResult(
                status=status,
                source_sha=state.source_sha or "",
                schema_version=state.schema_version or "",
                items=state.items_total,
                claims=state.claims_total,
                detail=detail,
            )
        finally:
            session.close()

    @staticmethod
    def _counts(session) -> tuple[int, int]:
        items = session.query(models.XingyanItem).filter(
            models.XingyanItem.status == "active"
        ).count()
        claims = session.query(models.XingyanClaim).filter(
            models.XingyanClaim.status == "active"
        ).count()
        return items, claims
