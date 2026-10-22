"""cell 数据卷初始化：空 SQLite 库 + WAL 模式（ADR-0002 v1 形态）。

平台侧**永不**读写租户 daas.db 的业务内容——本模块只在 provision 时
创建空库并落 WAL 模式（快照备份 / 拷文件迁移的文件语义前提）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_FILENAME = "daas.db"


def init_volume(volume_dir: Path) -> Path:
    """创建卷目录并初始化空 daas.db（WAL 模式持久化进文件头），返回库文件路径。"""
    volume_dir.mkdir(parents=True, exist_ok=True)
    db_path = volume_dir / DB_FILENAME
    conn = sqlite3.connect(db_path)
    try:
        # WAL 是持久属性：写入文件头，此后任何连接（cell 容器内引擎）默认 WAL
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"failed to enable WAL mode on {db_path} (got {mode!r})")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
    finally:
        conn.close()
    return db_path


def volume_initialized(volume_dir: Path) -> bool:
    """卷目录内是否已有 daas.db（pause 数据保留 / purge 前的断言用）。"""
    return (volume_dir / DB_FILENAME).is_file()
