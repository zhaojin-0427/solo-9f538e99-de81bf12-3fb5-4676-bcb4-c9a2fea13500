"""Domain services: atomic registration, revocation and download gating."""

from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
import tempfile

from flask import current_app, g

from . import db

KINDS = ("raw", "cleaned", "summary")
KIND_LABELS = {"raw": "原始测量", "cleaned": "清洗表", "summary": "批次汇总"}
_CHUNK = 1024 * 1024


class ServiceError(Exception):
    """User-facing validation / state error (HTTP 400 style)."""


# ---------------------------------------------------------------------------
# CSV validation
# ---------------------------------------------------------------------------

def validate_csv(path: str, allow_header_only: bool = False) -> None:
    """Reject anything that is not a well-formed, non-empty CSV file.

    ``allow_header_only`` is used for merge products: a merge that keeps no
    rows is still a valid deliverable consisting of just the header line.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError as exc:
        raise ServiceError("文件不是有效的 UTF-8 编码 CSV") from exc
    except OSError as exc:
        raise ServiceError(f"文件读取失败：{exc}") from exc

    if not text.strip():
        raise ServiceError("CSV 文件为空")

    # csv.reader silently skips blank lines, so detect them on the raw text.
    raw_lines = text.splitlines()
    for line_no, line in enumerate(raw_lines, start=1):
        if not line.strip():
            raise ServiceError(f"第 {line_no} 行为空行，CSV 不允许空行")

    try:
        rows = list(csv.reader(raw_lines))
    except csv.Error as exc:
        raise ServiceError(f"CSV 解析失败：{exc}") from exc

    header = [cell.strip() for cell in rows[0]]
    if not any(header):
        raise ServiceError("CSV 缺少表头行")
    if not allow_header_only and (
        len(rows) < 2
        or not any(any(cell.strip() for cell in row) for row in rows[1:])
    ):
        raise ServiceError("CSV 只有表头，没有数据行")
    width = len(rows[0])
    if width == 0:
        raise ServiceError("CSV 表头没有列")
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            raise ServiceError(
                f"第 {idx} 行列数为 {len(row)}，与表头列数 {width} 不一致"
            )
        if not any(cell.strip() for cell in row):
            raise ServiceError(f"第 {idx} 行为空行")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _normalise_parent_ids(raw: str) -> list[str]:
    ids = [token.strip() for token in raw.replace(",", " ").split()]
    # Preserve order, remove duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for pid in ids:
        if pid and pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    return ordered


def register_file(file_storage, kind: str, parent_ids_raw: str) -> str:
    """Validate, persist and register an upload as one atomic operation.

    The bytes first land in a private staging directory.  Only after the CSV
    content is validated and every declared parent is confirmed available do
    we rename the file into the object store and commit the database
    transaction.  Any failure rolls the database back and removes the staged /
    promoted file, so no half-finished registration is ever visible.
    """
    if kind not in KINDS:
        raise ServiceError("文件类型无效")
    if file_storage is None or not file_storage.filename:
        raise ServiceError("请选择要上传的文件")
    filename = os.path.basename(file_storage.filename)
    if not filename.lower().endswith(".csv"):
        raise ServiceError("只接受 .csv 文件")

    parent_ids = _normalise_parent_ids(parent_ids_raw or "")
    if kind == "raw":
        if parent_ids:
            raise ServiceError("原始测量文件没有父文件，请勿填写父文件 ID")
    elif not parent_ids:
        raise ServiceError(f"{KIND_LABELS[kind]}必须登记至少一个父文件 ID")

    staging_dir = current_app.config["STAGING_DIR"]
    storage_dir = current_app.config["STORAGE_DIR"]
    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(storage_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=staging_dir, prefix=".upload-")
    final_path: str | None = None
    committed = False
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = file_storage.stream.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise ServiceError("上传的文件为空")
        validate_csv(tmp_path)

        conn = db.get_db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Parent check happens inside the write lock: a concurrent revoke
            # cannot interleave between the check and the registration.
            for pid in parent_ids:
                prow = db.get_file(conn, pid)
                if prow is None:
                    raise ServiceError(
                        f"父文件 {pid} 不存在，提交时父项必须可用"
                    )
                if prow["revoked"]:
                    raise ServiceError(
                        f"父文件 {pid} 已被撤销，不能作为依赖登记"
                    )
                blocker, _, _ = db.find_blocking_ancestor(conn, pid)
                if blocker is not None:
                    raise ServiceError(
                        f"父文件 {pid} 因祖先 {blocker} 被撤销而不可用，"
                        "不能作为依赖登记"
                    )

            file_id = db.next_file_id(conn, kind)
            stored_name = f"{file_id}.csv"
            final_path = os.path.join(storage_dir, stored_name)
            os.rename(tmp_path, final_path)  # atomic within the volume
            tmp_path = None

            conn.execute(
                """
                INSERT INTO files
                    (id, kind, filename, content_sha256, size_bytes, stored_name)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    kind,
                    filename,
                    digest.hexdigest(),
                    size,
                    stored_name,
                ),
            )
            for position, pid in enumerate(parent_ids):
                conn.execute(
                    "INSERT INTO dependencies(child_id, parent_id, position) "
                    "VALUES (?, ?, ?)",
                    (file_id, pid, position),
                )
            conn.execute(
                "INSERT INTO audit_events(file_id, action, detail) "
                "VALUES (?, 'upload', ?)",
                (file_id, f"kind={kind}; parents={','.join(parent_ids) or '-'}"),
            )
            conn.execute("COMMIT")
            committed = True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return file_id
    except sqlite3.Error as exc:
        raise ServiceError(f"数据库登记失败：{exc}") from exc
    finally:
        # tmp_path: failed before promotion.  final_path: promotion
        # succeeded but the transaction did not commit -> remove orphan.
        # On successful commit the object must be preserved.
        cleanup = [tmp_path]
        if not committed and final_path is not None:
            cleanup.append(final_path)
        for path in cleanup:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    current_app.logger.exception("清理暂存文件失败: %s", path)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def revoke_file(file_id: str, reason: str) -> dict:
    """Revoke one file.  All transitive descendants stop downloading
    immediately because availability is recomputed from the revoke flag."""
    reason = (reason or "").strip()
    if not reason:
        raise ServiceError("请填写撤销原因")

    conn = db.get_db()
    conn.execute("BEGIN IMMEDIATE")
    already_revoked = False
    try:
        row = db.get_file(conn, file_id)
        if row is None:
            raise ServiceError(f"文件 {file_id} 不存在")
        if row["revoked"]:
            already_revoked = True
        else:
            conn.execute(
                "UPDATE files SET revoked = 1, "
                "revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "revoke_reason = ? WHERE id = ?",
                (reason, file_id),
            )
            conn.execute(
                "INSERT INTO audit_events(file_id, action, detail) "
                "VALUES (?, 'revoke', ?)",
                (file_id, reason),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    chains = [
        dict(r)
        for r in db.descendant_chains(conn, file_id)
        if r["descendant_id"] != file_id
    ]
    affected = sorted({r["descendant_id"] for r in chains})
    return {
        "file_id": file_id,
        "already_revoked": already_revoked,
        "affected": affected,
        "chains": chains,
    }


# ---------------------------------------------------------------------------
# Startup sweep: staging leftovers + orphaned object-store files
# ---------------------------------------------------------------------------

def sweep_working_dirs() -> None:
    storage_dir = current_app.config["STORAGE_DIR"]
    staging_dir = current_app.config["STAGING_DIR"]
    for directory in (staging_dir, storage_dir):
        os.makedirs(directory, exist_ok=True)

    for entry in os.listdir(staging_dir):
        path = os.path.join(staging_dir, entry)
        if entry.startswith((".upload-", ".merge-")):
            try:
                os.unlink(path)
            except OSError:
                current_app.logger.exception("清理暂存残留失败: %s", path)

    conn = db.get_db()
    registered = {
        r["stored_name"] for r in conn.execute("SELECT stored_name FROM files")
    }
    for entry in os.listdir(storage_dir):
        if entry not in registered:
            try:
                os.unlink(os.path.join(storage_dir, entry))
                current_app.logger.warning("启动清理未登记的孤立文件: %s", entry)
            except OSError:
                current_app.logger.exception("清理孤立文件失败: %s", entry)
