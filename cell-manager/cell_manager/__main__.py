"""CLI：``python -m cell_manager <action> <customer_id>``。

输出一律 JSON（stdout）——便于脚本化与后台集成；成功 exit 0、
失败 exit 1 且 stdout 为 ``{"ok": false, "error": {"type", "message"}}``。

用法示例（本机无 docker 的全生命周期演示）::

    export CELL_DRIVER=fake CELL_REGISTRY_MODE=fake
    export CELL_MANAGER_HOME=/tmp/cell-demo
    python -m cell_manager provision acme
    python -m cell_manager status acme

cell 授权束动作（add-customer-onboarding-automation；需
CELL_CUSTOMER_IDENTITY_MAP，铸 key 另需 CELL_REGISTRY_SECRET_KEY_FILE）::

    python -m cell_manager grant acme            # scope+条目可见性+预检映射（幂等）
    python -m cell_manager mint-key acme         # 铸客户 key：明文仅本次 stdout，
                                                 # 立即转存 0600/交付客户，勿入日志/git
    python -m cell_manager revoke-key acme <id>  # 吊一把 key（幂等）
    python -m cell_manager deprovision acme      # 退租编排：吊key→删scope→注条目→保留期
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from cell_manager import __version__
from cell_manager.errors import CellManagerError

ACTIONS = (
    "provision",
    "pause",
    "resume",
    "deprovision",
    "cancel-deprovision",
    "status",
    "purge-expired",
    "grant",
    "mint-key",
    "revoke-key",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cell_manager",
        description="柏讯企业 cell 生命周期管理（容器 + 独立 SQLite WAL 卷 + registry 条目 + cell 授权束）",
    )
    parser.add_argument("--version", action="version", version=f"cell_manager {__version__}")
    parser.add_argument("action", choices=ACTIONS, help="生命周期 / 授权束操作")
    parser.add_argument("customer_id", nargs="?", help="企业客户标识（purge-expired 可省略）")
    parser.add_argument(
        "key_id", nargs="?", help="key 标识（仅 revoke-key：要吊销的 patch key id）"
    )
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    # 延迟 import：argparse 错误（--help/--version）不需要构造管理面
    from cell_manager import default_grant_manager, default_manager
    from cell_manager.grant import decommission

    action = args.action
    if action == "purge-expired":
        manager = default_manager()
        purged = manager.purge_expired()
        return {
            "ok": True,
            "action": action,
            "purged": [cell.to_dict() for cell in purged],
        }
    if not args.customer_id:
        raise SystemExit(f"error: action {action!r} requires a customer_id")

    customer_id = args.customer_id
    if action in ("grant", "mint-key", "revoke-key"):
        grant_face = default_grant_manager()
        if action == "grant":
            detail = grant_face.grant(customer_id)
        elif action == "mint-key":
            detail = grant_face.mint_customer_key(customer_id)
        else:
            if not args.key_id:
                raise SystemExit("error: action 'revoke-key' requires a key_id")
            detail = grant_face.revoke_customer_key(customer_id, args.key_id)
        detail["ok"] = True
        detail["action"] = action
        return detail

    manager = default_manager()
    if action == "deprovision":
        # 退租编排（D6）：吊 key → 删 scope → 注销条目 → 生命周期 deprovision
        grant_face = default_grant_manager()
        payload = decommission(manager, grant_face, customer_id)
        payload["action"] = action
        return payload
    if action == "provision":
        result: Any = manager.provision(customer_id)
    elif action == "pause":
        result = manager.pause(customer_id)
    elif action == "resume":
        result = manager.resume(customer_id)
    elif action == "cancel-deprovision":
        result = manager.cancel_deprovision(customer_id)
    else:
        result = manager.status(customer_id)
    payload = result.to_dict()
    payload["ok"] = True
    payload["action"] = action
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _dispatch(args)
    except CellManagerError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": args.action,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
