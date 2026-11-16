"""Flask application factory and HTTP routes."""

from __future__ import annotations

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

from . import db, services


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

    @app.errorhandler(413)
    def too_large(_err):
        flash("上传文件超过 64MB 限制", "error")
        return redirect(url_for("index"))

    @app.errorhandler(404)
    def not_found(_err):
        return render_template("404.html"), 404

    return app
