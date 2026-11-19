"""check_ontology_digests 的四态分类与遗留名单（establish-ontology-kernel 6.2 收尾）。

这个核对脚本的价值全在「不让沉默变成通过」。库里可能同时存在：

- 能复现的；
- 已知遗留——digest 由「缺键补 null」规则之前的版本铸造，**换底前后都**复现不出，
  但已经被逐行钉进 ``legacy_ontology_digests.txt``；
- 未钉的遗留——同样不可复现，但没人确认过，仍然算失败；
- 真不一致——配方被改过，或这一行被改写过。

四种里只有前两种可以算通过，而第二种之所以能通过，是因为**有人逐行确认过**。
一个全局开关会顺带放过将来任何新出现的遗留行，所以那份名单是本文件要钉的东西。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_ontology_digests.py"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_ontology_digests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _raw(manifest: dict) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
    ).hexdigest()


#: 0050 之前的实体条目：没有 tier / sensitivity 两个键。
PRE_0050 = {
    "entities": [
        {"id": 7, "name": "冒烟日期", "dataset_id": "d1", "pk_columns": ["date"]}
    ],
    "relationships": [],
}

PADDED_DIGEST = "b" * 64


def test_ok_when_recomputed_matches(checker):
    stored = "a" * 64
    assert checker.classify(PRE_0050, stored, stored) == "ok"


def test_pinned_legacy_row_is_known_not_failing(checker):
    stored = _raw(PRE_0050)
    assert checker.classify(PRE_0050, stored, PADDED_DIGEST, pinned=stored) == "legacy-known"


def test_unpinned_legacy_row_is_still_a_failure(checker):
    stored = _raw(PRE_0050)
    assert checker.classify(PRE_0050, stored, PADDED_DIGEST) == "legacy"


def test_a_pin_does_not_vouch_for_a_rewritten_row(checker):
    """同 (customer, version) 被改写后库中值变了——名单里的旧 digest 不该替它背书。"""
    rewritten = "c" * 64
    assert checker.classify(
        PRE_0050, rewritten, PADDED_DIGEST, pinned=_raw(PRE_0050)
    ) == "mismatch"


def test_mismatch_when_neither_formula_matches(checker):
    assert checker.classify(PRE_0050, "c" * 64, PADDED_DIGEST) == "mismatch"


def test_load_pins_skips_comments_and_reads_the_shipped_list(checker):
    pins = checker.load_pins(SCRIPT.with_name("legacy_ontology_digests.txt"))
    assert ("drillcust001", 1) in pins
    assert len(pins[("drillcust001", 1)]) == 64


def test_load_pins_ignores_malformed_lines(checker, tmp_path):
    path = tmp_path / "pins.txt"
    path.write_text(
        "# 注释\n\nbad-line-without-tabs\nc1\t2\tdeadbeef\treason\n", encoding="utf-8"
    )
    pins = checker.load_pins(path)
    assert pins == {("c1", 2): "deadbeef"}


def test_missing_pin_file_means_no_pins(checker, tmp_path):
    assert checker.load_pins(tmp_path / "nope.txt") == {}
