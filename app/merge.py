"""Three-way merge of independently corrected cleaned tables.

A merge draft joins one cleaned *base* table with the two cleaned tables that
have **only** that base as parent (two engineers' independent corrections).
Rows are matched by the user-named key column; cells are compared as raw
strings, and a missing row is always distinguished from an empty field.

Automatic rules (no user input needed):
  * only one side changed a cell/row          -> take the changed side
  * both sides made the identical change      -> take it
  * both sides deleted the row                -> drop it
  * changes on disjoint cells                 -> merged cell-by-cell

Conflicts requiring a choice:
  * same cell changed to two different values -> pick left or right
  * one side deleted a row the other modified -> keep the modified row or
    delete it

Drafts persist in SQLite, so a refresh or a process restart resumes exactly
where the user left off.  Saving and confirming both carry the revision the
user saw; stale revisions are rejected.  Confirmation is atomic: the merged
CSV is staged, promoted, registered (parents = left + right tables) and the
draft is completed inside one ``BEGIN IMMEDIATE`` transaction, so a failure
or crash leaves either a complete registered product or a retryable draft.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile

from flask import current_app

from . import db
from .services import ServiceError, validate_csv

MAX_ROWS = 500

_SIDE_LABEL = {"left": "左表", "right": "右表"}


# ---------------------------------------------------------------------------
# Loading and validating source tables
# ---------------------------------------------------------------------------

def _load_table(file_row) -> tuple[list[str], list[dict]]:
    """Read a registered CSV from the object store into (header, row dicts)."""
    path = os.path.join(
        current_app.config["STORAGE_DIR"], file_row["stored_name"]
    )
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ServiceError(
            f"读取表 {file_row['id']} 的存储内容失败：{exc}"
        ) from exc
    if not rows:
        raise ServiceError(f"表 {file_row['id']} 的存储内容为空")
    header = rows[0]
    if len(set(header)) != len(header):
        raise ServiceError(f"表 {file_row['id']} 的列名存在重复，无法按键合并")
    data = rows[1:]
    if len(data) > MAX_ROWS:
        raise ServiceError(
            f"表 {file_row['id']} 共 {len(data)} 行，超过 {MAX_ROWS} 行上限"
        )
    return header, [dict(zip(header, row)) for row in data]


def _index_by_key(file_id: str, records: list[dict], key_column: str) -> dict:
    """Build {order: [...], rows: {key: record}} enforcing non-empty unique keys."""
    order: list[str] = []
    mapping: dict[str, dict] = {}
    for line_no, record in enumerate(records, start=2):
        key = record[key_column]
        if not key.strip():
            raise ServiceError(f"表 {file_id} 第 {line_no} 行的记录号为空")
        if key in mapping:
            raise ServiceError(f"表 {file_id} 的记录号“{key}”重复出现")
        order.append(key)
        mapping[key] = record
    return {"order": order, "rows": mapping}


def _availability_problem(conn, file_id: str) -> str | None:
    """None when the file is usable, else a Chinese reason fragment."""
    row = db.get_file(conn, file_id)
    if row is None:
        return "不存在"
    if row["revoked"]:
        return "已被撤销"
    blocker, _, _ = db.find_blocking_ancestor(conn, file_id)
    if blocker is not None:
        return f"因上游 {blocker} 被撤销而不可用"
    return None


# ---------------------------------------------------------------------------
# Merge computation
# ---------------------------------------------------------------------------

def _cell_merge(
    key: str,
    base_row: dict | None,
    left_row: dict,
    right_row: dict,
    columns: list[str],
    key_column: str,
    conflicts: list,
    entry: dict,
) -> None:
    """Both sides hold differing versions of the row: merge cell by cell.

    A base value of None means the row is *absent* from the base table,
    which is deliberately not the same as an empty string.
    """
    merged = {key_column: key}
    cells = []
    has_conflict = False
    for col in columns:
        if col == key_column:
            continue
        base_val = base_row[col] if base_row is not None else None
        left_val = left_row[col]
        right_val = right_row[col]
        cell = {
            "column": col,
            "base": base_val,
            "left": left_val,
            "right": right_val,
        }
        if left_val == right_val:
            cell["status"] = "same"
            cell["value"] = left_val
        elif base_row is not None and left_val == base_val:
            cell["status"] = "right"  # only the right side changed this cell
            cell["value"] = right_val
        elif base_row is not None and right_val == base_val:
            cell["status"] = "left"  # only the left side changed this cell
            cell["value"] = left_val
        else:
            cell["status"] = "conflict"
            cell["conflict_index"] = len(conflicts)
            conflicts.append(
                {
                    "type": "cell",
                    "key": key,
                    "column": col,
                    "base": base_val,
                    "left": left_val,
                    "right": right_val,
                }
            )
            has_conflict = True
        cells.append(cell)
        if cell["status"] != "conflict":
            merged[col] = cell["value"]
    if has_conflict:
        entry["kind"] = "cell_conflict"
        entry["cells"] = cells
        entry["note"] = "双方同格异改，需逐格选择"
    else:
        entry["kind"] = "auto_keep"
        entry["merged"] = merged
        entry["note"] = "双方改动落在不同单元格，自动合并"


def _compute_state(
    columns: list[str], key_column: str, base: dict, left: dict, right: dict
) -> dict:
    base_keys = set(base["rows"])
    left_keys = set(left["rows"])
    # Output order: base rows in base order, then rows added by the left
    # table in left order, then rows added only by the right table.
    order = list(base["order"])
    order += [k for k in left["order"] if k not in base_keys]
    order += [
        k for k in right["order"] if k not in base_keys and k not in left_keys
    ]

    rows: dict[str, dict] = {}
    conflicts: list = []
    for key in order:
        b = base["rows"].get(key)
        l = left["rows"].get(key)
        r = right["rows"].get(key)
        entry = {
            "presence": {
                "base": b is not None,
                "left": l is not None,
                "right": r is not None,
            }
        }
        if b is not None:
            if l is None and r is None:
                entry["kind"] = "auto_drop"
                entry["note"] = "双方均删除该行"
            elif l is None:
                if r == b:
                    entry["kind"] = "auto_drop"
                    entry["note"] = "左表删除，右表未改"
                else:
                    entry["kind"] = "delete_conflict"
                    entry["deleted_side"] = "left"
                    entry["keep_row"] = r
                    entry["conflict_index"] = len(conflicts)
                    conflicts.append(
                        {
                            "type": "delete",
                            "key": key,
                            "deleted_side": "left",
                            "base": b,
                            "modified": r,
                        }
                    )
                    entry["note"] = "左表删除该行，右表修改了该行"
            elif r is None:
                if l == b:
                    entry["kind"] = "auto_drop"
                    entry["note"] = "右表删除，左表未改"
                else:
                    entry["kind"] = "delete_conflict"
                    entry["deleted_side"] = "right"
                    entry["keep_row"] = l
                    entry["conflict_index"] = len(conflicts)
                    conflicts.append(
                        {
                            "type": "delete",
                            "key": key,
                            "deleted_side": "right",
                            "base": b,
                            "modified": l,
                        }
                    )
                    entry["note"] = "右表删除该行，左表修改了该行"
            elif l == b and r == b:
                entry["kind"] = "auto_keep"
                entry["merged"] = b
                entry["note"] = "三方一致"
            elif l == b:
                entry["kind"] = "auto_keep"
                entry["merged"] = r
                entry["note"] = "仅右表修改"
            elif r == b:
                entry["kind"] = "auto_keep"
                entry["merged"] = l
                entry["note"] = "仅左表修改"
            elif l == r:
                entry["kind"] = "auto_keep"
                entry["merged"] = l
                entry["note"] = "双方相同改动"
            else:
                _cell_merge(key, b, l, r, columns, key_column, conflicts, entry)
        else:
            if l is not None and r is None:
                entry["kind"] = "auto_keep"
                entry["merged"] = l
                entry["note"] = "仅左表新增"
            elif r is not None and l is None:
                entry["kind"] = "auto_keep"
                entry["merged"] = r
                entry["note"] = "仅右表新增"
            elif l == r:
                entry["kind"] = "auto_keep"
                entry["merged"] = l
                entry["note"] = "双方相同新增"
            else:
                _cell_merge(key, None, l, r, columns, key_column, conflicts, entry)
        rows[key] = entry
    return {
        "columns": columns,
        "key_column": key_column,
        "row_order": order,
        "rows": rows,
        "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# Choices / progress helpers
# ---------------------------------------------------------------------------

def unresolved_indices(state: dict, choices: dict) -> list[int]:
    return [
        i for i in range(len(state["conflicts"])) if str(i) not in choices
    ]


def _validate_choices(state: dict, submitted: dict) -> dict:
    """Whitelist-check submitted choices against the frozen conflict list."""
    conflicts = state["conflicts"]
    choices: dict[str, str] = {}
    for raw_index, value in submitted.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            raise ServiceError("提交的选择无效") from None
        if index < 0 or index >= len(conflicts):
            raise ServiceError("提交的选择无效")
        allowed = (
            ("left", "right")
            if conflicts[index]["type"] == "cell"
            else ("keep", "drop")
        )
        if value not in allowed:
            raise ServiceError("提交的选择无效")
        choices[str(index)] = value
    return choices


def render_merged_csv(state: dict, choices: dict) -> str:
    """Render the final CSV text.  All conflicts must already be resolved."""
    columns = state["columns"]
    key_column = state["key_column"]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for key in state["row_order"]:
        entry = state["rows"][key]
        kind = entry["kind"]
        if kind == "auto_drop":
            continue
        if kind == "auto_keep":
            merged = entry["merged"]
        elif kind == "delete_conflict":
            if choices.get(str(entry["conflict_index"])) != "keep":
                continue
            merged = entry["keep_row"]
        else:  # cell_conflict
            merged = {key_column: key}
            for cell in entry["cells"]:
                if cell["status"] == "conflict":
                    pick = choices[str(cell["conflict_index"])]
                    merged[cell["column"]] = cell[pick]
                else:
                    merged[cell["column"]] = cell["value"]
        writer.writerow([merged[col] for col in columns])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------

def create_merge_draft(
    base_id: str, left_id: str, right_id: str, key_column: str
) -> str:
    """Validate the three tables, freeze the merge state, store the draft."""
    key_column = (key_column or "").strip()
    if not base_id or not left_id or not right_id:
        raise ServiceError("请选择基表与左右两份清洗表")
    if not key_column:
        raise ServiceError("请填写记录号列名")
    if left_id == right_id:
        raise ServiceError("左右两份清洗表不能是同一份")

    conn = db.get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        file_rows = {}
        for role, fid in (("基表", base_id), ("左表", left_id), ("右表", right_id)):
            row = db.get_file(conn, fid)
            if row is None:
                raise ServiceError(f"{role} {fid} 不存在")
            if row["kind"] != "cleaned":
                raise ServiceError(f"{role} {fid} 不是清洗表，三方合并只接受清洗表")
            problem = _availability_problem(conn, fid)
            if problem is not None:
                raise ServiceError(f"{role} {fid} {problem}，不能用于合并")
            file_rows[fid] = row

        for role, fid in (("左表", left_id), ("右表", right_id)):
            parents = [p["id"] for p in db.list_parents(conn, fid)]
            if parents != [base_id]:
                actual = "、".join(parents) or "无"
                raise ServiceError(
                    f"{role} {fid} 必须仅以基表 {base_id} 为父项"
                    f"（实际父项：{actual}）"
                )

        headers: dict[str, list[str]] = {}
        tables: dict[str, list[dict]] = {}
        for fid in (base_id, left_id, right_id):
            header, records = _load_table(file_rows[fid])
            headers[fid] = header
            tables[fid] = records

        base_columns = headers[base_id]
        if set(headers[left_id]) != set(base_columns) or set(
            headers[right_id]
        ) != set(base_columns):
            raise ServiceError("三份表的列名不一致，无法按键合并")
        if key_column not in base_columns:
            raise ServiceError(f"记录号列“{key_column}”不在表列名中")

        indexed = {
            fid: _index_by_key(fid, tables[fid], key_column)
            for fid in (base_id, left_id, right_id)
        }
        state = _compute_state(
            base_columns,
            key_column,
            indexed[base_id],
            indexed[left_id],
            indexed[right_id],
        )
        draft_id = db.next_merge_id(conn)
        db.insert_merge_draft(
            conn,
            draft_id,
            base_id,
            left_id,
            right_id,
            key_column,
            json.dumps(state, ensure_ascii=False),
        )
        conn.execute("COMMIT")
        return draft_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _update_choices_tx(conn, draft, seen_revision: str, submitted: dict):
    """Revision-checked choices update; caller holds the write lock."""
    if str(draft["revision"]) != str(seen_revision):
        raise ServiceError("草稿已被他人更新，页面修订号已过期，请刷新后重试")
    state = json.loads(draft["state_json"])
    choices = _validate_choices(state, submitted)
    cur = conn.execute(
        """
        UPDATE merge_drafts
        SET choices_json = ?, revision = revision + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ? AND revision = ? AND status = 'open'
        """,
        (json.dumps(choices, ensure_ascii=False), draft["id"], draft["revision"]),
    )
    if cur.rowcount != 1:
        raise ServiceError("草稿已被他人更新，页面修订号已过期，请刷新后重试")
    return state, choices


def save_merge_choices(draft_id: str, seen_revision: str, submitted: dict) -> int:
    """Persist conflict choices; returns the number of remaining conflicts."""
    conn = db.get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        draft = db.get_merge_draft(conn, draft_id)
        if draft is None:
            raise ServiceError("合并草稿不存在")
        if draft["status"] == "completed":
            raise ServiceError("草稿已完成，选择已锁定，不能再修改")
        state, choices = _update_choices_tx(conn, draft, seen_revision, submitted)
        conn.execute("COMMIT")
        return len(unresolved_indices(state, choices))
    except Exception:
        conn.execute("ROLLBACK")
        raise


def confirm_merge(draft_id: str, seen_revision: str, submitted: dict) -> dict:
    """Resolve, generate, register and complete — atomically.

    Returns a status dict:
      * ``confirmed``   — new cleaned file registered (``file_id``)
      * ``already``     — draft was already completed; same ``file_id`` returned
      * ``incomplete``  — choices saved but conflicts remain (``remaining``)
      * ``unavailable`` — choices saved but a source is gone (``message``)
    """
    conn = db.get_db()
    conn.execute("BEGIN IMMEDIATE")
    tmp_path: str | None = None
    final_path: str | None = None
    committed = False
    try:
        draft = db.get_merge_draft(conn, draft_id)
        if draft is None:
            raise ServiceError("合并草稿不存在")
        if draft["status"] == "completed":
            # Idempotent: concurrent or repeated confirmation gets the same file.
            conn.execute("COMMIT")
            committed = True
            return {"status": "already", "file_id": draft["result_id"]}

        state, choices = _update_choices_tx(conn, draft, seen_revision, submitted)

        remaining = unresolved_indices(state, choices)
        if remaining:
            conn.execute("COMMIT")
            committed = True
            return {"status": "incomplete", "remaining": len(remaining)}

        for role, fid in (
            ("基表", draft["base_id"]),
            ("左表", draft["left_id"]),
            ("右表", draft["right_id"]),
        ):
            problem = _availability_problem(conn, fid)
            if problem is not None:
                conn.execute("COMMIT")  # keep the saved choices
                committed = True
                return {
                    "status": "unavailable",
                    "message": f"{role} {fid} {problem}，拒绝确认",
                }

        payload = render_merged_csv(state, choices).encode("utf-8")

        staging_dir = current_app.config["STAGING_DIR"]
        storage_dir = current_app.config["STORAGE_DIR"]
        os.makedirs(staging_dir, exist_ok=True)
        os.makedirs(storage_dir, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=staging_dir, prefix=".merge-")
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
        # Merged products may legitimately contain only the header row.
        validate_csv(tmp_path, allow_header_only=True)

        file_id = db.next_file_id(conn, "cleaned")
        stored_name = f"{file_id}.csv"
        final_path = os.path.join(storage_dir, stored_name)
        os.rename(tmp_path, final_path)  # atomic within the volume
        tmp_path = None

        conn.execute(
            """
            INSERT INTO files
                (id, kind, filename, content_sha256, size_bytes, stored_name)
            VALUES (?, 'cleaned', ?, ?, ?, ?)
            """,
            (
                file_id,
                f"merged-{draft_id}.csv",
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                stored_name,
            ),
        )
        # Lineage: the merged table derives from the two corrected tables.
        for position, pid in enumerate((draft["left_id"], draft["right_id"])):
            conn.execute(
                "INSERT INTO dependencies(child_id, parent_id, position) "
                "VALUES (?, ?, ?)",
                (file_id, pid, position),
            )
        conn.execute(
            "INSERT INTO audit_events(file_id, action, detail) "
            "VALUES (?, 'merge', ?)",
            (
                file_id,
                f"draft={draft_id}; base={draft['base_id']}; "
                f"parents={draft['left_id']},{draft['right_id']}",
            ),
        )
        conn.execute(
            """
            UPDATE merge_drafts
            SET status = 'completed', result_id = ?, revision = revision + 1,
                completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (file_id, draft_id),
        )
        conn.execute("COMMIT")
        committed = True
        return {"status": "confirmed", "file_id": file_id}
    except sqlite3.Error as exc:
        raise ServiceError(f"数据库登记失败：{exc}") from exc
    finally:
        if not committed:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            # Staged file: failed before promotion.  Promoted file: the
            # transaction did not commit -> remove the orphan so no
            # half-registered product is ever visible.
            for path in (tmp_path, final_path):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        current_app.logger.exception("清理合并暂存文件失败: %s", path)


# ---------------------------------------------------------------------------
# View model for the worksheet template
# ---------------------------------------------------------------------------

def _disp(value):
    """Display wrapper distinguishing a missing row from an empty field."""
    if value is None:
        return {"text": "（缺行）", "cls": "v-missing"}
    if value == "":
        return {"text": "（空）", "cls": "v-empty"}
    return {"text": value, "cls": ""}


def draft_view(conn, draft) -> dict:
    state = json.loads(draft["state_json"])
    choices = json.loads(draft["choices_json"])

    conflicts_view = []
    for index, conflict in enumerate(state["conflicts"]):
        choice = choices.get(str(index))
        if conflict["type"] == "cell":
            conflicts_view.append(
                {
                    "index": index,
                    "type": "cell",
                    "key": conflict["key"],
                    "column": conflict["column"],
                    "base": _disp(conflict["base"]),
                    "left": _disp(conflict["left"]),
                    "right": _disp(conflict["right"]),
                    "choice": choice,
                }
            )
        else:
            side = conflict["deleted_side"]
            conflicts_view.append(
                {
                    "index": index,
                    "type": "delete",
                    "key": conflict["key"],
                    "deleted_label": _SIDE_LABEL[side],
                    "modified_label": _SIDE_LABEL[
                        "right" if side == "left" else "left"
                    ],
                    "rows": [
                        {
                            "column": col,
                            "base": _disp(conflict["base"][col]),
                            "modified": _disp(conflict["modified"][col]),
                        }
                        for col in state["columns"]
                    ],
                    "choice": choice,
                }
            )

    auto_rows = []
    counts = {"auto_keep": 0, "auto_drop": 0}
    for key in state["row_order"]:
        entry = state["rows"][key]
        kind = entry["kind"]
        if kind in counts:
            counts[kind] += 1
            auto_rows.append(
                {
                    "key": key,
                    "action": "保留" if kind == "auto_keep" else "删除",
                    "note": entry.get("note", ""),
                }
            )

    total_conflicts = len(state["conflicts"])
    resolved = total_conflicts - len(unresolved_indices(state, choices))

    sources = []
    for role, fid in (
        ("基表", draft["base_id"]),
        ("左表", draft["left_id"]),
        ("右表", draft["right_id"]),
    ):
        sources.append(
            {
                "role": role,
                "id": fid,
                "available": _availability_problem(conn, fid) is None,
            }
        )

    result = None
    if draft["result_id"]:
        result = db.get_file(conn, draft["result_id"])

    return {
        "conflicts": conflicts_view,
        "total_conflicts": total_conflicts,
        "resolved": resolved,
        "remaining": total_conflicts - resolved,
        "auto_rows": auto_rows,
        "auto_keep": counts["auto_keep"],
        "auto_drop": counts["auto_drop"],
        "total_keys": len(state["row_order"]),
        "sources": sources,
        "result": result,
    }
