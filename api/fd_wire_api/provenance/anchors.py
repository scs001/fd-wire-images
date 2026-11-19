"""可插拔锚定后端（wire-provenance-ledger D5，任务 5.2/5.3/5.4）。

统一接口（``AnchorBackend`` 协议，配置驱动实例化）：

- ``name: str``——与 ``settings.anchor_backend`` 的配置名一致；
- ``submit(ledger, root, meta) -> dict``——提交 Merkle 根，返回回执字段
  ``{backend, external_ref, ts_external, ...}``（额外字段并入回执 payload）。
  **幂等可重试**：对同一 (ledger, root, epoch) 重复提交安全（区间根由账本
  内容确定性导出，崩溃后重封存重提交不产生半状态）；
- ``verify_local(receipt_payload) -> {"status", "detail"}``——离线/本地核验，
  不强制联网。status 取值：``verified``（本地证据确凿通过）、``pending``
  （尚不能定论——OTS 待 BTC 确认/缺链上情报）、``unsupported``（本后端
  不支持本地核验）、``failed``（本地证据确凿不通过——签名不符/根不匹配/
  凭据损坏）。

v1 实装两个零商务依赖后端：

- ``ots``——OpenTimestamps 公链锚定（回执 = base64(OTS 凭据字节)）；
- ``customer``——客户自持签名检查点（ed25519，自包含公钥，离线可验）。

``consortium`` / ``rfc3161`` 仅注册实现位（商务接入后即插，v1.5），
``none`` 为 chain_only 观察期的防御性空实现。

链上零明文红线：所有后端提交链上的外部内容仅含哈希、序号、时间与
最小元数据——绝无业务 payload 值、列名或可反推内容的值（D5）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Protocol

from opentimestamps.calendar import DEFAULT_AGGREGATORS, RemoteCalendar
from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    LitecoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    BytesSerializationContext,
    UnsupportedMajorVersion,
)
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from . import envelope
from .settings import get_settings

logger = logging.getLogger(__name__)

#: OTS 单 calendar 提交超时（秒）——测试 monkeypatch 网络时不受影响
OTS_SUBMIT_TIMEOUT = 10.0


class AnchorBackend(Protocol):
    """锚定后端协议（D5：配置驱动实例化，档位与 deployment profile 共同决定）。"""

    name: str

    def submit(self, ledger: str, root: str, meta: dict) -> dict:
        """提交区间根，返回回执字段（幂等可重试）。失败抛异常，由上层不封存。"""
        ...  # pragma: no cover

    def verify_local(self, receipt_payload: dict) -> dict:
        """离线/本地核验回执 payload（不强制联网）。"""
        ...  # pragma: no cover


# --------------------------------------------------------------- none（留位）

class NullBackend:
    """chain_only 观察期占位：不锚定、不封存。

    ``seal_ledger`` 在 backend=none 时本就不该被调到（封存循环空转）；
    被调到即配置错位，防御性抛错而不是静默"成功"。
    """

    name = "none"

    def submit(self, ledger: str, root: str, meta: dict) -> dict:
        raise RuntimeError(
            "anchor_backend=none（chain_only）不锚定：如需封存，请将"
            " FD_WIRE_PROVENANCE_ANCHOR_BACKEND 配置为 ots/customer 等后端"
        )

    def verify_local(self, receipt_payload: dict) -> dict:
        return {
            "status": "unsupported",
            "detail": "none 后端不产生锚定回执，无本地核验语义",
        }


#: consortium/rfc3161 实现位（任务 5.2：接口在案，商务接入后即插，v1.5）
class _PlaceholderBackend:
    """留位后端：配置面/接口面就绪，submit 显式 NotImplementedError。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def submit(self, ledger: str, root: str, meta: dict) -> dict:
        raise NotImplementedError(
            f"anchor-{self.name} 商务接入后即插（v1.5）：配置已就绪，实现待接入"
        )

    def verify_local(self, receipt_payload: dict) -> dict:
        return {
            "status": "unsupported",
            "detail": f"anchor-{self.name} 尚未接入（v1.5），无凭据可核验",
        }


null_backend = NullBackend()
consortium_backend = _PlaceholderBackend("consortium")
rfc3161_backend = _PlaceholderBackend("rfc3161")


# --------------------------------------------------------------- customer（5.4）

#: 账本名进文件路径的白名单（越界字符折叠为 "_"，防路径穿越）
_PATH_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_dir_name(ledger: str) -> str:
    name = _PATH_SAFE.sub("_", ledger).strip("._") or "ledger"
    return name[:64]


class CustomerBackend:
    """客户自持检查点（anchor-customer，D5/任务 5.4）。

    对 ``{ledger, epoch, first_seq, last_seq, merkle_root, ts}`` 的规范化
    JSON 做 ed25519 签名，落自包含检查点文件（内嵌公钥）到
    ``settings.checkpoint_dir/<ledger>/epoch-<N>.json``；客户下载后离线
    保存即构成外部信任锚——平台此后无法不被察觉地改写该区间。

    签名钥匙：生产经 OpenBAO 交付至 ``signing_key_path``（PEM 私钥）；
    未配置路径时自动生成进程内一次性密钥并明示"仅测试"（跨进程不可验签）。
    """

    name = "customer"

    def _key(self) -> tuple[object, str, str]:
        """取签名钥（缓存 per path；ephemeral 键进程内恒定）。

        返回 ``(私钥对象, 公钥 PEM 文本, key_id)``；key_id = 公钥原始
        32 字节的 sha256 前 16 hex（钥匙轮换时新钥新 id，检查点可辨）。
        """
        from cryptography.hazmat.primitives import serialization

        cached = _KEY_CACHE.get(self._key_path)
        if cached is not None:
            return cached
        priv, pub_pem = self._load_or_create_key()
        pub_raw = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        key_id = "ed25519-" + hashlib.sha256(pub_raw).hexdigest()[:16]
        _KEY_CACHE[self._key_path] = (priv, pub_pem, key_id)
        return priv, pub_pem, key_id

    @property
    def _key_path(self) -> str:
        return get_settings().signing_key_path

    @staticmethod
    def _load_or_create_key() -> tuple[object, str]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = get_settings().signing_key_path
        if not path:
            priv = Ed25519PrivateKey.generate()
            logger.warning(
                "未配置 FD_WIRE_PROVENANCE_SIGNING_KEY_PATH：使用进程内一次性"
                " ed25519 密钥（仅测试——重启后签出的检查点无法用旧公钥验签）"
            )
        else:
            pem_file = Path(path)
            if pem_file.exists():
                priv = serialization.load_pem_private_key(
                    pem_file.read_bytes(), password=None
                )
            else:
                priv = Ed25519PrivateKey.generate()
                pem_file.parent.mkdir(parents=True, exist_ok=True)
                pem_file.write_bytes(
                    priv.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
                logger.warning("签名钥匙不存在，已在 %s 生成新 ed25519 私钥", path)
        pub_pem = (
            priv.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )
        return priv, pub_pem

    def checkpoint_path(self, ledger: str, epoch: int) -> Path:
        """检查点文件绝对路径：``<checkpoint_dir>/<ledger>/epoch-<N>.json``。"""
        return (
            Path(get_settings().checkpoint_dir)
            / _safe_dir_name(ledger)
            / f"epoch-{epoch}.json"
        )

    def submit(self, ledger: str, root: str, meta: dict) -> dict:
        """签名并落检查点文件；``external_ref`` = 相对 checkpoint_dir 的路径。

        签名覆盖**整个检查点去掉 signature 字段**（含 key_id/pubKeyPem——
        与 evidence_verify.py 的 pop("signature") 后整体验签口径逐字一致，
        公钥/钥标识被换即签名失效）。幂等：同 (ledger, epoch) 的检查点内容
        由区间根确定性导出，重复提交重写等价文件（原子替换）；账本
        append-only 保证同 epoch 不会换根。
        """
        priv, pub_pem, key_id = self._key()
        signed = {
            "ledger": ledger,
            "epoch": meta["epoch"],
            "first_seq": meta["first_seq"],
            "last_seq": meta["last_seq"],
            "merkle_root": root,
            "ts": meta["ts"],
            "key_id": key_id,
            "pubKeyPem": pub_pem,
        }
        signature = priv.sign(envelope.canonical_json(signed)).hex()
        checkpoint = {**signed, "signature": signature}

        target = self.checkpoint_path(ledger, meta["epoch"])
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
        logger.info(
            "客户检查点落盘 ledger=%s epoch=%s root=%s… → %s",
            ledger, meta["epoch"], root[:16], target,
        )
        rel = target.relative_to(Path(get_settings().checkpoint_dir)).as_posix()
        return {"backend": self.name, "external_ref": rel, "ts_external": meta["ts"]}

    def verify_local(self, receipt_payload: dict) -> dict:
        """离线核验：重验检查点 ed25519 签名 + 与回执 payload 的根/epoch 对账。"""
        ref = receipt_payload.get("external_ref")
        root = receipt_payload.get("merkle_root")
        epoch = receipt_payload.get("epoch")
        if not ref or not root:
            return {"status": "failed", "detail": "回执缺 external_ref/merkle_root"}
        path = Path(get_settings().checkpoint_dir) / ref
        if not path.is_file():
            return {"status": "failed", "detail": f"检查点文件缺失：{ref}"}
        try:
            cp = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"status": "failed", "detail": f"检查点文件不可读/非 JSON：{exc}"}
        if cp.get("merkle_root") != root or cp.get("epoch") != epoch:
            return {
                "status": "failed",
                "detail": "检查点与回执不匹配（merkle_root/epoch 对不上——回执冒充或错配）",
            }
        signature = cp.pop("signature", "")
        pub_pem = cp.get("pubKeyPem")
        if not signature or not pub_pem:
            return {"status": "failed", "detail": "检查点缺 signature/pubKeyPem"}
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            pub = serialization.load_pem_public_key(pub_pem.encode("utf-8"))
            assert isinstance(pub, Ed25519PublicKey)
            pub.verify(bytes.fromhex(signature), envelope.canonical_json(cp))
        except Exception as exc:  # 验签失败/公钥损坏/十六进制非法
            return {"status": "failed", "detail": f"ed25519 验签失败：{exc}"}
        return {
            "status": "verified",
            "detail": f"检查点签名有效（epoch={epoch}, key_id={cp.get('key_id')}）",
        }


#: （path → (私钥, 公钥 PEM, key_id)）进程内缓存；ephemeral 键以 "" 为键
_KEY_CACHE: dict[str, tuple] = {}


customer_backend = CustomerBackend()


# --------------------------------------------------------------- ots（5.3）

class OtsBackend:
    """OpenTimestamps 公链锚定（anchor-ots，D5/任务 5.3）。

    ``submit``：对 merkle_root 的 32 字节 digest 调 public calendars
    （``FD_WIRE_PROVENANCE_OTS_CALENDARS`` 逗号分隔可覆盖，默认官方聚合池），
    合并各 calendar 返回的 pending 凭据为一份 OTS DetachedTimestampFile，
    ``external_ref = base64(OTS 凭据字节)``。任一 calendar 成功即成功；
    全部失败抛异常（上层本轮不封存、下轮重试——重试对 calendar 幂等）。

    BTC 确认分钟到小时级异步补齐：回执先为 pending，凭据升级（calendar
    追加 BitcoinBlockHeaderAttestation）后可核验——滞后如实可见，不假装实时。
    """

    name = "ots"

    def _calendar_urls(self) -> tuple[str, ...]:
        override = get_settings().ots_calendars.strip()
        if override:
            return tuple(u.strip() for u in override.split(",") if u.strip())
        return tuple(DEFAULT_AGGREGATORS)

    def submit(self, ledger: str, root: str, meta: dict) -> dict:
        digest = bytes.fromhex(root)  # merkle_root hex = 32 字节摘要
        if len(digest) != 32:
            raise ValueError(f"merkle_root 非 32 字节摘要：{root!r}")
        stamp = Timestamp(digest)
        errors: list[str] = []
        submitted = 0
        for url in self._calendar_urls():
            try:
                calendar = RemoteCalendar(url)
                remote = calendar.submit(stamp.msg, timeout=OTS_SUBMIT_TIMEOUT)
                stamp.merge(remote)
                submitted += 1
            except Exception as exc:  # 网络/超时/协议错：记录后试下一个
                errors.append(f"{url}: {exc}")
        if not submitted:
            raise RuntimeError(
                f"OTS 全部 calendar 提交失败（本轮不封存，下轮重试）：{errors}"
            )
        if errors:
            logger.warning("OTS 部分 calendar 提交失败（已有 %d 成功）：%s", submitted, errors)
        ctx = BytesSerializationContext()
        DetachedTimestampFile(OpSHA256(), stamp).serialize(ctx)
        ots_b64 = base64.b64encode(ctx.getbytes()).decode("ascii")
        return {
            "backend": self.name,
            "external_ref": ots_b64,
            "ts_external": envelope.utc_now_iso_z(),
        }

    def verify_local(self, receipt_payload: dict) -> dict:
        """离线核验：解析凭据、对账 digest==merkle_root、盘点证明状态。

        不联网：仅含 PendingAttestation → "pending"（如实）；已含 BTC/LTC
        区块确认但本地无链上情报 → 亦 "pending"（凭据结构有效，终局核验
        需公开链数据，见 evidence_verify 的 anchors/ 复核口径）。
        """
        ref = receipt_payload.get("external_ref")
        root = receipt_payload.get("merkle_root")
        if not ref or not root:
            return {"status": "failed", "detail": "回执缺 external_ref/merkle_root"}
        try:
            ots_bytes = base64.b64decode(ref, validate=True)
            detached = DetachedTimestampFile.deserialize(
                BytesDeserializationContext(ots_bytes)
            )
        except UnsupportedMajorVersion as exc:
            return {"status": "unsupported", "detail": f"OTS 凭据版本不支持：{exc}"}
        except Exception as exc:
            return {"status": "failed", "detail": f"OTS 凭据无法解析：{exc}"}
        if detached.file_digest != bytes.fromhex(root):
            return {
                "status": "failed",
                "detail": "OTS 凭据承诺的摘要与回执 merkle_root 不符（回执冒充或错配）",
            }
        attestations = [a for _, a in detached.timestamp.all_attestations()]
        if not attestations:
            return {"status": "failed", "detail": "OTS 凭据不含任何证明"}
        confirmed = [
            a
            for a in attestations
            if isinstance(a, (BitcoinBlockHeaderAttestation, LitecoinBlockHeaderAttestation))
        ]
        if confirmed:
            return {
                "status": "pending",
                "detail": (
                    f"含 {len(confirmed)} 个区块确认，离线无链上情报无法终局核验"
                    "（需 BTC/LTC 公开链数据；凭据结构与摘要对账已通过）"
                ),
            }
        pending = [a for a in attestations if isinstance(a, PendingAttestation)]
        return {
            "status": "pending",
            "detail": (
                f"仅含 {len(pending)} 个 pending 证明（BTC 确认分钟~小时级异步补齐），"
                "凭据结构与摘要对账已通过"
            ),
        }


ots_backend = OtsBackend()


# --------------------------------------------------------------- 工厂

_BACKENDS: dict[str, AnchorBackend] = {
    "none": null_backend,
    "ots": ots_backend,
    "customer": customer_backend,
    "consortium": consortium_backend,
    "rfc3161": rfc3161_backend,
}


def get_backend(name: str) -> AnchorBackend:
    """配置名 → 后端实例；未知名以配置错误风格清晰报错（含合法名清单）。"""
    try:
        return _BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"未知锚定后端配置：{name!r}（合法值：{'/'.join(sorted(_BACKENDS))}；"
            "检查 FD_WIRE_PROVENANCE_ANCHOR_BACKEND）"
        ) from None
