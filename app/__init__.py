"""Flask application factory and HTTP routes."""

from __future__ import annotations

import json
import os

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import db, merge, services


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    instance = os.environ.get(
        "DATA_DIR", os.path.join(os.getcwd(), "data")
    )
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.path.join(instance, "stability.sqlite3"),
        STORAGE_DIR=os.path.join(instance, "objects"),
        STAGING_DIR=os.path.join(instance, "staging"),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
    )
    if config:
        app.config.update(config)

    db.init_app(app)
    with app.app_context():
        services.sweep_working_dirs()

    # -- pages -------------------------------------------------------------

    @app.route("/")
    def index():
        conn = db.get_db()
        files = db.list_all_files(conn)
        blocked = db.blocked_by_map(conn)
        rows = []
        for row in files:
            rows.append(
                {
                    "row": row,
                    "available": (not row["revoked"])
                    and row["id"] not in blocked,
                    "blocked_by": blocked.get(row["id"]),
                }
            )
        events = db.recent_events(conn)
        return render_template(
            "index.html",
            rows=rows,
            events=events,
            kinds=services.KINDS,
            kind_labels=services.KIND_LABELS,
        )

    @app.route("/files/<file_id>")
    def file_detail(file_id):
        conn = db.get_db()
        row = db.get_file(conn, file_id)
        if row is None:
            abort(404)
        blocker, blocker_name, blocker_path = db.find_blocking_ancestor(
            conn, file_id
        )
        available = (not row["revoked"]) and blocker is None
        parents = db.list_parents(conn, file_id)
        blocked_map = db.blocked_by_map(conn)
        blocked_parent_ids = {
            p["id"] for p in parents if p["id"] in blocked_map
        }
        children = db.list_children(conn, file_id)
        blocked_child_ids = {
            c["id"] for c in children if c["id"] in blocked_map
        }
        chains = [
            dict(r)
            for r in db.descendant_chains(conn, file_id)
            if r["descendant_id"] != file_id
        ]
        affected = sorted({r["descendant_id"] for r in chains})
        return render_template(
            "detail.html",
            row=row,
            available=available,
            blocker=blocker,
            blocker_name=blocker_name,
            blocker_path=blocker_path,
            parents=parents,
            children=children,
            blocked_parent_ids=blocked_parent_ids,
            blocked_child_ids=blocked_child_ids,
            chains=chains,
            affected=affected,
            kind_labels=services.KIND_LABELS,
        )

    # -- actions -----------------------------------------------------------

    @app.route("/upload", methods=["POST"])
    def upload():
        try:
            file_id = services.register_file(
                request.files.get("file"),
                request.form.get("kind", ""),
                request.form.get("parent_ids", ""),
            )
        except services.ServiceError as exc:
            flash(f"上传被拒绝：{exc}", "error")
            return redirect(url_for("index"))
        flash(f"登记成功，文件 ID 为 {file_id}", "success")
        return redirect(url_for("file_detail", file_id=file_id))

    @app.route("/files/<file_id>/revoke", methods=["POST"])
    def revoke(file_id):
        try:
            result = services.revoke_file(
                file_id, request.form.get("reason", "")
            )
        except services.ServiceError as exc:
            flash(f"撤销失败：{exc}", "error")
            return redirect(url_for("file_detail", file_id=file_id))
        if result["already_revoked"]:
            flash(f"文件 {file_id} 此前已被撤销", "notice")
        else:
            flash(
                f"文件 {file_id} 已撤销；受影响的派生文件 "
                f"{len(result['affected'])} 个："
                + (", ".join(result["affected"]) or "无"),
                "success",
            )
        return redirect(url_for("file_detail", file_id=file_id))

    @app.route("/files/<file_id>/download")
    def download(file_id):
        conn = db.get_db()
        row = db.get_file(conn, file_id)
        if row is None:
            abort(404)
        if row["revoked"]:
            flash(f"文件 {file_id} 已被撤销，禁止下载", "error")
            return redirect(url_for("file_detail", file_id=file_id))
        blocker, blocker_name, _ = db.find_blocking_ancestor(conn, file_id)
        if blocker is not None:
            flash(
                f"文件 {file_id} 的上游 {blocker}（{blocker_name}）"
                "已被撤销，本文件禁止下载",
                "error",
            )
            return redirect(url_for("file_detail", file_id=file_id))
        path = os.path.join(
            current_app.config["STORAGE_DIR"], row["stored_name"]
        )
        if not os.path.isfile(path):
            # Should never happen: registration is atomic.  Fail closed.
            flash("存储对象缺失，为安全起见禁止下载，请联系管理员", "error")
            return redirect(url_for("file_detail", file_id=file_id))
        return send_file(path, as_attachment=True, download_name=row["filename"])

    # -- three-way merge -----------------------------------------------------

    def _submitted_choices() -> dict:
        prefix = "choice-"
        return {
            name[len(prefix):]: value
            for name, value in request.form.items()
            if name.startswith(prefix)
        }

    @app.route("/merge")
    def merge_index():
        conn = db.get_db()
        blocked = db.blocked_by_map(conn)
        cleaned = [
            row
            for row in db.list_all_files(conn)
            if row["kind"] == "cleaned"
            and not row["revoked"]
            and row["id"] not in blocked
        ]
        drafts = []
        for draft in db.list_merge_drafts(conn):
            remaining = 0
            if draft["status"] == "open":
                state = json.loads(draft["state_json"])
                choices = json.loads(draft["choices_json"])
                remaining = len(merge.unresolved_indices(state, choices))
            drafts.append({"row": draft, "remaining": remaining})
        return render_template("merge.html", cleaned=cleaned, drafts=drafts)

    @app.route("/merge", methods=["POST"])
    def merge_create():
        try:
            draft_id = merge.create_merge_draft(
                request.form.get("base_id", ""),
                request.form.get("left_id", ""),
                request.form.get("right_id", ""),
                request.form.get("key_column", ""),
            )
        except services.ServiceError as exc:
            flash(f"创建合并草稿被拒绝：{exc}", "error")
            return redirect(url_for("merge_index"))
        flash(f"合并草稿 {draft_id} 已创建，请处理冲突后确认", "success")
        return redirect(url_for("merge_detail", draft_id=draft_id))

    @app.route("/merge/<draft_id>")
    def merge_detail(draft_id):
        conn = db.get_db()
        draft = db.get_merge_draft(conn, draft_id)
        if draft is None:
            abort(404)
        view = merge.draft_view(conn, draft)
        return render_template("merge_detail.html", draft=draft, view=view)

    @app.route("/merge/<draft_id>/save", methods=["POST"])
    def merge_save(draft_id):
        try:
            remaining = merge.save_merge_choices(
                draft_id,
                request.form.get("revision", ""),
                _submitted_choices(),
            )
        except services.ServiceError as exc:
            flash(f"保存被拒绝：{exc}", "error")
            return redirect(url_for("merge_detail", draft_id=draft_id))
        flash(f"选择已保存；剩余未解决冲突 {remaining} 处", "success")
        return redirect(url_for("merge_detail", draft_id=draft_id))

    @app.route("/merge/<draft_id>/confirm", methods=["POST"])
    def merge_confirm(draft_id):
        try:
            result = merge.confirm_merge(
                draft_id,
                request.form.get("revision", ""),
                _submitted_choices(),
            )
        except services.ServiceError as exc:
            flash(f"确认被拒绝：{exc}", "error")
            return redirect(url_for("merge_detail", draft_id=draft_id))
        status = result["status"]
        if status == "confirmed":
            flash(
                f"合并完成，成品清洗表 {result['file_id']} 已登记"
                "（父项为左右两表），草稿已锁定",
                "success",
            )
        elif status == "already":
            flash(
                f"该草稿此前已确认，成品文件为 {result['file_id']}（同一文件）",
                "notice",
            )
        elif status == "incomplete":
            flash(
                f"选择已保存；仍有 {result['remaining']} 处冲突未解决，不能确认",
                "error",
            )
        else:  # unavailable
            flash(f"确认被拒绝：{result['message']}", "error")
        return redirect(url_for("merge_detail", draft_id=draft_id))

    @app.errorhandler(413)
    def too_large(_err):
        flash("上传文件超过 64MB 限制", "error")
        return redirect(url_for("index"))

    @app.errorhandler(404)
    def not_found(_err):
        return render_template("404.html"), 404

    return app
