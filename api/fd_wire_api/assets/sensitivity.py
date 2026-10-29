"""有效敏感度闭包计算（wire-sensitivity-propagation 任务 2.1/2.2）。

**有效敏感度 = 自身声明与全部派生祖先闭包取严**（敏感 > 内部 > 常规）——
读时计算的纯派生口径，不落列（祖先/自身任一变化，下一次读取即如实，
spec 硬要求「无陈旧值」）。三个消费方共用本模块（同源同 helper）：

1. 资产目录/详情/筛选（assets/routes，有效敏感度 + 「继承」标记）；
2. 设档闸（market/service.set_visibility 闸门 1，directed/market 拒绝）；
3. 向下传播撤回（market/service.propagate_downward_reverts，敏感化连带撤回）；
4. （姊妹 change）产物面双点闸（fd-wire wire-sensitivity-market-closure）。

数据源 = ``dataset_parent_refs``（本地父集边，零引擎调用）。遍历实现：
**Python BFS 逐点闭包（edges 按客户整批装载）**——客户数据集数受套餐配额
约束（数百量级），每次调用两条批量查询（边 + 命中集元数据），无逐行 N+1；
刻意不引入双方言递归 CTE（方言面风险大于收益）。design D1 的 CTE 主案
据此调整为 BFS，物化列+事件失效重算仍列后备（若列表实测变慢）。

fail-closed 口径（design D3）：父引用指向不存在/他企的数据集（dangling）、
环、超深（>32 跳）→ ``incomplete=True``——闸按「父集不全」拒绝设档并如实
指名；**incomplete 不伪装成「敏感」**（目录徽标显示自身声明 + 不全警示，
两件事分开）。

环防护与深度上限在本模块统一实施（任务 1.3），调用方不得自建遍历。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import SENSITIVITY_DEFAULT, DatasetParentRef

#: 闭包深度上限（环防护之外的第二道闸；超深按父集不全同语义 fail-closed）
MAX_CLOSURE_DEPTH = 32

#: 敏感度取严序（rank 越大越严）
SENSITIVITY_RANK: dict[str, int] = {"常规": 0, "内部": 1, "敏感": 2}


def load_parent_edges(db: Session, customer_id: str) -> dict[str, list[str]]:
    """本客户全部父集边（child → [parent...]；整批一次查询）。"""
    rows = (
        db.execute(
            select(DatasetParentRef).where(DatasetParentRef.customer_id == customer_id)
        )
        .scalars()
        .all()
    )
    edges: dict[str, list[str]] = {}
    for row in rows:
        edges.setdefault(row.child_dataset_id, []).append(row.parent_dataset_id)
    return edges


def effective_sensitivity_bulk(
    db: Session,
    customer_id: str,
    dataset_ids: list[str],
    *,
    edges: dict[str, list[str]] | None = None,
    own_levels: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> dict[str, dict]:
    """批量计算有效敏感度（列表页与闸共用；一次调用两条批量查询）。

    返回 ``{dataset_id: {level, own, inherited, incomplete, incomplete_reason,
    chain}}``——``chain`` 为携带最严档位的祖先链（``[{id, name, sensitivity}]``，
    自身即最严时为空表）；``incomplete`` 时 ``level`` 仍为已知部分的最严档
    （调用方按 incomplete 独立处置，两件事不混）。

    ``edges``/``own_levels``/``names`` 可由调用方注入（列表页已批量装载同名
    数据时避免重复查询）；注入**允许部分覆盖**——闭包触及而注入缺失的 id
    自动补查（不会把缺失误当默认值）。
    """
    ids = [did for did in dataset_ids if did]
    if not ids:
        return {}
    if edges is None:
        edges = load_parent_edges(db, customer_id)

    # 闭包涉及的全部节点 = 起点 ∪ 沿边可达祖先（含 dangling 引用 id）
    touched: set[str] = set(ids)
    frontier = list(ids)
    depth = 0
    while frontier and depth < MAX_CLOSURE_DEPTH:
        nxt: list[str] = []
        for did in frontier:
            for parent in edges.get(did, ()):
                if parent not in touched:
                    touched.add(parent)
                    nxt.append(parent)
        frontier = nxt
        depth += 1

    from .models import AssetMeta  # noqa: PLC0415 —— 同模块惰性避免环

    merged_levels: dict[str, str] = dict(own_levels or {})
    missing_levels = touched - set(merged_levels)
    if missing_levels:
        rows = db.execute(
            select(AssetMeta.dataset_id, AssetMeta.sensitivity).where(
                AssetMeta.dataset_id.in_(missing_levels)
            )
        ).all()
        for row in rows:
            merged_levels[row[0]] = row[1] or SENSITIVITY_DEFAULT
    own_levels = merged_levels

    from ..datasets.models import Dataset  # noqa: PLC0415

    merged_names: dict[str, str] = dict(names or {})
    missing_names = touched - set(merged_names)
    if missing_names:
        # 只认本客户的行——他企/不存在的引用在此落空 → dangling → incomplete
        rows = db.execute(
            select(Dataset.id, Dataset.name).where(
                Dataset.id.in_(missing_names),
                Dataset.customer_id == customer_id,
            )
        ).all()
        for row in rows:
            merged_names[row[0]] = row[1]
    names = merged_names

    out: dict[str, dict] = {}
    for start in ids:
        # BFS 祖先闭包（visited 防环；深度超限 → incomplete）
        visited: set[str] = set()
        frontier = [start]
        depth = 0
        ancestors: list[str] = []
        incomplete_reason: str | None = None
        while frontier and incomplete_reason is None:
            if depth >= MAX_CLOSURE_DEPTH:
                incomplete_reason = "祖先闭包超过深度上限"
                break
            nxt: list[str] = []
            for did in frontier:
                for parent in edges.get(did, ()):
                    if parent in visited or parent == start:
                        incomplete_reason = "祖先闭包存在环"
                        break
                    visited.add(parent)
                    ancestors.append(parent)
                    if parent not in names:
                        # dangling：父集行已不存在（删除未清边/回填缺口）
                        incomplete_reason = f"父集引用不可解析: {parent}"
                        break
                    nxt.append(parent)
                if incomplete_reason:
                    break
            frontier = nxt
            depth += 1

        own = own_levels.get(start) or SENSITIVITY_DEFAULT
        candidates = [(start, own)] + [
            (aid, own_levels.get(aid) or SENSITIVITY_DEFAULT) for aid in ancestors
        ]
        level = max(candidates, key=lambda item: SENSITIVITY_RANK.get(item[1], 0))[1]
        inherited = SENSITIVITY_RANK.get(level, 0) > SENSITIVITY_RANK.get(own, 0)
        chain = [
            {"id": aid, "name": names.get(aid) or aid, "sensitivity": sens}
            for aid, sens in candidates[1:]
            if sens == level
        ]
        out[start] = {
            "level": level,
            "own": own,
            "inherited": inherited,
            "incomplete": incomplete_reason is not None,
            "incomplete_reason": incomplete_reason,
            "chain": chain,
        }
    return out


def effective_sensitivity(db: Session, customer_id: str, dataset_id: str) -> dict:
    """单数据集有效敏感度（闸用；见 effective_sensitivity_bulk）。"""
    return effective_sensitivity_bulk(db, customer_id, [dataset_id])[dataset_id]
