"""萬星 A2A 回执的跨仓读契约（close-wanxing-domain-linkage 任务 6.5）。

柏讯把需求单投给萬星的 A2A 面（``POST /api/wanxing/v1/a2a/{slug}``），靠回执
字段判成败。这些字段的对端承诺在萬星仓
``contracts/wanxing-public-api.v1.json`` 的 ``$defs.turnReceipt`` 里。本测试把
**柏讯实际解析的字段**与**契约声明的字段**钉在一起：萬星改名/删除任何柏讯在
读的字段而没动契约（``x-contract-rev`` 也不 bump），这里就红，而不是等生产上
静默读空（``result.messageId`` 会静静落回请求侧 uuid，``result.parts`` 缺失则
一律映射成 protocol 错——两种都不响亮）。

不改行为：只读 ``wanxing.py`` 源码文本与契约 JSON，**不导入** ``fd_wire_api``
（该包的 app 链会拉起整站，契约测试不该被无关模块的导入顺序牵连）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

#: api/tests/test_wanxing_receipt_contract.py → api/tests → api → fd-wire 仓根。
REPO_ROOT = Path(__file__).resolve().parents[2]
#: 被钉的解析实现（字段清单的唯一依据）。
WANXING_MODULE = REPO_ROOT / "api" / "fd_wire_api" / "demands" / "wanxing.py"
CONTRACT_FILENAME = "wanxing-public-api.v1.json"

#: 柏讯读的每一个回执字段：(契约里的路径, wanxing.py 里的读取表达式)。
#:
#: 第二个元素必须逐字出现在解析代码里——它防的是「柏讯自己改了字段名、本清单
#: 还在钉旧名」这种清单漂移：漂移时下面的断言先红，不会假装契约没过。
#:
#: 路径记法：对象用 ``.``，数组里的对象用 ``[]``（``result.parts[].kind``）。
#: 全部来自 ``HttpWanxingClient.send_message`` 与 ``_error_code``：
#: - 成功回执 ``body.result``（dict 才算成功）→ ``result.parts``（必须是 list）
#:   → 每个 part 的 ``kind == "text"`` / ``text``；``result.messageId`` 如带回
#:   则优先于请求侧 uuid；
#: - 鉴权失败回体 ``body.error``（字符串或对象）→ 对象时取 ``error.code``。
RECEIPT_READS: tuple[tuple[str, str], ...] = (
    ("result", 'body.get("result")'),
    ("result.parts", 'result.get("parts")'),
    ("result.parts[].kind", 'p.get("kind") == "text"'),
    ("result.parts[].text", 'p.get("text")'),
    ("result.messageId", 'result.get("messageId")'),
    ("error", 'body.get("error")'),
    ("error.code", 'error.get("code")'),
)


#: 仓内 vendored 副本（`cp` 自 fd-wanxing/contracts，逐字节相同）。
#:
#: 为什么必须有这一份：测试读兄弟仓路径在**单仓检出**里不成立——CI（公开快照仓、
#: 任何只 clone 本仓的人）里 `../fd-wanxing` 不存在，判据要么报错要么被写成 skip，
#: 而 skip 等于判据在 CI 里消失（跨仓判据最典型的死法）。有了副本就永远可跑；
#: 兄弟仓可达时再断言两份**逐字节**相等，谁持了陈旧副本会立刻红。
VENDORED_CONTRACT = Path(__file__).resolve().parent / "contracts" / CONTRACT_FILENAME


def _sibling_candidates() -> list[Path]:
    """兄弟仓里的契约（工作区里可达时才有），用于与 vendored 副本对账。

    向上有界搜索而不是写死两级：本仓常被 checkout 成 worktree
    （`<root>/.worktrees/fd-wire-xxx`、`/tmp/fd-wire-xxx`），写死
    `parent`/`parent.parent` 时兄弟仓恰好不在那两级上，对账就会**静默跳过**——
    我第一次做红证就是因为这个假绿（注入陈旧副本，测试照样 2 passed）。
    """
    found: list[Path] = []
    here = Path(__file__).resolve().parent
    for _ in range(4):  # api/tests → api → 仓根 → 上一级 → 上两级 → 上三级
        here = here.parent
        candidate = here / "fd-wanxing" / "contracts" / CONTRACT_FILENAME
        if candidate not in found:
            found.append(candidate)
    return found


def _contract_candidates() -> list[Path]:
    """契约路径候选，按优先级：env → 仓内 vendored 副本 → 兄弟仓。"""
    candidates: list[Path] = []
    env = os.environ.get("WANXING_CONTRACTS_DIR", "").strip()
    if env:
        raw = Path(env).expanduser()
        # env 给目录（档名固定）或直接给文件，两种都认。
        candidates.append(raw if raw.suffix == ".json" else raw / CONTRACT_FILENAME)
    candidates.append(VENDORED_CONTRACT)
    candidates.extend(_sibling_candidates())
    return candidates


def _contract_path() -> Path:
    """第一个存在的契约文件；一个都没有就 fail（不 skip——skip 会让判据在 CI 里消失）。"""
    tried = _contract_candidates()
    for path in tried:
        if path.is_file():
            return path
    pytest.fail(
        "找不到萬星公开面契约（跨仓只读依赖）。尝试过：\n  "
        + "\n  ".join(str(p) for p in tried)
        + "\n可设 WANXING_CONTRACTS_DIR 指向 fd-wanxing/contracts 目录。"
    )


def _declared_paths(schema: object, prefix: str = "") -> set[str]:
    """把 schema 的 ``properties`` 展开成路径集合（数组对象的 items 用 ``[]``）。"""
    declared: set[str] = set()
    if not isinstance(schema, dict):
        return declared
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return declared
    for name, sub in properties.items():
        path = f"{prefix}.{name}" if prefix else name
        declared.add(path)
        if isinstance(sub, dict):
            declared |= _declared_paths(sub, path)
            declared |= _declared_paths(sub.get("items"), f"{path}[]")
    return declared


def _module_source() -> str:
    if not WANXING_MODULE.is_file():  # 仓结构变了：说明书级失败，不静默
        pytest.fail(f"找不到柏讯 A2A 客户端实现：{WANXING_MODULE}")
    return WANXING_MODULE.read_text(encoding="utf-8")


def test_the_vendored_contract_matches_the_owner_when_the_sibling_is_reachable() -> None:
    """兄弟仓可达时，仓内副本必须与 owner **逐字节**相同。

    形状比对会容忍正好会出事的改动（数组元素里改名、枚举值变化）；字节比对不会。
    单仓检出（CI、外部 clone）时兄弟仓不在，这条自动跳过——但上面那条判据仍然跑，
    所以"跳过"只是少了漂移检测，不是少了判据。
    """
    sibling = next((p for p in _sibling_candidates() if p.is_file()), None)
    if sibling is None or not VENDORED_CONTRACT.is_file():
        return
    assert VENDORED_CONTRACT.read_bytes() == sibling.read_bytes(), (
        f"仓内副本与 owner 不一致：{VENDORED_CONTRACT} vs {sibling}\n"
        "刷新：cp <fd-wanxing>/contracts/wanxing-public-api.v1.json "
        "api/tests/contracts/ 然后复查 x-contract-rev 是否升号。"
    )


def test_every_receipt_field_fd_wire_reads_is_declared_in_turn_receipt() -> None:
    """柏讯读的每个回执字段，萬星契约的 ``$defs.turnReceipt`` 都必须声明。"""
    source = _module_source()
    stale = [path for path, snippet in RECEIPT_READS if snippet not in source]
    assert not stale, (
        f"字段清单漂移：{WANXING_MODULE} 里已找不到这些读取表达式对应的字段——"
        f"解析代码改了读法（或本清单过期）：{sorted(stale)}。"
        "改代码的同时请同步更新本文件的 RECEIPT_READS。"
    )

    contract = _contract_path()
    document = json.loads(contract.read_text(encoding="utf-8"))
    turn_receipt = (document.get("$defs") or {}).get("turnReceipt")
    if not isinstance(turn_receipt, dict):
        pytest.fail(f"{contract} 里没有 $defs.turnReceipt（契约被改名/删除了？）")

    declared = _declared_paths(turn_receipt)
    read = {path for path, _ in RECEIPT_READS}
    missing = sorted(read - declared)

    assert not missing, (
        f"萬星 A2A 回执契约 {contract}（$defs.turnReceipt）没有声明柏讯正在读的字段：\n  "
        + "\n  ".join(missing)
        + f"\n契约目前只声明：{', '.join(sorted(declared))}\n"
        "这些字段是跨仓契约的一部分：萬星改名后柏讯不会报错，只会静默读空"
        "（result.messageId → 落回请求侧 uuid；result.parts 缺失 → 一律 protocol 错）。\n"
        "修法：在 turnReceipt.properties.result.properties 下补上这些声明"
        "（parts 是数组，items.properties 里给 kind/text），并按契约头规则 bump x-contract-rev。"
    )
