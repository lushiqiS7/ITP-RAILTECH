from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, flash,
)
from werkzeug.security import generate_password_hash
from config import SECRET_KEY, EDIT_ROLES, ADMIN_ROLES
from services.pm_engine import enrich_trains, enrich_train
from services.auth_service import (
    authenticate, register_user, change_password, update_profile,
    get_users, find_user_by_id, update_last_login,
    admin_update_user, admin_reset_password, admin_delete_user,
    validate_password_strength,
)
from services.audit_service import log_audit, get_audit_logs
from services.train_service import (
    load_trains, update_mileage_from_scan, manual_edit_train,
    mark_maintenance_completed, get_maintenance_history, get_override_records,
    get_review_queue, append_history, find_train, create_train, delete_train,
)
from services.data_store import load_json, now_str
from config import HISTORY_FILE
from utils.auth import login_required, admin_required, edit_required, api_edit_required

app = Flask(__name__)
app.secret_key = SECRET_KEY


def init_default_users():
    """Set default passwords for demo users on first run."""
    from services.auth_service import get_users, save_users
    users = get_users()
    defaults = {"admin": "Admin123!", "maint01": "Maint123!", "ops01": "Ops12345!"}
    changed = False
    for u in users:
        if "placeholder" in u.get("password_hash", ""):
            pwd = defaults.get(u["user_id"], "Password1")
            u["password_hash"] = generate_password_hash(pwd)
            changed = True
    if changed:
        save_users(users)


init_default_users()


def current_user():
    if "user_id" not in session:
        return None
    return find_user_by_id(session["user_id"])


@app.context_processor
def inject_globals():
    user = current_user()
    return {
        "current_user": user,
        "can_edit": session.get("role") in EDIT_ROLES,
        "is_admin": session.get("role") in ADMIN_ROLES,
    }


# ── Auth Routes ──────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    remembered = request.cookies.get("remembered_user", "")
    if request.method == "POST":
        user_input = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"

        user, error = authenticate(user_input, password)
        if error:
            flash(error, "danger")
            return render_template("auth/login.html", remembered_user=remembered)

        session["user_id"] = user["user_id"]
        session["role"] = user["role"]
        session["full_name"] = user["full_name"]
        update_last_login(user["user_id"])
        log_audit("User Login", user["user_id"], user["role"])

        resp = redirect(request.args.get("next") or url_for("dashboard"))
        if remember:
            resp.set_cookie("remembered_user", user["user_id"], max_age=30 * 24 * 3600)
        return resp

    return render_template("auth/login.html", remembered_user=remembered)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        ok, msg = register_user(
            request.form.get("full_name", "").strip(),
            request.form.get("user_id", "").strip(),
            request.form.get("email", "").strip(),
            request.form.get("password", ""),
            request.form.get("role", "operator"),
        )
        if ok:
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("login"))
        flash(msg, "danger")
    return render_template("auth/register.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        flash(f"If an account exists for {email}, reset instructions have been sent.", "info")
        return redirect(url_for("login"))
    return render_template("auth/forgot_password.html")


@app.route("/logout")
def logout():
    uid = session.get("user_id")
    role = session.get("role")
    if uid:
        log_audit("User Logout", uid, role)
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ── Dashboard ────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    sort_by = request.args.get("sort", "urgency")
    filter_status = request.args.get("filter", "all")
    search = request.args.get("search", "").strip()

    trains = load_trains()
    enriched = enrich_trains(trains, sort_by=sort_by)

    if filter_status == "overdue":
        enriched = [t for t in enriched if t["pm_status"] == "Overdue"]
    elif filter_status == "due_soon":
        enriched = [t for t in enriched if t["pm_status"] == "Due Soon"]
    elif filter_status == "ok":
        enriched = [t for t in enriched if t["pm_status"] == "OK"]
    elif filter_status == "review":
        enriched = [t for t in enriched if t["needs_manual_review"]]

    if search:
        enriched = [t for t in enriched if search.lower() in t["train_id"].lower()]

    all_enriched = enrich_trains(load_trains())
    kpis = {
        "total": len(all_enriched),
        "overdue": sum(1 for t in all_enriched if t["pm_status"] == "Overdue"),
        "due_soon": sum(1 for t in all_enriched if t["pm_status"] == "Due Soon"),
        "ok": sum(1 for t in all_enriched if t["pm_status"] == "OK"),
        "review": sum(1 for t in all_enriched if t["needs_manual_review"]),
    }

    history = load_json(HISTORY_FILE, default=[])
    latest_scan = history[0] if history else None

    return render_template(
        "dashboard.html",
        trains=enriched,
        kpis=kpis,
        history=history[:8],
        latest_scan=latest_scan,
        filter_status=filter_status,
        search=search,
        sort_by=sort_by,
    )


# ── Settings ─────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            ok, msg = update_profile(user["user_id"], {
                "full_name": request.form.get("full_name"),
                "email": request.form.get("email"),
                "phone": request.form.get("phone"),
            })
            flash(msg, "success" if ok else "danger")
        elif action == "password":
            if request.form.get("new_password") != request.form.get("confirm_password"):
                flash("New passwords do not match.", "danger")
            else:
                ok, msg = change_password(
                    user["user_id"],
                    request.form.get("current_password"),
                    request.form.get("new_password"),
                )
                flash(msg, "success" if ok else "danger")
        return redirect(url_for("settings"))
    return render_template("settings.html", user=user)


# ── Admin Routes ─────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_dashboard():
    users = get_users()
    audit_logs = get_audit_logs()[:20]
    review_queue = get_review_queue()
    maintenance = get_maintenance_history()[:20]
    overrides = get_override_records()[:20]
    return render_template(
        "admin/index.html",
        users=users,
        audit_logs=audit_logs,
        review_queue=review_queue,
        maintenance=maintenance,
        overrides=overrides,
    )


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        action = request.form.get("action")
        uid = request.form.get("user_id")
        if action == "create":
            from services.auth_service import register_user
            ok, msg = register_user(
                request.form.get("full_name"), uid,
                request.form.get("email"), request.form.get("password", "TempPass1"),
                request.form.get("role", "operator"),
            )
            flash(msg, "success" if ok else "danger")
        elif action == "update":
            admin_update_user(uid, {
                "full_name": request.form.get("full_name"),
                "email": request.form.get("email"),
                "role": request.form.get("role"),
                "status": request.form.get("status"),
            })
            flash("User updated.", "success")
        elif action == "reset_password":
            ok, msg = admin_reset_password(uid, request.form.get("new_password"))
            flash(msg, "success" if ok else "danger")
        elif action == "delete":
            admin_delete_user(uid)
            flash("User deleted.", "success")
        return redirect(url_for("admin_users"))
    return render_template("admin/users.html", users=get_users())


@app.route("/admin/audit-logs")
@admin_required
def admin_audit_logs():
    search = request.args.get("search", "")
    action_filter = request.args.get("action_type", "")
    logs = get_audit_logs({
        "search": search or None,
        "action_type": action_filter or None,
    })
    return render_template("admin/audit_logs.html", logs=logs, search=search, action_filter=action_filter)


@app.route("/admin/review-queue")
@admin_required
def admin_review_queue():
    return render_template("admin/review_queue.html", queue=get_review_queue())


@app.route("/admin/trains", methods=["GET", "POST"])
@admin_required
def admin_trains():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            ok, msg = create_train({
                "train_id": request.form.get("train_id"),
                "model": request.form.get("model"),
                "current_mileage": request.form.get("current_mileage"),
                "serviceability_status": request.form.get("serviceability_status"),
                "completed_milestones": request.form.get("completed_milestones", ""),
                "remarks": request.form.get("remarks", ""),
            }, session["user_id"], session.get("role"))
            flash(msg, "success" if ok else "danger")
        elif action == "delete":
            ok, msg = delete_train(request.form.get("train_id"), session["user_id"], session.get("role"))
            flash(msg, "success" if ok else "danger")
        return redirect(url_for("admin_trains"))
    trains = enrich_trains(load_trains())
    return render_template("admin/trains.html", trains=trains)


@app.route("/scan")
@login_required
def scan_page():
    trains = load_trains()
    train_ids = [t["train_id"] for t in trains]
    return render_template("scan.html", train_ids=train_ids)


@app.route("/maintenance-history")
@login_required
def maintenance_history():
    train_id = request.args.get("train_id")
    records = get_maintenance_history(train_id)
    return render_template("maintenance_history.html", records=records, train_id=train_id)


# ── API Routes ───────────────────────────────────────────────

@app.route("/api/dashboard-data")
@login_required
def api_dashboard_data():
    trains = enrich_trains(load_trains())
    history = load_json(HISTORY_FILE, default=[])
    all_trains = enrich_trains(load_trains())
    return jsonify({
        "trains": trains,
        "latest_scan": history[0] if history else None,
        "scan_history": history[:8],
        "kpis": {
            "total": len(all_trains),
            "overdue": sum(1 for t in all_trains if t["pm_status"] == "Overdue"),
            "due_soon": sum(1 for t in all_trains if t["pm_status"] == "Due Soon"),
            "ok": sum(1 for t in all_trains if t["pm_status"] == "OK"),
            "review": sum(1 for t in all_trains if t["needs_manual_review"]),
        },
    })


@app.route("/api/update-mileage", methods=["POST"])
def api_update_mileage():
    """Accept mileage updates from the camera scanner (no login required)."""
    data = request.get_json() or {}
    train_id = data.get("train_id")
    mileage = data.get("mileage")
    if not train_id or mileage is None:
        return jsonify({"success": False, "message": "Missing train_id or mileage"}), 400
    try:
        mileage = int(mileage)
    except ValueError:
        return jsonify({"success": False, "message": "Mileage must be numeric"}), 400

    ocr_conf = float(data.get("ocr_confidence", 0.95))
    qr_conf = float(data.get("qr_confidence", 0.95))
    user_id = session.get("user_id", "scanner")
    user_role = session.get("role", "system")
    ok, msg = update_mileage_from_scan(
        train_id, mileage, ocr_conf, qr_conf, user_id, user_role,
    )
    status = 200 if ok else 400
    return jsonify({"success": ok, "message": msg}), status


@app.route("/api/manual-edit", methods=["POST"])
@api_edit_required
def api_manual_edit():
    data = request.get_json() or {}
    train_id = data.get("train_id")
    field = data.get("field")
    new_value = data.get("new_value")
    reason = data.get("reason", "")
    if not train_id or not field or new_value is None:
        return jsonify({"success": False, "message": "Missing required fields"}), 400
    if field in ("current_mileage", "pm_status") and not reason:
        return jsonify({"success": False, "message": "Reason is required for this edit"}), 400
    ok, msg = manual_edit_train(train_id, field, new_value, reason,
                                session["user_id"], session.get("role"))
    train = enrich_train(find_train(train_id)) if ok else None
    return jsonify({"success": ok, "message": msg, "train": train})


@app.route("/api/maintenance-completed", methods=["POST"])
@api_edit_required
def api_maintenance_completed():
    data = request.get_json() or {}
    train_id = data.get("train_id")
    remarks = data.get("remarks", "")
    if not train_id:
        return jsonify({"success": False, "message": "Missing train_id"}), 400
    ok, result = mark_maintenance_completed(
        train_id, remarks, session["user_id"], session.get("role"),
    )
    if ok:
        return jsonify({"success": True, "message": "Maintenance marked as completed", "train": result})
    return jsonify({"success": False, "message": "Train not found"}), 404


@app.route("/api/trains")
def api_trains():
    return jsonify(enrich_trains(load_trains()))


@app.route("/api/trains/<train_id>")
@login_required
def api_train_detail(train_id):
    train = find_train(train_id)
    if not train:
        return jsonify({"success": False, "message": "Not found"}), 404
    return jsonify(enrich_train(train))


@app.route("/api/history")
@login_required
def api_history():
    return jsonify(load_json(HISTORY_FILE, default=[]))


@app.route("/api/undo-override", methods=["POST"])
@api_edit_required
def api_undo_override():
    from services.train_service import get_override_records, manual_edit_train
    data = request.get_json() or {}
    train_id = data.get("train_id")
    overrides = get_override_records(train_id)
    if not overrides:
        return jsonify({"success": False, "message": "No overrides to revert"}), 404
    latest = overrides[0]
    ok, msg = manual_edit_train(
        train_id, latest["field_changed"], latest["old_value"],
        "Revert of override " + latest["override_id"],
        session["user_id"], session.get("role"),
    )
    return jsonify({"success": ok, "message": msg})


@app.route("/api/admin/add-train", methods=["POST"])
@admin_required
def api_admin_add_train():
    data = request.get_json() or request.form
    ok, msg = create_train(data, session["user_id"], session.get("role"))
    train = enrich_train(find_train(data.get("train_id", "").strip().upper())) if ok else None
    return jsonify({"success": ok, "message": msg, "train": train}), (200 if ok else 400)


@app.route("/api/admin/delete-train", methods=["POST"])
@admin_required
def api_admin_delete_train():
    data = request.get_json() or {}
    train_id = data.get("train_id")
    if not train_id:
        return jsonify({"success": False, "message": "Missing train_id"}), 400
    ok, msg = delete_train(train_id, session["user_id"], session.get("role"))
    return jsonify({"success": ok, "message": msg})


@app.route("/api/scan-ocr", methods=["POST"])
@login_required
def api_scan_ocr():
    """Server-side OCR fallback: accepts base64 image, returns extracted digits."""
    import base64
    import re
    data = request.get_json() or {}
    image_data = data.get("image", "")
    if not image_data:
        return jsonify({"success": False, "message": "No image provided"}), 400
    try:
        import numpy as np
        import cv2
        import pytesseract
        from PIL import Image
        import io

        if "," in image_data:
            image_data = image_data.split(",", 1)[1]
        img_bytes = base64.b64decode(image_data)
        img = Image.open(io.BytesIO(img_bytes))
        frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        config = r"--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789"
        text = pytesseract.image_to_string(thresh, config=config).strip()
        digits = re.sub(r"[^0-9]", "", text)
        confidence = min(0.99, 0.65 + len(digits) * 0.04) if digits else 0.0
        return jsonify({"success": True, "mileage": digits, "confidence": confidence, "raw": text})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
