"""fd-wire 的五个内核端口（establish-ontology-kernel 6.4 / design D3、D6、D7）。

内核持有机制（词表闸、写边界折叠、唯一写路径+配对事件、manifest、提案、账本），
本模块是**唯一**知道这些机制落在 ``entities`` / ``entity_relationships`` /
``ontology_events`` / ``ontology_snapshots`` / ``domain_action_instances`` 的地方。

**换底范围（重要）**：fd-wire 的业务写入仍走自己的 ``service.py``——本 change
只把 digest 与事件信封交给内核（6.2/6.3），端口在此落地是为了让符合性闸门能用
**真实落库映射**检查不变量，也为将来真正切写入路径留好接缝。把 service 改写成
内核壳是另一件事，不在本 change。

**投影规则**（内核 ``Unit`` ↔ fd-wire 行）：

- 实体：``kind="entity"``（客户内唯一的名字即 ``external_key``），``body`` 取
  ``description``，``tier`` 取行上的档位，其余列进 ``attrs``，``raw`` 带上
  ``versioning.entity_payload(row)``——事件负载是已发布契约，形状不能变；
- 关系：``kind`` 是基数（``n:1``/``1:1``/``1:n``/``m:n``），``external_key`` 是
  内核的 ``{from}->{kind}:{to}``（两端都是**实体名**），端点列进 ``attrs``。

**一处代价要说清**：fd-wire 的行有内核 ``Unit`` 不建模的必填域列（实体的
``dataset_id``、关系的 ``from_column``/``to_column``）。闸门的沙箱写不带这些值，
故 ``_SANDBOX_*`` 兜底常量只在**调用方没给**时填——它们是沙箱占位，不是业务数据；
真实调用方（将来的 service 壳）必须自带真值。

**事务纪律**：每个端口持有一个已进入事务的 ``Session``，自己从不 commit；内核
只保证 ``upsert`` 与 ``append`` 成对落库（design D2）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fd_ontology_kernel.contracts import (
    Event,
    Proposal,
    Publication,
    Unit,
    relationship_key,
)
from fd_ontology_kernel.kernel import AdapterDeclaration
from fd_ontology_kernel.vocab import Vocabulary
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import versioning
from .models import Entity, EntityRelationship, OntologyEvent, OntologySnapshot

#: 本机形的声明：内核契约 1.0，**形状 1 = id 寻址**（条目带自增 id）。
#: 注意与行的 ``SCHEMA_VERSION=2`` 区分：那是**导出文档**版本，不是 manifest 形状
#: 版本，两者是不同轴（design D6）。
DECLARATION = AdapterDeclaration(
    system="fd-wire",
    kernel_contract="1.0",
    manifest_shape=1,
)

#: 词表副本（与 ``service.py`` 的常量逐字对齐；本模块不 import service 是为了
#: 避开 datasets/ai 的导入链——同 0050 迁移「钉死副本 + 测试守对齐」的先例）。
ENTITY_KIND = versioning.KIND_ENTITY
RELATIONSHIP_KIND = versioning.KIND_RELATIONSHIP
RELATIONSHIP_KINDS: tuple[str, ...] = ("n:1", "1:1", "1:n", "m:n")
ENTITY_TIERS: tuple[str, ...] = ("raw", "bronze", "silver", "gold")
ENTITY_SENSITIVITIES: tuple[str, ...] = ("public", "internal", "confidential")

#: 沙箱占位（只在调用方没给必填域列时使用；见模块头注的「一处代价」）。
_SANDBOX_DATASET_ID = "conformance-sandbox"
_SANDBOX_COLUMN = "conformance_key"

#: 内核提案状态 ← 闸实例状态（design D7：映射，不新建提案表）。
_PROPOSAL_STATE = {
    "pending": "pending",
    "awaiting_confirmation": "pending",
    "executed": "confirmed",
    "rejected": "rejected",
    "failed": "rejected",
    "disabled": "rejected",
}


def wire_vocabulary(*, release: str = "wire-enums-1") -> Vocabulary:
    """fd-wire 的封闭词表：实体种类一种、关系基数四种，档位与敏感度两套枚举。

    这不是「为了凑够断言」编的集合：``service.py`` 对这四个集合都有单点校验，
    写进去的非法值会被 422 拒掉。
    """
    return Vocabulary.from_values(
        release=release,
        values={
            "entity_types": (ENTITY_KIND,),
            "relationship_kinds": RELATIONSHIP_KINDS,
            "tiers": ENTITY_TIERS,
            "sensitivities": ENTITY_SENSITIVITIES,
        },
    )


class WireVocabulary:
    """``VocabularySource``：一套固定枚举，所有客户共用（fd-wire 无域级词表）。"""

    def __init__(self, vocabulary: Vocabulary | None = None) -> None:
        self._vocabulary = vocabulary or wire_vocabulary()

    def load(self, *, scope_id: str) -> Vocabulary:
        return self._vocabulary


# ── 关系键：`{from}->{kind}:{to}` 的解析 ─────────────────────────────────────


def split_relationship_key(external_key: str) -> tuple[str, str, str]:
    """``{from}->{kind}:{to}`` → 三段。

    fd-wire 的基数本身带冒号（``n:1``），所以不能按「最后一个冒号」切；按已知
    基数集合做前缀匹配，闭集内唯一。解不出来就抛错并带上原键——静默返回半个
    身份比报错危险得多。
    """
    text = (external_key or "").strip()
    head, sep, rest = text.partition("->")
    if not sep or not head:
        raise ValueError(f"关系键缺少 '->' 前缀：{external_key!r}")
    for kind in sorted(RELATIONSHIP_KINDS, key=len, reverse=True):
        prefix = f"{kind}:"
        if rest.startswith(prefix):
            to_key = rest[len(prefix):]
            if to_key:
                return head, kind, to_key
    raise ValueError(
        f"关系键的基数不在封闭集合 {RELATIONSHIP_KINDS} 内：{external_key!r}"
    )


# ── 行 ↔ Unit ───────────────────────────────────────────────────────────────


#: 内核在 ``attrs`` 里自己塞的键（写边界折叠的 ``aliases``/``folded_by`` 等）。
#: 它们没有 fd-wire 的业务列，落 ``kernel_attrs``（0055 的附加袋）。
_KERNEL_ONLY_ATTRS: tuple[str, ...] = ("aliases", "folded_by", "tags", "deleted")


def _split_attrs(attrs: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(业务列属性, 内核附加袋)``。

    分成两堆而不是「统统塞进袋子」：业务字段有自己的列和 service 校验，进袋子
    等于绕过校验。
    """
    business: dict[str, Any] = {}
    kernel_only: dict[str, Any] = {}
    for name, value in dict(attrs or {}).items():
        if name in _KERNEL_ONLY_ATTRS:
            kernel_only[name] = value
        else:
            business[name] = value
    return business, kernel_only


def _merged_attrs(row_attrs: Mapping[str, Any], kernel_attrs: Any) -> dict[str, Any]:
    merged = dict(kernel_attrs or {})
    merged.update(row_attrs)
    return merged


def unit_from_entity(entity: Entity) -> Unit:
    """实体行 → 内核 unit（``raw`` 保留已发布的事件负载形状）。"""
    return Unit(
        scope_id=entity.customer_id,
        kind=ENTITY_KIND,
        external_key=entity.name,
        body=entity.description or "",
        tier=entity.tier or "raw",
        attrs=_merged_attrs(
            {
                "dataset_id": entity.dataset_id,
                "pk_columns": list(entity.pk_columns or []),
                "display_column": entity.display_column,
                "column_notes": dict(entity.column_notes or {}) or None,
                "sensitivity": entity.sensitivity,
            },
            entity.kernel_attrs,
        ),
        raw=versioning.entity_payload(entity),
    )


def unit_from_relationship(
    relationship: EntityRelationship, *, names: Mapping[int, str]
) -> Unit:
    """关系行 → 内核 unit（键用两端实体名；id 只在 ``raw``/``attrs`` 里）。"""
    from_name = names.get(int(relationship.from_entity_id), "")
    to_name = names.get(int(relationship.to_entity_id), "")
    key = relationship_key(
        from_key=from_name, kind=relationship.kind, to_key=to_name
    )
    return Unit(
        scope_id=relationship.customer_id,
        kind=relationship.kind,
        external_key=key,
        body=relationship.label or "",
        tier=relationship.tier or "raw",
        attrs=_merged_attrs(
            {
                "from_key": from_name,
                "to_key": to_name,
                "from_column": relationship.from_column,
                "to_column": relationship.to_column,
                "declared_kind": relationship.declared_kind,
                "sensitivity": relationship.sensitivity,
                "junction_dataset_id": relationship.junction_dataset_id,
                "junction_source_column": relationship.junction_source_column,
                "junction_target_column": relationship.junction_target_column,
            },
            relationship.kernel_attrs,
        ),
        raw=versioning.relationship_payload(relationship),
    )


# ── UnitStore ───────────────────────────────────────────────────────────────


class WireUnitStore:
    """``UnitStore`` over ``entities`` / ``entity_relationships``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- 查询助手 --

    def _entity(self, scope_id: str, name: str) -> Entity | None:
        return self._session.scalar(
            select(Entity).where(
                Entity.customer_id == scope_id, Entity.name == name
            )
        )

    def _names(self, scope_id: str, ids: Iterable[int]) -> dict[int, str]:
        return versioning._entity_names(
            self._session, customer_id=scope_id, ids=ids
        )

    def _relationship(
        self, scope_id: str, *, from_id: int, to_id: int, kind: str
    ) -> EntityRelationship | None:
        return self._session.scalar(
            select(EntityRelationship)
            .where(
                EntityRelationship.customer_id == scope_id,
                EntityRelationship.from_entity_id == from_id,
                EntityRelationship.to_entity_id == to_id,
                EntityRelationship.kind == kind,
            )
            .order_by(EntityRelationship.id)
            .limit(1)
        )

    def _relationship_by_key(
        self, scope_id: str, external_key: str
    ) -> EntityRelationship | None:
        from_key, kind, to_key = split_relationship_key(external_key)
        source = self._entity(scope_id, from_key)
        target = self._entity(scope_id, to_key)
        if source is None or target is None:
            return None
        return self._relationship(
            scope_id, from_id=source.id, to_id=target.id, kind=kind
        )

    # -- 端口 --

    def get(self, *, scope_id: str, kind: str, external_key: str) -> Unit | None:
        if kind == ENTITY_KIND:
            row = self._entity(scope_id, external_key)
            return unit_from_entity(row) if row is not None else None
        row = self._relationship_by_key(scope_id, external_key)
        if row is None:
            return None
        names = self._names(scope_id, (row.from_entity_id, row.to_entity_id))
        return unit_from_relationship(row, names=names)

    def upsert(self, unit: Unit) -> Unit:
        if unit.kind == ENTITY_KIND:
            return self._upsert_entity(unit)
        return self._upsert_relationship(unit)

    def _upsert_entity(self, unit: Unit) -> Unit:
        business, kernel_only = _split_attrs(unit.attrs)
        attrs = business
        row = self._entity(unit.scope_id, unit.external_key)
        if row is None:
            row = Entity(
                customer_id=unit.scope_id,
                name=unit.external_key,
                # 一集一体（uq_entities_customer_dataset）：沙箱占位必须逐实体唯一，
                # 否则第二个沙箱实体撞唯一约束——那不是业务错误，是占位不唯一。
                dataset_id=str(
                    attrs.get("dataset_id")
                    or f"{_SANDBOX_DATASET_ID}:{unit.external_key}"
                ),
                pk_columns=list(attrs.get("pk_columns") or []),
            )
            self._session.add(row)
        row.description = unit.body or None
        row.tier = unit.tier or None
        if "pk_columns" in attrs:
            row.pk_columns = list(attrs.get("pk_columns") or [])
        if "display_column" in attrs:
            row.display_column = attrs.get("display_column")
        if "column_notes" in attrs:
            row.column_notes = attrs.get("column_notes")
        if "sensitivity" in attrs:
            row.sensitivity = attrs.get("sensitivity")
        if kernel_only:
            merged = dict(row.kernel_attrs or {})
            merged.update(kernel_only)
            row.kernel_attrs = merged
        self._session.flush()
        return unit_from_entity(row)

    def _upsert_relationship(self, unit: Unit) -> Unit:
        business, kernel_only = _split_attrs(unit.attrs)
        attrs = business
        from_key, kind, to_key = split_relationship_key(unit.external_key)
        source = self._entity(unit.scope_id, from_key)
        target = self._entity(unit.scope_id, to_key)
        if source is None or target is None:
            raise ValueError(
                f"关系键两端必须是本企业已存在的实体名：{unit.external_key!r}"
            )
        row = self._relationship(
            unit.scope_id, from_id=source.id, to_id=target.id, kind=kind
        )
        if row is None:
            row = EntityRelationship(
                customer_id=unit.scope_id,
                from_entity_id=source.id,
                from_column=str(attrs.get("from_column") or _SANDBOX_COLUMN),
                to_entity_id=target.id,
                to_column=str(attrs.get("to_column") or _SANDBOX_COLUMN),
                kind=kind,
            )
            self._session.add(row)
        row.label = unit.body or None
        row.tier = unit.tier or None
        for name in (
            "declared_kind",
            "sensitivity",
            "junction_dataset_id",
            "junction_source_column",
            "junction_target_column",
        ):
            if name in attrs:
                setattr(row, name, attrs.get(name))
        if "from_column" in attrs:
            row.from_column = str(attrs["from_column"])
        if "to_column" in attrs:
            row.to_column = str(attrs["to_column"])
        if kernel_only:
            merged = dict(row.kernel_attrs or {})
            merged.update(kernel_only)
            row.kernel_attrs = merged
        self._session.flush()
        names = {source.id: source.name, target.id: target.name}
        return unit_from_relationship(row, names=names)

    def list_live(self, *, scope_id: str) -> list[Unit]:
        entities = list(
            self._session.scalars(
                select(Entity)
                .where(Entity.customer_id == scope_id)
                .order_by(Entity.name)
            ).all()
        )
        relationships = list(
            self._session.scalars(
                select(EntityRelationship)
                .where(EntityRelationship.customer_id == scope_id)
                .order_by(EntityRelationship.id)
            ).all()
        )
        names = self._names(scope_id, {e.id for e in entities})
        return [unit_from_entity(e) for e in entities] + [
            unit_from_relationship(r, names=names) for r in relationships
        ]

    def live_key_kinds(self, *, scope_id: str) -> dict[str, str]:
        """只有实体能当关系端点（fd-wire 不建模「关系之间」的边）。"""
        rows = self._session.execute(
            select(Entity.name).where(Entity.customer_id == scope_id)
        ).all()
        return {str(row[0]): ENTITY_KIND for row in rows}

    def raw_count(self) -> int:
        """库里实例行数（账本自检的「打开前后不变」判据，跨作用域）。"""
        entities = self._session.scalar(select(func.count(Entity.id)))
        relationships = self._session.scalar(select(func.count(EntityRelationship.id)))
        return int(entities or 0) + int(relationships or 0)


# ── EventJournal ────────────────────────────────────────────────────────────


class WireEventJournal:
    """``EventJournal`` over ``ontology_events``（append-only，不改不删）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def next_seq(self, scope_id: str) -> int:
        """内核在 ``_next_seq`` 里认这个名字：有 seq 的面就交给存储定序。"""
        return versioning.next_event_seq(self._session, customer_id=scope_id)

    def append(self, event: Event) -> Event:
        row = OntologyEvent(
            customer_id=event.scope_id,
            seq=int(event.seq) if event.seq is not None else self.next_seq(event.scope_id),
            op=event.op,
            target_kind=event.target_kind,
            # fd-wire 的事件表按代理键存储；内核只给语义键时必须反解出 id。
            target_id=self._resolve_target_id(event),
            target_key=event.target_key,
            before_json=dict(event.before) if event.before is not None else None,
            after_json=dict(event.after) if event.after is not None else None,
            actor=event.actor,
            via=event.via if event.via in versioning.VIAS else versioning.VIA_CONSOLE,
            source_ref=event.source_ref,
            source_instance_id=event.source_instance_id,
        )
        self._session.add(row)
        self._session.flush()
        return event

    def _resolve_target_id(self, event: Event) -> int:
        if event.target_ref is not None:
            return int(event.target_ref)
        store = WireUnitStore(self._session)
        if event.target_kind == ENTITY_KIND:
            row = store._entity(event.scope_id, event.target_key)
        else:
            row = store._relationship_by_key(event.scope_id, event.target_key)
        if row is None:
            raise ValueError(
                f"事件需要行 id，但按语义键找不到行：{event.target_kind}/"
                f"{event.target_key!r}"
            )
        return int(row.id)

    def fold(self, *, scope_id: str) -> list[Event]:
        names_cache: dict[int, str] = {}
        rows = versioning.list_events(self._session, customer_id=scope_id)
        out: list[Event] = []
        for row in rows:

            key = row.target_key
            if not key:
                key = self._derive_key(row, names_cache)
            out.append(
                Event(
                    scope_id=row.customer_id,
                    op=row.op,
                    target_kind=row.target_kind,
                    target_key=key or f"#{row.target_id}",
                    actor=row.actor or "",
                    via=row.via,
                    before=row.before_json,
                    after=row.after_json,
                    target_ref=row.target_id,
                    seq=row.seq,
                    source_ref=row.source_ref,
                    source_instance_id=row.source_instance_id,
                    occurred_at=row.created_at.isoformat()
                    if row.created_at is not None
                    else None,
                )
            )
        return out

    def _derive_key(self, row: OntologyEvent, cache: dict[int, str]) -> str | None:
        """历史行（0054 前）没有 ``target_key``：从行反解——只对未改名的对象成立。"""
        payload = row.after_json or row.before_json or {}
        if row.target_kind == ENTITY_KIND:
            name = payload.get("name")
            return str(name) if name else None
        return versioning.target_key_for(
            self._session,
            customer_id=row.customer_id,
            target_kind=row.target_kind,
            payload=payload,
        )

    def count_since(
        self,
        *,
        scope_id: str,
        seq: int | None = None,
        since: str | None = None,
    ) -> int:
        if seq is not None:
            stmt = select(func.count(OntologyEvent.id)).where(
                OntologyEvent.customer_id == scope_id, OntologyEvent.seq > int(seq)
            )
            return int(self._session.scalar(stmt) or 0)
        return versioning.count_events_since(
            self._session, customer_id=scope_id, after_created_at=None
        )


# ── PublicationStore ────────────────────────────────────────────────────────


def publication_from_row(row: OntologySnapshot) -> Publication:
    return Publication(
        scope_id=row.customer_id,
        version=int(row.version),
        digest=row.digest,
        payload=row.manifest_json or {},
        manifest_schema_version=DECLARATION.manifest_shape,
        trigger=row.trigger,
        label=row.label,
        created_by=row.created_by,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


class WirePublicationStore:
    """``PublicationStore`` over ``ontology_snapshots``（insert-only）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest(self, *, scope_id: str) -> Publication | None:
        row = versioning.latest_snapshot(self._session, customer_id=scope_id)
        return publication_from_row(row) if row is not None else None

    def get(
        self,
        *,
        scope_id: str,
        version: int | None = None,
        digest: str | None = None,
    ) -> Publication | None:
        if version is None and digest is None:
            return None
        stmt = select(OntologySnapshot).where(
            OntologySnapshot.customer_id == scope_id
        )
        if version is not None:
            stmt = stmt.where(OntologySnapshot.version == int(version))
        if digest is not None:
            stmt = stmt.where(OntologySnapshot.digest == digest)
        row = self._session.scalar(stmt.order_by(OntologySnapshot.id.desc()).limit(1))
        return publication_from_row(row) if row is not None else None

    def next_version(self, *, scope_id: str) -> int:
        current = self._session.scalar(
            select(func.max(OntologySnapshot.version)).where(
                OntologySnapshot.customer_id == scope_id
            )
        )
        return int(current or 0) + 1

    def insert(self, publication: Publication) -> Publication:
        row = OntologySnapshot(
            customer_id=publication.scope_id,
            version=int(publication.version),
            digest=publication.digest,
            manifest_json=dict(publication.payload),
            trigger=publication.trigger,
            label=publication.label,
            created_by=publication.created_by,
        )
        self._session.add(row)
        self._session.flush()
        return publication_from_row(row)

    def count(self) -> int:
        """全部快照行数（幂等发布与账本只读的判据）。"""
        return int(self._session.scalar(select(func.count(OntologySnapshot.id))) or 0)


# ── ProposalBox ─────────────────────────────────────────────────────────────


def proposal_from_instance(instance, *, type_name: str) -> Proposal:
    """闸实例 → 内核提案（design D7：映射，不新建表）。

    ``external_key`` 用 ``{动作类型名}#{实例号}``：闸实例没有「提名键」这个概念
    （它的身份就是实例号），所以别把它包装成一个业务键。
    """
    evidence = dict(instance.args_json or {})
    from fd_ontology_kernel.proposals import evidence_digest

    return Proposal(
        scope_id=instance.customer_id,
        source=instance.channel,
        kind=type_name,
        external_key=f"{type_name}#{instance.id}",
        body="",
        evidence=evidence,
        evidence_digest=evidence_digest(evidence),
        submitted_by=instance.proposer_sub,
        state=_PROPOSAL_STATE.get(instance.status, "pending"),
        proposal_id=instance.id,
        decided_by=instance.confirmed_by,
        decision_note=instance.error or "",
    )


class WireProposalBox:
    """``ProposalBox`` over ``domain_action_instances``（动作闸执行记录）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _type_name(self, type_id: int) -> str:
        from ..domain_actions.models import DomainActionType

        name = self._session.scalar(
            select(DomainActionType.name).where(DomainActionType.id == int(type_id))
        )
        return str(name or f"action-type-{type_id}")

    def _one(self, scope_id: str, proposal_id: int) -> Proposal | None:
        from ..domain_actions.models import DomainActionInstance

        row = self._session.get(DomainActionInstance, int(proposal_id))
        if row is None or row.customer_id != scope_id:
            return None
        return proposal_from_instance(row, type_name=self._type_name(row.type_id))

    def find_claim(
        self, *, scope_id: str, external_key: str, evidence_digest: str
    ) -> Proposal | None:
        for proposal in self.list_pending(scope_id=scope_id, limit=1000):
            if (
                proposal.external_key == external_key
                and proposal.evidence_digest == evidence_digest
            ):
                return proposal
        return None

    def insert(self, proposal: Proposal) -> Proposal:
        """映射面只读：新提名仍走动作闸（``domain_actions.service``）。"""
        raise NotImplementedError(
            "fd-wire 的提案是动作闸执行记录的投影：请走 domain_actions.confirm/"
            "execute 流程，不要在这里插提案行"
        )

    def merge_submitters(
        self, *, proposal_id: int, submitters: Sequence[str]
    ) -> Proposal:
        proposal = self._one_any(proposal_id)
        if proposal is None:
            raise ValueError(f"闸实例 {proposal_id} 不存在")
        # 闸实例只有一个 proposer；合并提交方只能落在返回的提案视图上。
        return Proposal(
            scope_id=proposal.scope_id,
            source=proposal.source,
            kind=proposal.kind,
            external_key=proposal.external_key,
            body=proposal.body,
            evidence=proposal.evidence,
            evidence_digest=proposal.evidence_digest,
            submitted_by=proposal.submitted_by,
            state=proposal.state,
            sources=tuple(submitters),
            proposal_id=proposal_id,
        )

    def _one_any(self, proposal_id: int) -> Proposal | None:
        from ..domain_actions.models import DomainActionInstance

        row = self._session.get(DomainActionInstance, int(proposal_id))
        if row is None:
            return None
        return proposal_from_instance(row, type_name=self._type_name(row.type_id))

    def find_by_key(
        self, *, scope_id: str, external_key: str, state: str | None = None
    ) -> Proposal | None:
        from ..domain_actions.models import DomainActionInstance

        rows = self._session.scalars(
            select(DomainActionInstance)
            .where(DomainActionInstance.customer_id == scope_id)
            .order_by(DomainActionInstance.id)
        ).all()
        for row in rows:
            proposal = proposal_from_instance(
                row, type_name=self._type_name(row.type_id)
            )
            if proposal.external_key == external_key and (
                state is None or proposal.state == state
            ):
                return proposal
        return None

    def get(self, *, scope_id: str, proposal_id: int) -> Proposal | None:
        return self._one(scope_id, proposal_id)

    def decide(
        self, *, proposal_id: int, state: str, decided_by: str, note: str = ""
    ) -> Proposal:
        raise NotImplementedError(
            "决定权在动作闸：请调 domain_actions 的 confirm/reject（本端口只读映射）"
        )

    def list_pending(
        self, *, scope_id: str, batch: str | None = None, limit: int = 200
    ) -> list[Proposal]:
        from ..domain_actions.models import DomainActionInstance

        rows = self._session.scalars(
            select(DomainActionInstance)
            .where(
                DomainActionInstance.customer_id == scope_id,
                DomainActionInstance.status.in_(
                    ("pending", "awaiting_confirmation")
                ),
            )
            .order_by(DomainActionInstance.id)
            .limit(int(limit))
        ).all()
        return [
            proposal_from_instance(row, type_name=self._type_name(row.type_id))
            for row in rows
        ]


__all__ = [
    "DECLARATION",
    "ENTITY_KIND",
    "ENTITY_SENSITIVITIES",
    "ENTITY_TIERS",
    "RELATIONSHIP_KIND",
    "RELATIONSHIP_KINDS",
    "WireEventJournal",
    "WireProposalBox",
    "WirePublicationStore",
    "WireUnitStore",
    "WireVocabulary",
    "proposal_from_instance",
    "publication_from_row",
    "split_relationship_key",
    "unit_from_entity",
    "unit_from_relationship",
    "wire_vocabulary",
]
