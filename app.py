from flask import Flask, render_template, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

TRAINS_FILE = os.path.join("data", "trains.json")
HISTORY_FILE = os.path.join("data", "scan_history.json")

PM_CYCLES = [2000, 13000, 40000, 120000, 360000]
PM_WARNING_KM = 5000
MILESTONE_LABELS = {
    2000: "2K",
    13000: "13K",
    40000: "40K",
    120000: "120K",
    360000: "360K",
}


def load_json(file_path):
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r") as f:
        return json.load(f)


def save_json(file_path, data):
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)


def load_trains():
    return load_json(TRAINS_FILE)


def save_trains(trains):
    save_json(TRAINS_FILE, trains)


def load_history():
    return load_json(HISTORY_FILE)


def save_history(history):
    save_json(HISTORY_FILE, history)


def append_history(entry):
    history = load_history()
    history.insert(0, entry)
    history = history[:30]
    save_history(history)


def handle_scan_update(data):
    if not data:
        return jsonify({"success": False, "message": "No JSON received"}), 400

    train_id = data.get("train_id")
    mileage = data.get("mileage")
    source = data.get("source", "pi_ocr")
    timestamp = data.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not train_id or mileage is None:
        return jsonify({"success": False, "message": "Missing train_id or mileage"}), 400

    try:
        mileage = int(mileage)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Mileage must be numeric"}), 400

    trains = load_trains()

    for train in trains:
        if train["train_id"] == train_id:
            previous_mileage = train["total_distance"]

            if mileage <= previous_mileage:
                log_entry = {
                    "timestamp": timestamp,
                    "train_id": train_id,
                    "scanned_mileage": mileage,
                    "previous_mileage": previous_mileage,
                    "result": "Rejected",
                    "reason": "Mileage not higher than previous value",
                    "source": source
                }
                append_history(log_entry)

                return jsonify({
                    "success": False,
                    "message": f"Rejected: {mileage} is not greater than existing mileage {previous_mileage}"
                }), 400

            train["total_distance"] = mileage
            train["last_updated"] = timestamp
            if data.get("ocr_confidence") is not None:
                train["ocr_confidence"] = data.get("ocr_confidence")
            save_trains(trains)

            log_entry = {
                "timestamp": timestamp,
                "train_id": train_id,
                "scanned_mileage": mileage,
                "previous_mileage": previous_mileage,
                "result": "Accepted",
                "reason": "Mileage updated successfully",
                "source": source
            }
            append_history(log_entry)

            return jsonify({
                "success": True,
                "message": f"{train_id} updated successfully",
                "train_id": train_id,
                "previous_mileage": previous_mileage,
                "new_mileage": mileage,
                "last_updated": timestamp,
                "source": source
            })

    log_entry = {
        "timestamp": timestamp,
        "train_id": train_id,
        "scanned_mileage": mileage,
        "previous_mileage": None,
        "result": "Rejected",
        "reason": "Train ID not found",
        "source": source
    }
    append_history(log_entry)

    return jsonify({
        "success": False,
        "message": f"Train ID {train_id} not found"
    }), 404


def get_next_pm_ahead(total_distance):
    """Smallest PM milestone strictly ahead of current mileage."""
    for cycle in PM_CYCLES:
        if total_distance < cycle:
            return cycle

    block = 360000
    completed_blocks = total_distance // block
    return (completed_blocks + 1) * block


def get_predecessor_milestone(milestone):
    predecessor = 0
    for cycle in PM_CYCLES:
        if cycle < milestone:
            predecessor = cycle
    if milestone > PM_CYCLES[-1]:
        predecessor = max(predecessor, (milestone // 360000 - 1) * 360000)
    return predecessor


def format_milestone_label(km):
    return MILESTONE_LABELS.get(km, f"{km:,} km")


def evaluate_pm_status(total_distance):
    """
    PM status rules:
    - Due Soon: within PM_WARNING_KM of the next milestone ahead
    - Overdue: passed the previous PM milestone without service reset
      (within grace window and still before the next milestone ahead)
    - OK: otherwise
    """
    next_ahead = get_next_pm_ahead(total_distance)
    predecessor = get_predecessor_milestone(next_ahead)
    distance_remaining = next_ahead - total_distance
    overdue_by_km = 0
    pm_target = next_ahead

    if total_distance in PM_CYCLES:
        overdue_by_km = 0
        pm_target = total_distance
        status, color = "Overdue", "red"
        action_message = (
            f"At the {format_milestone_label(pm_target)} PM milestone — "
            "service required immediately."
        )
        return {
            "next_pm": next_ahead,
            "pm_target": pm_target,
            "distance_remaining": 0,
            "overdue_by_km": overdue_by_km,
            "status": status,
            "color": color,
            "status_priority": 0,
            "action_message": action_message,
            "distance_display": "At PM milestone",
            "progress_start": predecessor,
            "progress_end": next_ahead,
        }

    if 0 < distance_remaining <= PM_WARNING_KM:
        status, color = "Due Soon", "yellow"
        action_message = (
            f"{distance_remaining:,} km remaining to the "
            f"{format_milestone_label(next_ahead)} PM milestone."
        )
        distance_display = f"{distance_remaining:,} km remaining"
        status_priority = 1
    else:
        in_grace_after_pred = (
            predecessor > 0
            and total_distance > predecessor
            and total_distance <= predecessor + PM_WARNING_KM
        )
        due_soon_start = next_ahead - PM_WARNING_KM
        grace_overlaps_next_window = due_soon_start <= predecessor + PM_WARNING_KM

        if in_grace_after_pred and (
            grace_overlaps_next_window or distance_remaining > PM_WARNING_KM
        ):
            pm_target = predecessor
            overdue_by_km = total_distance - predecessor
            status, color = "Overdue", "red"
            action_message = (
                f"{overdue_by_km:,} km past the "
                f"{format_milestone_label(predecessor)} PM milestone — "
                "service required."
            )
            distance_display = f"Overdue by {overdue_by_km:,} km"
            status_priority = 0
        else:
            status, color = "OK", "green"
            action_message = (
                f"{distance_remaining:,} km remaining to the "
                f"{format_milestone_label(next_ahead)} PM milestone."
            )
            distance_display = f"{distance_remaining:,} km remaining"
            status_priority = 2

    return {
        "next_pm": next_ahead,
        "pm_target": pm_target,
        "distance_remaining": distance_remaining,
        "overdue_by_km": overdue_by_km,
        "status": status,
        "color": color,
        "status_priority": status_priority,
        "action_message": action_message,
        "distance_display": distance_display,
        "progress_start": predecessor,
        "progress_end": next_ahead,
    }


def get_serviceability(train, pm_status):
    stored = train.get("serviceability")
    if stored in ("Serviceable", "Limited", "Not Serviceable"):
        return stored

    if pm_status == "Overdue":
        return "Not Serviceable"
    if pm_status == "Due Soon":
        return "Limited"
    return "Serviceable"


def build_milestone_checklist(total_distance, pm_target, next_ahead, status):
    checklist = []
    for value in PM_CYCLES:
        label = MILESTONE_LABELS[value]
        if total_distance >= value:
            state = "completed"
        elif value == pm_target and status == "Overdue":
            state = "overdue"
        elif value == next_ahead:
            state = "current"
        else:
            state = "future"

        checklist.append({
            "label": label,
            "value": value,
            "state": state,
            "done": state == "completed",
        })

    return checklist


def enrich_train_data(trains):
    enriched = []

    for train in trains:
        total_distance = train["total_distance"]
        pm = evaluate_pm_status(total_distance)
        serviceability = get_serviceability(train, pm["status"])
        checklist = build_milestone_checklist(
            total_distance,
            pm["pm_target"],
            pm["next_pm"],
            pm["status"],
        )

        cycle_span = pm["progress_end"] - pm["progress_start"]
        if cycle_span > 0:
            progress_pct = ((total_distance - pm["progress_start"]) / cycle_span) * 100
            progress_pct = max(0, min(progress_pct, 100))
        else:
            progress_pct = 0

        enriched.append({
            "train_id": train["train_id"],
            "model": train["model"],
            "total_distance": total_distance,
            "next_pm": pm["next_pm"],
            "pm_target": pm["pm_target"],
            "distance_to_pm": pm["distance_remaining"],
            "distance_remaining": pm["distance_remaining"],
            "overdue_by_km": pm["overdue_by_km"],
            "distance_display": pm["distance_display"],
            "action_message": pm["action_message"],
            "status": pm["status"],
            "color": pm["color"],
            "status_priority": pm["status_priority"],
            "checklist": checklist,
            "progress_pct": round(progress_pct, 1),
            "progress_start": pm["progress_start"],
            "progress_end": pm["progress_end"],
            "serviceability": serviceability,
            "ocr_confidence": train.get("ocr_confidence"),
            "last_updated": train.get("last_updated", "N/A"),
        })

    enriched.sort(key=lambda item: (item["status_priority"], item["distance_remaining"]))
    return enriched


@app.route("/")
def dashboard():
    trains = load_trains()
    enriched_trains = enrich_train_data(trains)
    history = load_history()
    latest_scan = history[0] if history else None

    return render_template(
        "dashboard.html",
        trains=enriched_trains,
        history=history[:8],
        latest_scan=latest_scan,
        pm_warning_km=PM_WARNING_KM,
    )


@app.route("/api/update-mileage", methods=["POST"])
def update_mileage():
    return handle_scan_update(request.get_json(silent=True))


@app.route("/api/submit-scan", methods=["POST"])
def submit_scan():
    return handle_scan_update(request.get_json(silent=True))


@app.route("/api/trains", methods=["GET"])
def get_trains():
    return jsonify(load_trains())


@app.route("/api/history", methods=["GET"])
def get_history():
    return jsonify(load_history())


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)