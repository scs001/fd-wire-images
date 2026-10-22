"""上传解析（CSV / Excel）与保守列类型推断（契约 §3.4）。

- CSV 用 stdlib（``csv``）：原件原样存 ``stored_path``。
- Excel 用 openpyxl（read_only 流式）：转 CSV 暂存 ``stored_path``。
- 原件哈希：落盘前对客户端发来的原始字节流式算 sha256（Excel 先算
  原件再转换）——登记值可由客户侧对原件独立复算（wire-provenance-ledger）。
- 类型推断：抽样前 200 行——整列可解析 INTEGER → INTEGER；否则整列可
  解析 REAL → REAL；否则 TEXT（保守兜底）。空串一律视为缺失值。
- 扫描是流式的：不做「全量进内存」；建档 ingest 时按 2000 行分块重读。
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 哈希计算分块大小（1MiB，流式、内存有界）
_HASH_CHUNK_BYTES = 1024 * 1024

INTEGER = "INTEGER"
REAL = "REAL"
TEXT = "TEXT"

#: 类型推断抽样行数（契约 §3.4）
INFERENCE_SAMPLE_ROWS = 200
#: 预览返回的样例行数
PREVIEW_SAMPLE_ROWS = 5
#: 引擎侧 ingest 单块行数上限（契约 §2.2 单块上限 2000 行）
INGEST_CHUNK_ROWS = 2000

_INT_RE = re.compile(r"^[+-]?\d+$")
#: 列名保留前缀（契约 §1：列名保留前缀 ``__``）
RESERVED_COLUMN_PREFIX = "__"


class UploadError(ValueError):
    """上传解析/校验失败（路由层转 400，fail-closed 不落库）。"""


@dataclass
class ParsedUpload:
    """一次上传的扫描结果（全部来自 already-stored 的 CSV）。"""

    columns: list[dict[str, str]]
    row_count: int
    sample_rows: list[dict[str, Any]]
    size_bytes: int


def _clean_header(raw: Any, index: int) -> str:
    name = "" if raw is None else str(raw).strip()
    if not name:
        name = f"column_{index + 1}"
    if name.startswith(RESERVED_COLUMN_PREFIX):
        raise UploadError(f"列名不得以 {RESERVED_COLUMN_PREFIX} 开头（平台保留前缀）: {name}")
    return name


def normalize_headers(raw_headers: list[Any]) -> list[str]:
    """表头清洗：空名补位、保留前缀拒收、重名拒收（不静默改名）。"""
    headers = [_clean_header(raw, i) for i, raw in enumerate(raw_headers)]
    if not headers:
        raise UploadError("文件缺少表头行")
    seen: set[str] = set()
    for name in headers:
        if name in seen:
            raise UploadError(f"表头存在重名列: {name}")
        seen.add(name)
    return headers


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def infer_type(values: list[Any]) -> str:
    """整列保守推断：INTEGER → REAL → TEXT（全空列=TEXT）。"""
    seen = False
    int_ok = True
    real_ok = True
    for value in values:
        text = _cell_text(value)
        if text == "":
            continue
        seen = True
        if int_ok and not _INT_RE.match(text):
            int_ok = False
        if real_ok:
            try:
                float(text)
            except (TypeError, ValueError):
                real_ok = False
        if not int_ok and not real_ok:
            return TEXT
    if not seen:
        return TEXT
    if int_ok:
        return INTEGER
    if real_ok:
        return REAL
    return TEXT


def coerce_value(value: Any, column_type: str) -> Any:
    """按声明类型转换单元格；不可转换时保留原值（不静默丢数据）。"""
    text = _cell_text(value)
    if text == "":
        return None
    if column_type == INTEGER:
        try:
            return int(text)
        except (TypeError, ValueError):
            return value
    if column_type == REAL:
        try:
            return float(text)
        except (TypeError, ValueError):
            return value
    return text


def _open_csv_rows(path: Path) -> Iterator[list[str]]:
    """流式读取 CSV（utf-8-sig 容忍 BOM）；空行跳过。"""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row or (len(row) == 1 and row[0] == ""):
                continue
            yield row


def _scan_csv(path: Path, max_rows: int) -> ParsedUpload:
    rows = _open_csv_rows(path)
    try:
        headers = normalize_headers(next(rows))
    except StopIteration:
        raise UploadError("文件为空（缺少表头行）") from None
    sample_window: list[list[str]] = []
    preview: list[dict[str, Any]] = []
    count = 0
    for row in rows:
        count += 1
        if count > max_rows:
            raise UploadError(
                f"quota: 文件行数超过上限 {max_rows}（已数到第 {count} 行）——"
                "请拆分文件或申请提高配额"
            )
        if len(sample_window) < INFERENCE_SAMPLE_ROWS:
            sample_window.append(row)
        if len(preview) < PREVIEW_SAMPLE_ROWS:
            preview.append(row)
    if count == 0:
        raise UploadError("文件没有数据行")
    types = _infer_column_types(headers, sample_window)
    columns = [
        {"name": name, "type": types[i]} for i, name in enumerate(headers)
    ]
    sample_rows = [
        {
            columns[i]["name"]: coerce_value(row[i], columns[i]["type"])
            if i < len(row)
            else None
            for i in range(len(columns))
        }
        for row in preview
    ]
    return ParsedUpload(
        columns=columns,
        row_count=count,
        sample_rows=sample_rows,
        size_bytes=path.stat().st_size,
    )


def _infer_column_types(headers: list[str], window: list[list[str]]) -> list[str]:
    types: list[str] = []
    for index in range(len(headers)):
        values = [row[index] for row in window if index < len(row)]
        types.append(infer_type(values))
    return types


def _write_excel_as_csv(source: Path, target: Path, max_rows: int) -> None:
    """Excel → CSV 暂存（单遍流式写）；openpyxl 缺失/格式不支持即 400。"""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时 fail-closed
        raise UploadError("Excel 解析依赖缺失（openpyxl 未安装）") from exc
    try:
        workbook = load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl 对损坏/伪造文件抛各类异常
        raise UploadError(f"Excel 文件无法解析: {exc}") from exc
    try:
        sheet = workbook.worksheets[0]
        with target.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            count = 0
            for row in sheet.iter_rows(values_only=True):
                if count > max_rows:
                    raise UploadError(
                        f"quota: 文件行数超过上限 {max_rows}（已数到第 {count} 行）——"
                        "请拆分文件或申请提高配额"
                    )
                writer.writerow(["" if cell is None else cell for cell in row])
                count += 1
        if count == 0:
            raise UploadError("文件为空（缺少表头行）")
    finally:
        workbook.close()


def sha256_of_file(path: Path) -> str:
    """对文件字节流式计算 sha256，返回 ``sha256:<hex>``（登记口径）。

    只对**原始上传字节**调用（CSV 原件即暂存件；Excel 在转换前对 .xlsx
    原件调用）——原件哈希才可由客户侧独立复算（wire-provenance-ledger
    「不可变登记基线」）。
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def store_upload_file(
    *, source_path: Path, original_name: str, upload_dir: Path, max_rows: int
) -> tuple[Path, ParsedUpload, str]:
    """落盘 + 扫描（返回 stored_path、预览解析结果与原件 content_sha256）。

    CSV：原件原样复制（哈希=原件）；Excel（.xlsx/.xlsm）：**先对原件算
    哈希再转 CSV 暂存**（哈希≠转换后 CSV）。其余拒绝。
    """
    # 原件哈希先于任何转换/复制动作落定（Excel 转换会丢失原件字节形态）
    content_sha256 = sha256_of_file(source_path)
    suffix = Path(original_name).suffix.lower()
    upload_dir.mkdir(parents=True, exist_ok=True)
    if suffix == ".csv":
        stored = upload_dir / f"{Path(original_name).stem[:80]}-{_token()}.csv"
        stored.write_bytes(source_path.read_bytes())
    elif suffix in (".xlsx", ".xlsm"):
        stored = upload_dir / f"{Path(original_name).stem[:80]}-{_token()}.csv"
        try:
            _write_excel_as_csv(source_path, stored, max_rows)
        except UploadError:
            stored.unlink(missing_ok=True)
            raise
    else:
        raise UploadError(
            f"不支持的文件类型 {suffix or '(无扩展名)'}（支持 .csv / .xlsx / .xlsm）"
        )
    try:
        parsed = _scan_csv(stored, max_rows)
    except UploadError:
        stored.unlink(missing_ok=True)
        raise
    return stored, parsed, content_sha256


def iter_ingest_chunks(path: Path, columns: list[dict[str, str]]) -> Iterator[list[dict]]:
    """按 2000 行分块产出已按声明类型转换的行（重读暂存 CSV，内存有界）。

    首行是表头（扫描阶段已校验），绝不当数据行送入引擎。
    """
    headers = [column["name"] for column in columns]
    types = [column["type"] for column in columns]
    rows = _open_csv_rows(path)
    try:
        next(rows)  # 跳过表头
    except StopIteration:
        return
    batch: list[dict] = []
    for row in rows:
        values = {
            headers[i]: coerce_value(row[i], types[i]) if i < len(row) else None
            for i in range(len(headers))
        }
        batch.append(values)
        if len(batch) >= INGEST_CHUNK_ROWS:
            yield batch
            batch = []
    if batch:
        yield batch


def _token() -> str:
    import secrets

    return secrets.token_hex(8)