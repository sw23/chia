"""Unit tests for the deterministic FindingStore (no cluster needed)."""

from __future__ import annotations

import json
import os

import pytest

from chia.analysis.mantis.finding_store import FindingStore


@pytest.fixture()
def store(tmp_path):
    s = FindingStore(str(tmp_path / "workspace"))
    s.ensure_dirs()
    return s


def _finding(**kw):
    base = {
        "title": "Missing CDC synchronizer",
        "description": "async signal sampled without a 2-flop synchronizer",
        "code_paths": ["rtl/dma.sv:145"],
        "severity": "HIGH",
        "history": [],
    }
    base.update(kw)
    return base


def test_write_assigns_id_and_reads_back(store):
    fid = store.write(_finding())
    assert fid
    assert store.list_ids() == [fid]
    got = store.read(fid)
    assert got["id"] == fid
    assert got["title"] == "Missing CDC synchronizer"


def test_write_is_atomic_and_roundtrips_json(store):
    fid = store.write(_finding(id="fixed-id"))
    assert fid == "fixed-id"
    on_disk = json.load(open(os.path.join(store.findings_dir, "fixed-id.json")))
    assert on_disk["code_paths"] == ["rtl/dma.sv:145"]


def test_set_fields_and_append_history(store):
    fid = store.write(_finding())
    store.set_fields(fid, status="VALID", reasoning="looks real")
    store.append_history(fid, "review", "validated", "passed negative filters")
    got = store.read(fid)
    assert got["status"] == "VALID"
    assert got["history"][-1] == {
        "stage": "review", "action": "validated",
        "details": "passed negative filters",
    }


def test_delete_and_missing_are_tolerant(store):
    fid = store.write(_finding())
    store.delete(fid)
    store.delete(fid)  # deleting twice must not raise
    assert store.list_ids() == []


def test_unsafe_id_rejected(store):
    with pytest.raises(ValueError):
        store.read("../../etc/passwd")


def test_read_all_skips_corrupt_files(store):
    good = store.write(_finding())
    with open(os.path.join(store.findings_dir, "broken.json"), "w") as fh:
        fh.write("{ not valid json")
    ids = {f["id"] for f in store.read_all()}
    assert ids == {good}


def test_summaries_shape(store):
    store.write(_finding(code_paths=["rtl/fsm.sv:12"], severity="MEDIUM"))
    (summary,) = store.summaries()
    assert summary["file"] == "rtl/fsm.sv"
    assert summary["line"] == "12"
    assert summary["severity"] == "MEDIUM"
    assert set(summary) == {"id", "title", "severity", "file", "line", "snippet"}


def test_plan_roundtrip_and_investigations(store):
    assert store.investigations() == []
    store.write_plan({"investigations": [{"title": "Review DMA", "target_files": ["rtl/dma.sv"]}]})
    inv = store.investigations()
    assert len(inv) == 1 and inv[0]["title"] == "Review DMA"


def test_learnings_append_read_clear(store):
    store.append_learning({"type": "trajectory_insight", "insight": "x"})
    store.append_learning({"type": "trajectory_insight", "insight": "y"})
    assert [l["insight"] for l in store.read_learnings()] == ["x", "y"]
    store.clear_learnings()
    assert store.read_learnings() == []


def test_stage_status_idempotency(store):
    a = store.write(_finding())
    b = store.write(_finding())
    assert store.stage_state(a, "review") is None
    assert set(store.ids_needing_stage("review")) == {a, b}

    store.mark_stage(a, "review")                      # default: done
    assert store.stage_state(a, "review") == "done"
    assert store.ids_needing_stage("review") == [b]    # a now skipped

    store.mark_stage(b, "review", state="skipped")
    assert store.stage_state(b, "review") == "skipped"
    # skipped is not "done", so it still counts as outstanding work
    assert store.ids_needing_stage("review") == [b]
    # stage_status persists in the finding file
    assert store.read(a)["stage_status"]["review"] == {"state": "done"}


def test_archive_iteration_moves_and_clears(store):
    store.write(_finding())
    store.write(_finding())
    dest = store.archive_iteration(1)
    assert dest and os.path.isdir(dest)
    assert len(os.listdir(dest)) == 2
    assert store.list_ids() == []           # workspace cleared
    assert store.archive_iteration(2) is None  # nothing left to archive
