"""管线定义解析与 DAG 校验（wire-dag-executor D1）。

``steps[]`` 每项可声明可选 ``inputs: [node_key...]``：

- ``node_key`` = 步骤名（版本内唯一；缺省名 ``step-<seq>``），保留名
  ``base`` 指本版本的基础数据集；
- 缺省（无 ``inputs``）= 前一步输出（v1 线性，逐字节既有语义——本模块对
  这类版本只做节点键推导，不做任何新增校验，存量版本零迁移）；
- 显式 ``inputs`` 可引用任意前序步骤名；空数组等价于 ``["base"]``。

校验（仅在**声明了 inputs 的版本**上执行——v1 线性链保持既有 10 步护栏
与错误文案，零行为变化）：

- 每个引用可达（引用不存在 422 指名该引用）；
- 无环（Kahn 拓扑排序失败即 422 指名环边）；
- 节点 ≤ 50、深度 ≤ 10。

深度口径：从基础数据集出发的最长节点链长度（线性三步 = 3）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 保留节点键：基础数据集
BASE_NODE = "base"

#: DAG 版本节点上限（design D1）
MAX_DAG_NODES = 50
#: DAG 深度上限（design D1；与级联深度上限同量级）
MAX_DAG_DEPTH = 10
#: v1 线性链步骤上限（既有护栏，保持逐字节不变）
MAX_LINEAR_STEPS = 10

#: 节点重试上限（design D4：0..2）
MAX_NODE_RETRIES = 2


class InvalidSteps(ValueError):
    """步骤定义不合法（创建/改版/运行时的前置校验错误）。"""


@dataclass(frozen=True)
class Node:
    """图节点：``key`` 是稳定节点键（=步骤名），``name`` 是输出命名用的步骤名。"""

    key: str
    name: str
    seq: int
    op: str
    params: dict
    retries: int
    inputs: tuple[str, ...]  # BASE_NODE 或其它节点键


@dataclass(frozen=True)
class PipelineGraph:
    """管线定义图：节点序（按 seq）+ 拓扑序 + 线性判定。"""

    nodes: tuple[Node, ...]
    order: tuple[str, ...]
    is_linear: bool
    depth: int

    def node(self, key: str) -> Node:
        for node in self.nodes:
            if node.key == key:
                return node
        raise KeyError(key)

    def by_key(self) -> dict[str, Node]:
        return {node.key: node for node in self.nodes}


def _declares_inputs(step: dict) -> bool:
    return "inputs" in step and step.get("inputs") is not None


def build_graph(steps: list[dict]) -> PipelineGraph:
    """把已通过结构校验的步骤定义组装成图（并做 DAG 校验）。

    ``steps`` 为 ``validate_steps`` 清洗后的有序定义。未声明 ``inputs`` 的
    版本走线性推导，不触发任何新增校验（v1 零漂移）。
    """
    if not steps:
        raise InvalidSteps("steps 须为非空列表（至少一个步骤）")
    has_inputs = any(_declares_inputs(step) for step in steps)
    if has_inputs and len(steps) > MAX_DAG_NODES:
        raise InvalidSteps(f"节点数超过上限 {MAX_DAG_NODES}（DAG 护栏）")

    # 节点键：步骤名；线性版本内重名时退回 step-<seq>（节点行/槽位键须唯一，
    # 存量 v1 从未校验重名，这里如实降级而非拒绝既有定义）。
    names = [str(step.get("name") or f"step-{index}") for index, step in enumerate(steps, start=1)]
    keys: list[str] = []
    if has_inputs:
        for index, name in enumerate(names, start=1):
            if name == BASE_NODE:
                raise InvalidSteps(
                    f"第 {index} 个步骤名 {BASE_NODE!r} 为保留名（指基础数据集），请改名"
                )
        if len(set(names)) != len(names):
            dup = next(n for n in names if names.count(n) > 1)
            raise InvalidSteps(f"步骤名重复：{dup!r}（DAG 版本内节点名须唯一）")
        keys = list(names)
    else:
        keys = (
            list(names)
            if len(set(names)) == len(names)
            else [f"step-{index}" for index in range(1, len(steps) + 1)]
        )

    key_set = set(keys)
    nodes: list[Node] = []
    for index, step in enumerate(steps, start=1):
        raw_inputs = step.get("inputs")
        if _declares_inputs(step):
            if not isinstance(raw_inputs, list):
                raise InvalidSteps(f"第 {index} 个步骤 inputs 须为步骤名数组")
            resolved: list[str] = []
            for ref in raw_inputs:
                if not isinstance(ref, str) or not ref.strip():
                    raise InvalidSteps(
                        f"第 {index} 个步骤 inputs 含非法引用 {ref!r}（须为非空步骤名）"
                    )
                ref = ref.strip()
                if ref == BASE_NODE:
                    if BASE_NODE not in resolved:
                        resolved.append(BASE_NODE)
                    continue
                if ref not in key_set:
                    raise InvalidSteps(
                        f"第 {index} 个步骤（{keys[index - 1]}）inputs 引用不存在的步骤名: {ref!r}"
                    )
                if ref not in resolved:
                    resolved.append(ref)
            if not resolved:
                resolved = [BASE_NODE]
        else:
            resolved = [keys[index - 2] if index > 1 else BASE_NODE]

        retries = step.get("retries")
        nodes.append(
            Node(
                key=keys[index - 1],
                name=names[index - 1],
                seq=index,
                op=str(step.get("op") or ""),
                params=dict(step.get("params") or {}),
                retries=int(retries or 0),
                inputs=tuple(resolved),
            )
        )

    # 环检测（Kahn）：入度为 0 的节点先出队；剩余节点全在环上或环下游。
    indegree = {node.key: 0 for node in nodes}
    dependents: dict[str, list[str]] = {node.key: [] for node in nodes}
    for node in nodes:
        for ref in node.inputs:
            if ref == BASE_NODE:
                continue
            indegree[node.key] += 1
            dependents[ref].append(node.key)
    queue = [key for key, degree in indegree.items() if degree == 0]
    order: list[str] = []
    while queue:
        key = queue.pop(0)
        order.append(key)
        for child in dependents[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(nodes):
        cycle = _find_cycle(nodes, set(indegree) - set(order))
        raise InvalidSteps(f"步骤定义存在环：{' → '.join(cycle)}（不可前向引用）")

    # 深度：基础数据集为 0，节点 = 输入深度 + 1（最长链）。
    depth_by_key: dict[str, int] = {}
    for key in order:
        node = next(n for n in nodes if n.key == key)
        parent_depths = [
            0 if ref == BASE_NODE else depth_by_key.get(ref, 0) for ref in node.inputs
        ]
        depth_by_key[key] = max(parent_depths, default=0) + 1
    depth = max(depth_by_key.values(), default=0)
    if has_inputs and depth > MAX_DAG_DEPTH:
        raise InvalidSteps(f"节点深度超过上限 {MAX_DAG_DEPTH}（最长链 {depth} 层）")

    return PipelineGraph(
        nodes=tuple(nodes), order=tuple(order), is_linear=not has_inputs, depth=depth
    )


def _find_cycle(nodes: list[Node], stuck: set[str]) -> list[str]:
    """在环上找一条可读路径（用于 422 指名环边）。"""
    keys = {node.key: node for node in nodes}
    start = next(iter(stuck))
    path = [start]
    seen = {start}
    current = start
    while True:
        node = keys[current]
        nxt = next(
            (
                ref
                for ref in node.inputs
                if ref != BASE_NODE and ref in stuck and ref in keys
            ),
            None,
        )
        if nxt is None:
            return path
        if nxt in seen:
            return path + [nxt]
        path.append(nxt)
        seen.add(nxt)
        current = nxt


def node_keys(graph: PipelineGraph) -> list[str]:
    """全部节点键（按 seq 序）。"""
    return [node.key for node in graph.nodes]
