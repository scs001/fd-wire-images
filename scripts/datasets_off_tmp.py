#!/usr/bin/env python3
"""datasets 库与上传目录迁出 /tmp 的备份/恢复工具（wire-provenance-ledger D8.1）。

自包含：仅 Python 标准库（sqlite3 / tarfile / hashlib / argparse），可
直接 `kubectl cp` 进 pod 内执行——不依赖镜像里预装任何东西。

背景：datasets 模块的 sqlite 库与上传原件暂存目录原落 /tmp（容器临时层，
pod 重建即丢登记与原件）。迁移 = 旧 pod 上 backup 导出 → 新清单（PVC）
上线 → 挂 PVC 的 pod 内 restore 一次性灌入；步骤细节见 deploy/README.md
「datasets 迁出 /tmp」。

备份目录布局（``backup`` 产出，``restore`` 消费）::

    <out>/db.sqlite          # 经 sqlite backup API 复制的库文件
                             #   （在线安全：WAL/进行中事务不丢、不撕裂）
    <out>/uploads.tar.gz     # 上传原件目录（相对路径 tar，gzip）
    <out>/MANIFEST.json      # 哈希与元数据（restore 先验后写，fail-closed）

用法::

    # 1) 备份（在旧 pod 上，路径按旧 env 取值）
    python3 datasets_off_tmp.py backup \\
        --db /tmp/fd_wire.db --uploads /tmp/fd_wire_dataset_uploads \\
        --out /tmp/datasets_off_tmp_backup

    # 2) 恢复（在新 pod 上——挂 PVC，路径按新 env 取值；幂等可重跑）
    python3 datasets_off_tmp.py restore \\
        --backup-dir /tmp/datasets_off_tmp_backup \\
        --db /var/lib/fd-wire/db/fd_wire_datasets.db \\
        --uploads /var/lib/fd-wire/uploads

    # 3) 演练自测（本地临时目录做一轮 backup → restore → 比对）
    python3 datasets_off_tmp.py --selftest

恢复语义：库文件经「同目录临时文件 + os.replace」原子落位（覆盖目标处
已有空库/半库）；上传目录按相对路径解包覆盖（同名文件以备份为准，重复
执行结果一致——幂等）。恢复完成后建议 `PRAGMA integrity_check`（脚本已
自动执行 quick_check，非 ok 即失败退出）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "MANIFEST.json"
DB_NAME = "db.sqlite"
UPLOADS_NAME = "uploads.tar.gz"
#: 备份清单格式版本（前向兼容：restore 对未知版本 fail-closed 拒绝）
MANIFEST_VERSION = 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _die(message: str) -> "None":
    print(f"错误: {message}", file=sys.stderr)
    raise SystemExit(2)


# ------------------------------------------------------------------ backup


def cmd_backup(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    uploads_dir = Path(args.uploads)
    out_dir = Path(args.out)
    if not db_path.exists():
        _die(f"库文件不存在: {db_path}（确认 --db 指向旧 env 的实际路径）")
    if not uploads_dir.is_dir():
        _die(f"上传目录不存在: {uploads_dir}（确认 --uploads 指向旧 env 的实际路径）")
    if out_dir.exists() and any(out_dir.iterdir()):
        _die(f"输出目录非空: {out_dir}（备份目录须为空或不存在，避免混入旧备份）")
    out_dir.mkdir(parents=True, exist_ok=True)

    # sqlite backup API：对在线库做页级复制——比 cp 文件安全（不撕裂
    # WAL/进行中事务），旧 pod 不用停服即可导出一致性快照。
    db_copy = out_dir / DB_NAME
    with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(db_copy)) as target:
        source.backup(target)

    uploads_tar = out_dir / UPLOADS_NAME
    with tarfile.open(uploads_tar, "w:gz") as tar:
        # arcname="."：解包即得到上传目录内容本身（相对路径）
        tar.add(str(uploads_dir), arcname=".")

    manifest = {
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "db": {
            "source": str(db_path),
            "sha256": _sha256_file(db_copy),
            "size_bytes": db_copy.stat().st_size,
            "integrity_quick_check": _quick_check(db_copy),
        },
        "uploads": {
            "source": str(uploads_dir),
            "sha256": _sha256_file(uploads_tar),
            "size_bytes": uploads_tar.stat().st_size,
            "file_count": sum(1 for p in uploads_dir.rglob("*") if p.is_file()),
        },
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"备份完成 → {out_dir}\n"
        f"  库: {manifest['db']['size_bytes']} 字节"
        f"（quick_check={manifest['db']['integrity_quick_check']}）\n"
        f"  上传原件: {manifest['uploads']['file_count']} 个文件"
    )


def _quick_check(db_path: Path) -> str:
    with sqlite3.connect(str(db_path)) as conn:
        return str(conn.execute("PRAGMA quick_check").fetchone()[0])


# ------------------------------------------------------------------ restore


def cmd_restore(args: argparse.Namespace) -> None:
    backup_dir = Path(args.backup_dir)
    db_target = Path(args.db)
    uploads_target = Path(args.uploads)
    manifest_path = backup_dir / MANIFEST_NAME
    db_src = backup_dir / DB_NAME
    tar_src = backup_dir / UPLOADS_NAME
    for required in (manifest_path, db_src, tar_src):
        if not required.exists():
            _die(f"备份件缺失: {required}（确认 --backup-dir 指向完整备份目录）")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != MANIFEST_VERSION:
        _die(f"备份清单版本不支持: {manifest.get('version')}（期望 {MANIFEST_VERSION}）")
    # 先验后写（fail-closed）：搬运途中的损坏在此拦下，不污染目标卷
    for label, path, expected in (
        ("库", db_src, manifest["db"]["sha256"]),
        ("上传原件包", tar_src, manifest["uploads"]["sha256"]),
    ):
        actual = _sha256_file(path)
        if actual != expected:
            _die(f"{label}备份件哈希不符（期望 {expected}，实际 {actual}）——备份损坏，中止恢复")

    # 1) 库文件原子落位：同目录临时文件写好后 os.replace（幂等：重复
    #    restore 得到同一份库；中断不留半库在目标名上）
    db_target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(db_target.parent), prefix=".restore-", suffix=".sqlite"
    )
    os.close(fd)
    try:
        shutil.copyfile(db_src, tmp_name)
        os.replace(tmp_name, db_target)
    finally:
        Path(tmp_name).unlink(missing_ok=True)

    check = _quick_check(db_target)
    if check != "ok":
        _die(f"恢复后库完整性校验失败: quick_check={check}——保留备份，人工排查")

    # 2) 上传目录解包覆盖（幂等：tar 解包同名覆盖，重复执行结果一致）
    uploads_target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_src, "r:gz") as tar:
        # 成员已限上传目录内部（backup 时 arcname="."）；仍做一层防穿越
        for member in tar.getmembers():
            if not member.name.rstrip("/") in (".", "") and (
                member.name.startswith("/")
                or ".." in Path(member.name).parts
            ):
                _die(f"备份包内出现可疑路径: {member.name}——中止恢复")
        tar.extractall(str(uploads_target), filter="data")

    restored = sum(1 for p in uploads_target.rglob("*") if p.is_file())
    print(
        f"恢复完成:\n"
        f"  库 → {db_target}（quick_check=ok）\n"
        f"  上传原件 → {uploads_target}（现 {restored} 个文件）\n"
        f"建议随后重启 api（kubectl -n fd-prod rollout restart deploy/fd-wire-api），"
        f"让进程重开新库文件句柄。"
    )


# ------------------------------------------------------------------ selftest


def cmd_selftest() -> None:
    """临时目录里做一轮 backup → restore → 逐字节比对（不改任何真实路径）。"""
    with tempfile.TemporaryDirectory(prefix="datasets_off_tmp_selftest_") as tmp:
        root = Path(tmp)
        # 造源：一个带数据/WAL 的库 + 一个多文件上传目录
        src_db = root / "old" / "fd_wire.db"
        src_db.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(src_db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(100)]
        )
        conn.commit()
        conn.close()
        src_uploads = root / "old" / "uploads" / "a" / "b"
        src_uploads.mkdir(parents=True)
        (src_uploads / "f1.csv").write_bytes(b"id\n1\n2\n")
        (src_uploads / "f2.csv").write_bytes(b"x" * 100_000)

        backup_dir = root / "backup"
        cmd_backup(
            argparse.Namespace(db=str(src_db), uploads=str(src_db.parent / "uploads"),
                               out=str(backup_dir))
        )
        # 目标处预置一个「新库已自动建表」的空库与一个同名异物文件——restore 必须
        # 整库覆盖且同名文件以备份为准（幂等语义）
        dst_db = root / "new" / "db" / "fd_wire_datasets.db"
        dst_db.parent.mkdir(parents=True)
        sqlite3.connect(str(dst_db)).execute("CREATE TABLE preexisting (x)").connection.close()
        dst_uploads = root / "new" / "uploads"
        dst_uploads.mkdir(parents=True)
        (dst_uploads / "stale.txt").write_text("should be gone? no—restore 只覆盖同名")

        cmd_restore(
            argparse.Namespace(backup_dir=str(backup_dir), db=str(dst_db),
                               uploads=str(dst_uploads))
        )

        conn = sqlite3.connect(str(dst_db))
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert count == 100, f"恢复后行数不符: {count}"
        assert (dst_uploads / "a" / "b" / "f1.csv").read_bytes() == b"id\n1\n2\n"
        assert (dst_uploads / "a" / "b" / "f2.csv").read_bytes() == b"x" * 100_000
        # 幂等：再跑一遍 restore，结果不变
        cmd_restore(
            argparse.Namespace(backup_dir=str(backup_dir), db=str(dst_db),
                               uploads=str(dst_uploads))
        )
        conn = sqlite3.connect(str(dst_db))
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100
        conn.close()
    print("selftest 通过：backup → restore → 比对 → 二次 restore（幂等）全部一致")


# ------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datasets_off_tmp.py",
        description=(
            "datasets 库与上传目录迁出 /tmp 的备份/恢复（wire-provenance-ledger "
            "D8.1；仅标准库，可 kubectl cp 进 pod 执行；步骤见 deploy/README.md）"
        ),
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="本地临时目录自测：backup → restore → 逐字节/逐行比对 → 幂等复跑",
    )
    sub = parser.add_subparsers(dest="command")

    p_backup = sub.add_parser(
        "backup", help="备份：sqlite backup API 复制库 + tar 打包上传目录 + MANIFEST"
    )
    p_backup.add_argument("--db", required=True, help="旧 datasets sqlite 库路径（如 /tmp/fd_wire.db）")
    p_backup.add_argument(
        "--uploads", required=True, help="旧上传目录（如 /tmp/fd_wire_dataset_uploads）"
    )
    p_backup.add_argument("--out", required=True, help="备份输出目录（须为空或不存在）")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser(
        "restore",
        help="恢复：先验 MANIFEST 哈希，库原子落位 + 上传目录解包（幂等可重跑）",
    )
    p_restore.add_argument("--backup-dir", required=True, help="backup 产出的备份目录")
    p_restore.add_argument(
        "--db", required=True, help="目标库路径（新 env，如 /var/lib/fd-wire/db/fd_wire_datasets.db）"
    )
    p_restore.add_argument(
        "--uploads", required=True, help="目标上传目录（新 env，如 /var/lib/fd-wire/uploads）"
    )
    p_restore.set_defaults(func=cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        cmd_selftest()
        return 0
    if args.command is None:
        parser.print_help()
        return 1
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
