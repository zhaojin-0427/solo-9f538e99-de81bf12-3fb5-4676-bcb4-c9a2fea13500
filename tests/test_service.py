"""End-to-end style tests for the file registration / revocation service."""

from __future__ import annotations

import io
import os
import sqlite3
import threading

import pytest

from app import create_app, db
from app.services import register_file, revoke_file, ServiceError


VALID_CSV = "batch,value\nB1,10.5\nB2,11.0\n"


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "stability.sqlite3"),
            "STORAGE_DIR": str(tmp_path / "objects"),
            "STAGING_DIR": str(tmp_path / "staging"),
            "WTF_CSRF_ENABLED": False,
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def _upload(client, kind, content=VALID_CSV, filename="m.csv",
            parent_ids=""):
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
    import re

    match = re.search(r"(RAW|CLN|SUM)-\d{4}", body)
    assert match, body
    return match.group(0)


# ---------------------------------------------------------------------------
# 1. Basic registration and human-readable IDs
# ---------------------------------------------------------------------------

def test_register_raw_cleaned_summary_chain(client):
    raw_id = _id_from_flash(_upload(client, "raw", filename="raw.csv"))
    assert raw_id == "RAW-0001"

    cln_id = _id_from_flash(
        _upload(client, "cleaned", filename="clean.csv", parent_ids=raw_id)
    )
    assert cln_id == "CLN-0001"

    sum_id = _id_from_flash(
        _upload(client, "summary", filename="sum.csv", parent_ids=cln_id)
    )
    assert sum_id == "SUM-0001"

    body = client.get("/").get_data(as_text=True)
    assert "RAW-0001" in body and "CLN-0001" in body and "SUM-0001" in body
    assert "可下载" in body


def test_second_raw_gets_new_id_and_repair_does_not_revive(client):
    first = _id_from_flash(_upload(client, "raw"))
    second = _id_from_flash(_upload(client, "raw"))
    assert first != second and second == "RAW-0002"


# ---------------------------------------------------------------------------
# 2. Parent availability rules at submit time
# ---------------------------------------------------------------------------

def test_derived_requires_parent(client):
    resp = _upload(client, "cleaned", parent_ids="")
    assert "必须登记至少一个父文件 ID" in resp.get_data(as_text=True)


def test_raw_cannot_have_parents(client):
    resp = _upload(client, "raw", parent_ids="RAW-9999")
    assert "原始测量文件没有父文件" in resp.get_data(as_text=True)


def test_unknown_parent_rejected(client):
    resp = _upload(client, "cleaned", parent_ids="RAW-9999")
    assert "父文件 RAW-9999 不存在" in resp.get_data(as_text=True)


def test_revoked_parent_rejected(client):
    raw_id = _id_from_flash(_upload(client, "raw"))
    client.post(
        f"/files/{raw_id}/revoke",
        data={"reason": "原始测量错误"},
        follow_redirects=True,
    )
    resp = _upload(client, "cleaned", parent_ids=raw_id)
    assert "已被撤销" in resp.get_data(as_text=True)


def test_blocked_parent_rejected(client):
    """Parent itself unrevoked but blocked by a revoked ancestor."""
    raw_id = _id_from_flash(_upload(client, "raw"))
    cln_id = _id_from_flash(_upload(client, "cleaned", parent_ids=raw_id))
    client.post(f"/files/{raw_id}/revoke", data={"reason": "bad data"})
    resp = _upload(client, "summary", parent_ids=cln_id)
    body = resp.get_data(as_text=True)
    assert "不可用" in body and raw_id in body


# ---------------------------------------------------------------------------
# 3. Revocation propagation and download gating
# ---------------------------------------------------------------------------

def _build_chain(client):
    raw_id = _id_from_flash(_upload(client, "raw", filename="raw.csv"))
    cln_id = _id_from_flash(
        _upload(client, "cleaned", filename="clean.csv", parent_ids=raw_id)
    )
    sum_id = _id_from_flash(
        _upload(client, "summary", filename="sum.csv", parent_ids=cln_id)
    )
    return raw_id, cln_id, sum_id


def test_download_works_before_revoke(client):
    raw_id, cln_id, sum_id = _build_chain(client)
    for fid in (raw_id, cln_id, sum_id):
        resp = client.get(f"/files/{fid}/download")
        assert resp.status_code == 200
        assert b"batch,value" in resp.data


def test_revoke_cascades_and_lists_affected_chain(client):
    raw_id, cln_id, sum_id = _build_chain(client)
    resp = client.post(
        f"/files/{raw_id}/revoke",
        data={"reason": "测量 CSV 有误"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    # Detail page lists every affected descendant and the full chain.
    assert cln_id in body and sum_id in body
    assert f"{raw_id} -&gt; {cln_id} -&gt; {sum_id}" in body
    assert "已撤销" in body

    # All three stop downloading immediately.
    for fid in (raw_id, cln_id, sum_id):
        resp = client.get(f"/files/{fid}/download", follow_redirects=False)
        assert resp.status_code == 302
        detail = client.get(f"/files/{fid}").get_data(as_text=True)
        assert "下载已停用" in detail

    # List page badges reflect blocked state.
    listing = client.get("/").get_data(as_text=True)
    assert "依赖阻断" in listing


def test_repair_upload_gets_new_id_and_old_descendants_stay_dead(client):
    raw_id, cln_id, sum_id = _build_chain(client)
    client.post(f"/files/{raw_id}/revoke", data={"reason": "wrong csv"})

    repaired = _id_from_flash(_upload(client, "raw", filename="raw_fixed.csv"))
    assert repaired == "RAW-0002" and repaired != raw_id

    # Old derived files remain blocked; registering the repair changes nothing.
    for fid in (cln_id, sum_id):
        resp = client.get(f"/files/{fid}/download", follow_redirects=False)
        assert resp.status_code == 302

    # A new derivation from the repaired raw file is allowed.
    new_cln = _id_from_flash(
        _upload(client, "cleaned", parent_ids=repaired)
    )
    assert client.get(f"/files/{new_cln}/download").status_code == 200


def test_revoke_requires_reason(client):
    raw_id, _, _ = _build_chain(client)
    resp = client.post(
        f"/files/{raw_id}/revoke", data={"reason": ""},
        follow_redirects=True,
    )
    assert "请填写撤销原因" in resp.get_data(as_text=True)
    assert client.get(f"/files/{raw_id}/download").status_code == 200


def test_revoke_idempotent(client):
    raw_id, _, _ = _build_chain(client)
    client.post(f"/files/{raw_id}/revoke", data={"reason": "first"})
    resp = client.post(
        f"/files/{raw_id}/revoke", data={"reason": "second"},
        follow_redirects=True,
    )
    assert "此前已被撤销" in resp.get_data(as_text=True)


def test_diamond_dependency_all_paths_block(client):
    raw1 = _id_from_flash(_upload(client, "raw"))
    raw2 = _id_from_flash(_upload(client, "raw"))
    cln = _id_from_flash(
        _upload(client, "cleaned", parent_ids=f"{raw1} {raw2}")
    )
    client.post(f"/files/{raw2}/revoke", data={"reason": "x"})
    # Blocked through the raw2 path even though raw1 is healthy.
    assert client.get(f"/files/{cln}/download",
                      follow_redirects=False).status_code == 302
    detail = client.get(f"/files/{cln}").get_data(as_text=True)
    assert raw2 in detail and "阻断路径" in detail


# ---------------------------------------------------------------------------
# 4. CSV validation and no half-finished state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content,needle",
    [
        ("", "为空"),
        ("only_header\n", "没有数据行"),
        ("a,b,c\n1,2\n", "列数"),
        ("a,b\n1,2\n\n3,4\n", "空行"),
    ],
)
def test_bad_csv_rejected(client, content, needle):
    resp = _upload(client, "raw", content=content, filename="bad.csv")
    assert needle in resp.get_data(as_text=True)


def test_non_csv_extension_rejected(client):
    resp = _upload(client, "raw", content=VALID_CSV, filename="data.txt")
    assert "只接受 .csv" in resp.get_data(as_text=True)


def test_failed_registration_leaves_no_trace(app, client, tmp_path):
    # Failure: unknown parent.  Nothing may be registered or stored.
    resp = _upload(client, "cleaned", parent_ids="RAW-9999")
    assert resp.status_code == 200
    assert os.listdir(tmp_path / "objects") == []
    assert [e for e in os.listdir(tmp_path / "staging")
            if e.startswith(".upload-")] == []
    conn = sqlite3.connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0
    conn.close()


def test_bad_csv_leaves_no_trace(app, client, tmp_path):
    _upload(client, "raw", content="a\n\nb\n", filename="bad.csv")
    assert os.listdir(tmp_path / "objects") == []
    conn = sqlite3.connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# 5. Immutability of registered records
# ---------------------------------------------------------------------------

def test_no_mutation_routes_exist(app, client):
    raw_id, cln_id, _ = _build_chain(client)
    # Only GET detail/download and POST revoke are exposed; no edit/delete.
    for method, path in [
        ("post", f"/files/{raw_id}"),
        ("put", f"/files/{raw_id}"),
        ("delete", f"/files/{raw_id}"),
        ("post", f"/files/{raw_id}/dependencies"),
    ]:
        resp = client.open(path, method=method)
        assert resp.status_code in (404, 405)


def test_dependency_rows_are_immutable_in_db(app, client):
    raw_id, cln_id, _ = _build_chain(client)
    with app.app_context():
        conn = db.get_db()
        parents = db.list_parents(conn, cln_id)
        assert [p["id"] for p in parents] == [raw_id]
        # No update endpoints exist; schema itself forbids duplicate edges.
        with pytest.raises(sqlite3.Error):
            conn.execute(
                "INSERT INTO dependencies(child_id, parent_id, position) "
                "VALUES (?, ?, ?)", (cln_id, raw_id, 9),
            )


# ---------------------------------------------------------------------------
# 6. Interleaved upload vs revoke: no file can ever reference a revoked parent
# ---------------------------------------------------------------------------

def test_concurrent_upload_vs_revoke(app, tmp_path):
    with app.app_context():
        conn = db.get_db()
        from werkzeug.datastructures import FileStorage

        def make_storage():
            return FileStorage(
                stream=io.BytesIO(VALID_CSV.encode()),
                filename="c.csv",
            )

        # Pre-register the raw parent through the service.
        raw_id = register_file(make_storage(), "raw", "")

        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def worker_upload():
            barrier.wait()
            with app.app_context():
                try:
                    register_file(make_storage(), "cleaned", raw_id)
                    outcomes.append("upload-ok")
                except ServiceError:
                    outcomes.append("upload-rejected")

        def worker_revoke():
            barrier.wait()
            with app.app_context():
                revoke_file(raw_id, "concurrent revoke")
                outcomes.append("revoke-ok")

        t1 = threading.Thread(target=worker_upload)
        t2 = threading.Thread(target=worker_revoke)
        t1.start(); t2.start(); t1.join(); t2.join()

        assert "revoke-ok" in outcomes
        # Whatever the order, the invariant must hold afterwards.
        rows = conn.execute(
            "SELECT f.id, f.revoked FROM files f "
            "JOIN dependencies d ON d.child_id = f.id "
            "WHERE d.parent_id = ?", (raw_id,),
        ).fetchall()
        for child_id, revoked in rows:
            # If the edge was registered, the child must now be blocked and
            # therefore not downloadable.
            assert db.is_available(conn, child_id) is False
        assert db.is_available(conn, raw_id) is False


def test_repeated_interleaving_never_publishes_blocked_reference(app):
    """Run many interleaved rounds; no registered child of a revoked parent
    may ever be downloadable."""
    with app.app_context():
        from werkzeug.datastructures import FileStorage

        def storage():
            return FileStorage(stream=io.BytesIO(VALID_CSV.encode()),
                               filename="c.csv")

        for round_no in range(15):
            raw_id = register_file(storage(), "raw", "")
            errors: list[Exception] = []

            def up():
                with app.app_context():
                    try:
                        register_file(storage(), "cleaned", raw_id)
                    except ServiceError as exc:
                        errors.append(exc)

            def rev():
                with app.app_context():
                    revoke_file(raw_id, f"round {round_no}")

            t1 = threading.Thread(target=up)
            t2 = threading.Thread(target=rev)
            t1.start(); t2.start(); t1.join(); t2.join()

            conn = db.get_db()
            children = conn.execute(
                "SELECT child_id FROM dependencies WHERE parent_id=?",
                (raw_id,),
            ).fetchall()
            for (child_id,) in children:
                assert db.is_available(conn, child_id) is False


# ---------------------------------------------------------------------------
# 7. Restart consistency: eligibility is fully derivable from persisted state
# ---------------------------------------------------------------------------

def test_eligibility_survives_restart(tmp_path):
    config = {
        "TESTING": True,
        "DATABASE": str(tmp_path / "db.sqlite3"),
        "STORAGE_DIR": str(tmp_path / "objects"),
        "STAGING_DIR": str(tmp_path / "staging"),
    }
    app1 = create_app(config)
    client1 = app1.test_client()
    raw_id, cln_id, sum_id = _build_chain(client1)
    client1.post(f"/files/{raw_id}/revoke", data={"reason": "persist me"})

    # Simulate process restart: brand-new app instance on the same data dir.
    app2 = create_app(config)
    client2 = app2.test_client()
    for fid in (raw_id, cln_id, sum_id):
        resp = client2.get(f"/files/{fid}/download", follow_redirects=False)
        assert resp.status_code == 302, f"{fid} downloadable after restart"

    body = client2.get("/").get_data(as_text=True)
    assert "已撤销" in body and "依赖阻断" in body

    # Staging leftovers from a crashed upload are swept on startup.
    crash_file = os.path.join(config["STAGING_DIR"], ".upload-crashed")
    open(crash_file, "w").write("partial")
    create_app(config)
    assert not os.path.exists(crash_file)


def test_object_store_immutable_after_restart(tmp_path):
    config = {
        "TESTING": True,
        "DATABASE": str(tmp_path / "db.sqlite3"),
        "STORAGE_DIR": str(tmp_path / "objects"),
        "STAGING_DIR": str(tmp_path / "staging"),
    }
    app1 = create_app(config)
    raw_id = _id_from_flash(app1.test_client().post(
        "/upload",
        data={"kind": "raw",
              "file": (io.BytesIO(VALID_CSV.encode()), "r.csv"),
              "parent_ids": ""},
        content_type="multipart/form-data",
        follow_redirects=True,
    ))
    stored = os.path.join(config["STORAGE_DIR"], f"{raw_id}.csv")
    assert os.path.isfile(stored)
    before = open(stored, "rb").read()
    create_app(config)  # restart sweep must not touch registered objects
    assert open(stored, "rb").read() == before


def test_startup_sweep_removes_orphans(tmp_path):
    config = {
        "TESTING": True,
        "DATABASE": str(tmp_path / "db.sqlite3"),
        "STORAGE_DIR": str(tmp_path / "objects"),
        "STAGING_DIR": str(tmp_path / "staging"),
    }
    app1 = create_app(config)
    raw_id = _id_from_flash(app1.test_client().post(
        "/upload",
        data={"kind": "raw",
              "file": (io.BytesIO(VALID_CSV.encode()), "r.csv"),
              "parent_ids": ""},
        content_type="multipart/form-data",
        follow_redirects=True,
    ))
    # Simulate crash leftovers in both working directories.
    open(os.path.join(config["STAGING_DIR"], ".upload-crashed"), "w").write("x")
    open(os.path.join(config["STORAGE_DIR"], "ORPHAN-0001.csv"), "w").write("x")
    create_app(config)
    assert not os.path.exists(
        os.path.join(config["STAGING_DIR"], ".upload-crashed"))
    assert not os.path.exists(
        os.path.join(config["STORAGE_DIR"], "ORPHAN-0001.csv"))
    # Registered object survives the sweep.
    assert os.path.isfile(
        os.path.join(config["STORAGE_DIR"], f"{raw_id}.csv"))
