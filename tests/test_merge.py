"""Tests for the three-way merge of independently corrected cleaned tables."""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import threading

import pytest

from app import create_app, db, merge
from app.services import ServiceError


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "stability.sqlite3"),
            "STORAGE_DIR": str(tmp_path / "objects"),
            "STAGING_DIR": str(tmp_path / "staging"),
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def _upload(client, kind, content, filename="m.csv", parent_ids=""):
    data = {
        "kind": kind,
        "file": (io.BytesIO(content.encode()), filename),
        "parent_ids": parent_ids,
    }
    return client.post(
        "/upload", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def _id_from_flash(resp) -> str:
    body = resp.get_data(as_text=True)
    match = re.search(r"(RAW|CLN|SUM)-\d{4}", body)
    assert match, body
    return match.group(0)


BASE_CSV = "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.0,ok\nR3,B3,12.0,ok\n"


def _make_trio(client, left_csv, right_csv, base_csv=BASE_CSV):
    """Register raw -> base cleaned -> left/right cleaned; return the ids."""
    raw_id = _id_from_flash(_upload(client, "raw", "batch,value\nB1,1\n", "raw.csv"))
    base_id = _id_from_flash(
        _upload(client, "cleaned", base_csv, "base.csv", parent_ids=raw_id)
    )
    left_id = _id_from_flash(
        _upload(client, "cleaned", left_csv, "left.csv", parent_ids=base_id)
    )
    right_id = _id_from_flash(
        _upload(client, "cleaned", right_csv, "right.csv", parent_ids=base_id)
    )
    return raw_id, base_id, left_id, right_id


def _create(app, base_id, left_id, right_id, key="记录号"):
    with app.app_context():
        return merge.create_merge_draft(base_id, left_id, right_id, key)


def _draft(app, draft_id):
    with app.app_context():
        conn = db.get_db()
        return db.get_merge_draft(conn, draft_id)


def _save(app, draft_id, revision, choices):
    with app.app_context():
        return merge.save_merge_choices(draft_id, revision, choices)


def _confirm(app, draft_id, revision, choices=None):
    with app.app_context():
        return merge.confirm_merge(draft_id, revision, choices or {})


def _download_text(client, file_id):
    resp = client.get(f"/files/{file_id}/download")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 1. Draft creation validation
# ---------------------------------------------------------------------------

def test_create_draft_happy_path(app, client):
    _, base_id, left_id, right_id = _make_trio(
        client,
        "记录号,批号,含量,备注\nR1,B1,10.6,ok\nR2,B2,11.0,ok\nR3,B3,12.0,ok\n",
        "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.0,ok\nR3,B3,12.5,ok\n",
    )
    draft_id = _create(app, base_id, left_id, right_id)
    assert draft_id == "MRG-0001"
    draft = _draft(app, draft_id)
    assert draft["status"] == "open" and draft["revision"] == 0
    state = json.loads(draft["state_json"])
    assert state["columns"] == ["记录号", "批号", "含量", "备注"]
    assert state["conflicts"] == []  # one-sided changes only -> fully automatic


def test_create_requires_distinct_sides(app, client):
    _, base_id, left_id, _ = _make_trio(client, BASE_CSV, BASE_CSV)
    with pytest.raises(ServiceError, match="不能是同一份"):
        _create(app, base_id, left_id, left_id)


def test_create_requires_cleaned_kind(app, client):
    raw_id = _id_from_flash(_upload(client, "raw", "a\n1\n", "r.csv"))
    _, base_id, left_id, right_id = _make_trio(client, BASE_CSV, BASE_CSV)
    with pytest.raises(ServiceError, match="不是清洗表"):
        _create(app, raw_id, left_id, right_id)
    with pytest.raises(ServiceError, match="不是清洗表"):
        _create(app, base_id, raw_id, right_id)


def test_create_requires_sole_parentage(app, client):
    raw_id = _id_from_flash(_upload(client, "raw", "a\n1\n", "r.csv"))
    base_id = _id_from_flash(
        _upload(client, "cleaned", BASE_CSV, "base.csv", parent_ids=raw_id))
    left_id = _id_from_flash(
        _upload(client, "cleaned", BASE_CSV, "l.csv", parent_ids=base_id))
    # Right table has an extra parent besides the base.
    right_id = _id_from_flash(
        _upload(client, "cleaned", BASE_CSV, "r.csv",
                parent_ids=f"{base_id} {raw_id}"))
    with pytest.raises(ServiceError, match="仅以基表"):
        _create(app, base_id, left_id, right_id)
    # A table derived from something else entirely is also rejected.
    other_id = _id_from_flash(
        _upload(client, "cleaned", BASE_CSV, "o.csv", parent_ids=raw_id))
    with pytest.raises(ServiceError, match="仅以基表"):
        _create(app, base_id, left_id, other_id)


def test_create_rejects_unavailable_source(app, client):
    _, base_id, left_id, right_id = _make_trio(client, BASE_CSV, BASE_CSV)
    client.post(f"/files/{left_id}/revoke", data={"reason": "bad"})
    with pytest.raises(ServiceError, match="已被撤销"):
        _create(app, base_id, left_id, right_id)


def test_create_rejects_column_mismatch(app, client):
    _, base_id, left_id, _ = _make_trio(client, BASE_CSV, BASE_CSV)
    other = _id_from_flash(_upload(
        client, "cleaned", "记录号,批号,含量\nR1,B1,10.5\n", "x.csv",
        parent_ids=base_id))
    with pytest.raises(ServiceError, match="列名不一致"):
        _create(app, base_id, left_id, other)


def test_create_rejects_over_500_rows(app, client):
    header = "记录号,批号,含量,备注\n"
    big = header + "".join(f"R{i},B{i},1.0,x\n" for i in range(501))
    raw_id = _id_from_flash(_upload(client, "raw", "a\n1\n", "r.csv"))
    base_id = _id_from_flash(
        _upload(client, "cleaned", BASE_CSV, "base.csv", parent_ids=raw_id))
    left_id = _id_from_flash(
        _upload(client, "cleaned", big, "big.csv", parent_ids=base_id))
    right_id = _id_from_flash(
        _upload(client, "cleaned", BASE_CSV, "r.csv", parent_ids=base_id))
    with pytest.raises(ServiceError, match="500"):
        _create(app, base_id, left_id, right_id)


def test_create_rejects_duplicate_and_empty_keys(app, client):
    dup = "记录号,批号,含量,备注\nR1,B1,1,x\nR1,B2,2,y\n"
    empty = "记录号,批号,含量,备注\nR1,B1,1,x\n,B2,2,y\n"
    for bad_csv, needle in ((dup, "重复"), (empty, "为空")):
        raw_id = _id_from_flash(_upload(client, "raw", "a\n1\n", "r.csv"))
        base_id = _id_from_flash(_upload(
            client, "cleaned", BASE_CSV, "b.csv", parent_ids=raw_id))
        left_id = _id_from_flash(_upload(
            client, "cleaned", bad_csv, "l.csv", parent_ids=base_id))
        right_id = _id_from_flash(_upload(
            client, "cleaned", BASE_CSV, "r.csv", parent_ids=base_id))
        with pytest.raises(ServiceError, match=needle):
            _create(app, base_id, left_id, right_id)


def test_create_rejects_unknown_key_column(app, client):
    _, base_id, left_id, right_id = _make_trio(client, BASE_CSV, BASE_CSV)
    with pytest.raises(ServiceError, match="不在表列名中"):
        _create(app, base_id, left_id, right_id, key="不存在的列")


# ---------------------------------------------------------------------------
# 2. Merge semantics
# ---------------------------------------------------------------------------

def test_one_sided_and_identical_changes_auto_merge(app, client):
    left = "记录号,批号,含量,备注\nR1,B1,10.6,ok\nR2,B2,11.0,ok\nR3,B3,99.9,fix\n"
    right = "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.1,ok\nR3,B3,99.9,fix\n"
    _, base_id, left_id, right_id = _make_trio(client, left, right)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0")
    assert result["status"] == "confirmed"
    text = _download_text(client, result["file_id"])
    # R1: only left changed; R2: only right changed; R3: both made the same fix.
    assert text == (
        "记录号,批号,含量,备注\n"
        "R1,B1,10.6,ok\n"
        "R2,B2,11.1,ok\n"
        "R3,B3,99.9,fix\n"
    )


def test_disjoint_cell_changes_auto_merge(app, client):
    # Both sides edited the same row but different cells.
    left = "记录号,批号,含量,备注\nR1,B1,10.6,ok\n"
    right = "记录号,批号,含量,备注\nR1,B1,10.5,edited\n"
    _, base_id, left_id, right_id = _make_trio(
        client, left, right,
        base_csv="记录号,批号,含量,备注\nR1,B1,10.5,ok\n",
    )
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0")
    assert result["status"] == "confirmed"
    assert _download_text(client, result["file_id"]) == (
        "记录号,批号,含量,备注\nR1,B1,10.6,edited\n"
    )


def test_cell_conflict_choose_left_or_right(app, client):
    left = "记录号,批号,含量,备注\nR1,B1,10.6,ok\n"
    right = "记录号,批号,含量,备注\nR1,B1,10.7,ok\n"
    _, base_id, left_id, right_id = _make_trio(
        client, left, right,
        base_csv="记录号,批号,含量,备注\nR1,B1,10.5,ok\n",
    )
    draft_id = _create(app, base_id, left_id, right_id)
    # Unresolved: confirmation refused.
    result = _confirm(app, draft_id, "0", {"0": "left"})
    assert result["status"] == "confirmed"
    assert "R1,B1,10.6,ok" in _download_text(client, result["file_id"])

    # Fresh draft: pick the right side instead.
    _, base_id, left_id, right_id = _make_trio(
        client, left, right,
        base_csv="记录号,批号,含量,备注\nR1,B1,10.5,ok\n",
    )
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0", {"0": "right"})
    assert "R1,B1,10.7,ok" in _download_text(client, result["file_id"])


def test_delete_vs_modify_conflict_keep_or_drop(app, client):
    base = "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.0,ok\n"
    left = "记录号,批号,含量,备注\nR1,B1,10.5,ok\n"            # left deleted R2
    right = "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.5,fix\n"  # right fixed R2
    _, base_id, left_id, right_id = _make_trio(client, left, right, base)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0", {"0": "keep"})
    assert result["status"] == "confirmed"
    assert "R2,B2,11.5,fix" in _download_text(client, result["file_id"])

    _, base_id, left_id, right_id = _make_trio(client, left, right, base)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0", {"0": "drop"})
    text = _download_text(client, result["file_id"])
    assert "R2" not in text and "R1,B1,10.5,ok" in text


def test_missing_row_differs_from_empty_field(app, client):
    # Base has a real value; left empties the cell; right deletes the row.
    # Emptying a cell is a modification, so this is a delete/modify conflict,
    # not an automatic deletion.
    base = "记录号,批号,含量,备注\nR1,B1,10.5,ok\n"
    left = "记录号,批号,含量,备注\nR1,B1,,ok\n"
    right = "记录号,批号,含量,备注\n"
    right += "R9,B9,9.9,extra\n"  # keep right non-empty; R1 absent = deleted
    _, base_id, left_id, right_id = _make_trio(client, left, right, base)
    draft_id = _create(app, base_id, left_id, right_id)
    draft = _draft(app, draft_id)
    state = json.loads(draft["state_json"])
    kinds = {k: v["kind"] for k, v in state["rows"].items()}
    assert kinds["R1"] == "delete_conflict"  # not auto-dropped
    result = _confirm(app, draft_id, "0", {"0": "keep"})
    text = _download_text(client, result["file_id"])
    assert "R1,B1,,ok" in text  # the emptied field survives as empty


def test_both_added_rows(app, client):
    base = "记录号,批号,含量,备注\nR1,B1,10.5,ok\n"
    left = base + "R2,B2,11.0,new\n"
    right = base + "R2,B2,11.0,new\nR3,B3,12.0,only-r\n"
    _, base_id, left_id, right_id = _make_trio(client, left, right, base)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0")
    assert result["status"] == "confirmed"
    text = _download_text(client, result["file_id"])
    # Identical addition merged automatically; right-only row appended after.
    assert text == (
        "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.0,new\nR3,B3,12.0,only-r\n"
    )


def test_both_added_differently_is_conflict(app, client):
    base = "记录号,批号,含量,备注\nR1,B1,10.5,ok\n"
    left = base + "R2,B2,11.0,from-left\n"
    right = base + "R2,B2,11.0,from-right\n"
    _, base_id, left_id, right_id = _make_trio(client, left, right, base)
    draft_id = _create(app, base_id, left_id, right_id)
    draft = _draft(app, draft_id)
    state = json.loads(draft["state_json"])
    # No base row for R2: the differing cell cannot be auto-resolved.
    assert state["rows"]["R2"]["kind"] == "cell_conflict"
    conflict = state["conflicts"][0]
    assert conflict["base"] is None  # 缺行, not empty string
    result = _confirm(app, draft_id, "0", {"0": "right"})
    assert "R2,B2,11.0,from-right" in _download_text(client, result["file_id"])


def test_header_only_product_when_nothing_kept(app, client):
    # Each side deletes the row the other side keeps unchanged: every base
    # row is dropped by a one-sided delete, and neither side adds anything.
    # (A cleaned upload must contain at least one data row, so "both sides
    # delete everything" cannot be uploaded directly.)
    base = "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.0,ok\n"
    left = "记录号,批号,含量,备注\nR1,B1,10.5,ok\n"   # deleted R2
    right = "记录号,批号,含量,备注\nR2,B2,11.0,ok\n"  # deleted R1
    _, base_id, left_id, right_id = _make_trio(client, left, right, base)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0")
    assert result["status"] == "confirmed"
    text = _download_text(client, result["file_id"])
    assert text == "记录号,批号,含量,备注\n"  # header only


# ---------------------------------------------------------------------------
# 3. Draft persistence, revision checks, confirmation rules
# ---------------------------------------------------------------------------

def _conflict_trio(client):
    """One cell conflict (R1 含量) + one delete conflict (R2)."""
    base = "记录号,批号,含量,备注\nR1,B1,10.5,ok\nR2,B2,11.0,ok\n"
    left = "记录号,批号,含量,备注\nR1,B1,10.6,ok\n"              # changed R1, deleted R2
    right = "记录号,批号,含量,备注\nR1,B1,10.7,ok\nR2,B2,11.5,fix\n"  # changed R1+R2
    _, base_id, left_id, right_id = _make_trio(client, left, right, base)
    return base_id, left_id, right_id


def test_save_and_resume_after_restart(app, client, tmp_path):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    remaining = _save(app, draft_id, "0", {"0": "left"})
    assert remaining == 1  # the delete conflict is still open

    config = {
        "TESTING": True,
        "DATABASE": app.config["DATABASE"],
        "STORAGE_DIR": app.config["STORAGE_DIR"],
        "STAGING_DIR": app.config["STAGING_DIR"],
    }
    app2 = create_app(config)  # simulate process restart
    with app2.app_context():
        conn = db.get_db()
        draft = db.get_merge_draft(conn, draft_id)
        assert json.loads(draft["choices_json"]) == {"0": "left"}
        assert draft["revision"] == 1
        # Resume: resolve the rest and confirm against the restarted app.
        result = merge.confirm_merge(draft_id, "1", {"0": "left", "1": "keep"})
    assert result["status"] == "confirmed"
    text = _download_text(app2.test_client(), result["file_id"])
    assert "R1,B1,10.6,ok" in text and "R2,B2,11.5,fix" in text


def test_stale_revision_rejected_for_save_and_confirm(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    _save(app, draft_id, "0", {"0": "left"})  # revision now 1
    with pytest.raises(ServiceError, match="过期"):
        _save(app, draft_id, "0", {"0": "right"})
    with pytest.raises(ServiceError, match="过期"):
        _confirm(app, draft_id, "0", {"0": "left", "1": "keep"})
    # The draft itself is untouched by the stale attempts.
    draft = _draft(app, draft_id)
    assert draft["revision"] == 1
    assert json.loads(draft["choices_json"]) == {"0": "left"}


def test_confirm_with_unresolved_conflicts_refused(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0", {"0": "left"})
    assert result["status"] == "incomplete" and result["remaining"] == 1
    draft = _draft(app, draft_id)
    assert draft["status"] == "open"  # still a retryable draft
    assert json.loads(draft["choices_json"]) == {"0": "left"}  # choices kept


def test_confirm_registers_result_with_left_right_parents(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0", {"0": "right", "1": "keep"})
    assert result["status"] == "confirmed"
    file_id = result["file_id"]
    assert file_id.startswith("CLN-")
    with app.app_context():
        conn = db.get_db()
        row = db.get_file(conn, file_id)
        assert row["kind"] == "cleaned"
        parents = [p["id"] for p in db.list_parents(conn, file_id)]
        assert parents == [left_id, right_id]
        draft = db.get_merge_draft(conn, draft_id)
        assert draft["status"] == "completed"
        assert draft["result_id"] == file_id


def test_duplicate_and_concurrent_confirm_return_same_file(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    choices = {"0": "left", "1": "keep"}

    outcomes: list[dict] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        with app.app_context():
            outcomes.append(merge.confirm_merge(draft_id, "0", dict(choices)))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(outcomes) == 2
    file_ids = {o["file_id"] for o in outcomes}
    assert len(file_ids) == 1  # exactly one product registered
    statuses = sorted(o["status"] for o in outcomes)
    assert statuses == ["already", "confirmed"]

    # A later duplicate confirm still returns the same file.
    again = _confirm(app, draft_id, "0", dict(choices))
    assert again == {"status": "already", "file_id": file_ids.pop()}


def test_completed_draft_cannot_be_modified(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0", {"0": "left", "1": "keep"})
    assert result["status"] == "confirmed"
    with pytest.raises(ServiceError, match="已完成"):
        _save(app, draft_id, "2", {"0": "right"})


def test_confirm_refused_when_source_unavailable(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    _save(app, draft_id, "0", {"0": "left", "1": "keep"})
    client.post(f"/files/{right_id}/revoke", data={"reason": "更正有误"})
    result = _confirm(app, draft_id, "1", {"0": "left", "1": "keep"})
    assert result["status"] == "unavailable"
    assert right_id in result["message"]
    draft = _draft(app, draft_id)
    assert draft["status"] == "open"  # still retryable, choices preserved
    assert json.loads(draft["choices_json"]) == {"0": "left", "1": "keep"}


def test_result_follows_lineage_revocation_rules(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    result = _confirm(app, draft_id, "0", {"0": "left", "1": "keep"})
    file_id = result["file_id"]
    assert client.get(f"/files/{file_id}/download").status_code == 200
    # Revoking a parent (left table) blocks the merged product immediately.
    client.post(f"/files/{left_id}/revoke", data={"reason": "左表作废"})
    resp = client.get(f"/files/{file_id}/download", follow_redirects=False)
    assert resp.status_code == 302


def test_invalid_choice_payload_rejected(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    with pytest.raises(ServiceError, match="无效"):
        _save(app, draft_id, "0", {"0": "sideways"})
    with pytest.raises(ServiceError, match="无效"):
        _save(app, draft_id, "0", {"99": "left"})
    with pytest.raises(ServiceError, match="无效"):
        _save(app, draft_id, "0", {"1": "left"})  # delete conflict: keep/drop only


def test_crashed_confirm_swept_on_restart_and_draft_retryable(app, client):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    _save(app, draft_id, "0", {"0": "left", "1": "keep"})

    # Simulate a crash mid-confirm: staged file + promoted-but-unregistered
    # object, no committed transaction.
    staged = os.path.join(app.config["STAGING_DIR"], ".merge-crashed")
    with open(staged, "w") as fh:
        fh.write("partial")
    orphan = os.path.join(app.config["STORAGE_DIR"], "CLN-0099.csv")
    with open(orphan, "w") as fh:
        fh.write("half-baked")

    config = {
        "TESTING": True,
        "DATABASE": app.config["DATABASE"],
        "STORAGE_DIR": app.config["STORAGE_DIR"],
        "STAGING_DIR": app.config["STAGING_DIR"],
    }
    app2 = create_app(config)  # restart sweeps the debris
    assert not os.path.exists(staged)
    assert not os.path.exists(orphan)

    # The draft is still open with its choices and confirms cleanly.
    with app2.app_context():
        result = merge.confirm_merge(
            draft_id, "1", {"0": "left", "1": "keep"}
        )
    assert result["status"] == "confirmed"
    text = _download_text(app2.test_client(), result["file_id"])
    assert "R1,B1,10.6,ok" in text


# ---------------------------------------------------------------------------
# 4. HTTP pages
# ---------------------------------------------------------------------------

def test_merge_pages_and_http_flow(app, client):
    base_id, left_id, right_id = _conflict_trio(client)

    # Selection page lists the drafts and the create form.
    body = client.get("/merge").get_data(as_text=True)
    assert "新建三方合并草稿" in body and base_id in body

    # Create through the browser form.
    resp = client.post("/merge", data={
        "base_id": base_id, "left_id": left_id, "right_id": right_id,
        "key_column": "记录号",
    }, follow_redirects=False)
    assert resp.status_code == 302
    draft_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    # Worksheet shows both conflicts and the auto-merge summary.
    body = client.get(f"/merge/{draft_id}").get_data(as_text=True)
    assert "双方同格异改" in body and "删改冲突" in body
    assert "自动合入明细" in body and 'name="revision"' in body

    # Save the first choice via HTTP; revision is enforced.
    resp = client.post(f"/merge/{draft_id}/save",
                       data={"revision": "0", "choice-0": "left"},
                       follow_redirects=True)
    assert "剩余未解决冲突 1 处" in resp.get_data(as_text=True)
    resp = client.post(f"/merge/{draft_id}/save",
                       data={"revision": "0", "choice-1": "keep"},
                       follow_redirects=True)
    assert "过期" in resp.get_data(as_text=True)

    # Confirm with everything resolved; the page then offers the download.
    resp = client.post(
        f"/merge/{draft_id}/confirm",
        data={"revision": "1", "choice-0": "left", "choice-1": "keep"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "合并完成" in body and "下载成品 CSV" in body
    match = re.search(r"CLN-\d{4}", body)
    assert match
    text = _download_text(client, match.group(0))
    assert "R1,B1,10.6,ok" in text and "R2,B2,11.5,fix" in text

    # The completed worksheet is locked and shows the result.
    body = client.get(f"/merge/{draft_id}").get_data(as_text=True)
    assert "已完成" in body and "选择已锁定" in body


def test_merge_create_form_errors_flash_back(app, client):
    raw_id = _id_from_flash(_upload(client, "raw", "a\n1\n", "r.csv"))
    resp = client.post("/merge", data={
        "base_id": raw_id, "left_id": "CLN-9999", "right_id": "CLN-9998",
        "key_column": "记录号",
    }, follow_redirects=True)
    assert "创建合并草稿被拒绝" in resp.get_data(as_text=True)


def test_failed_confirm_leaves_no_trace(app, client, tmp_path):
    base_id, left_id, right_id = _conflict_trio(client)
    draft_id = _create(app, base_id, left_id, right_id)
    # Force a failure: revoke a source so confirm bails out before writing.
    client.post(f"/files/{base_id}/revoke", data={"reason": "基表作废"})
    result = _confirm(app, draft_id, "0", {"0": "left", "1": "keep"})
    assert result["status"] == "unavailable"
    # No half-finished product: object store holds exactly the 4 uploads.
    assert sorted(os.listdir(tmp_path / "objects")) == [
        "CLN-0001.csv", "CLN-0002.csv", "CLN-0003.csv", "RAW-0001.csv",
    ]
    staging = [e for e in os.listdir(tmp_path / "staging")
               if e.startswith(".merge-")]
    assert staging == []
    conn = sqlite3.connect(app.config["DATABASE"])
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE kind='cleaned'"
    ).fetchone()[0] == 3
    conn.close()
