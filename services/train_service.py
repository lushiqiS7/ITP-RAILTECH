from config import (
    TRAINS_FILE, HISTORY_FILE, MAINTENANCE_FILE, OVERRIDE_FILE,
    OCR_CONFIDENCE_THRESHOLD, QR_CONFIDENCE_THRESHOLD,
)
from services.data_store import load_json, save_json, now_str, new_id
from services.pm_engine import (
    enrich_train, complete_maintenance, get_next_pm_milestone, sync_milestones_for_mileage,
)
from services.audit_service import log_audit


def load_trains():
    return load_json(TRAINS_FILE, default=[])


def save_trains(trains):
    save_json(TRAINS_FILE, trains)


def find_train(train_id):
    for t in load_trains():
        if t["train_id"] == train_id:
            return t
    return None


def append_history(entry):
    history = load_json(HISTORY_FILE, default=[])
    history.insert(0, entry)
    save_json(HISTORY_FILE, history[:100])


def append_maintenance_record(record):
    records = load_json(MAINTENANCE_FILE, default=[])
    records.insert(0, record)
    save_json(MAINTENANCE_FILE, records)


def append_override(record):
    records = load_json(OVERRIDE_FILE, default=[])
    records.insert(0, record)
    save_json(OVERRIDE_FILE, records)


def update_mileage_from_scan(train_id, mileage, ocr_confidence, qr_confidence, user_id=None, user_role="system"):
    """Process an OCR/QR scan update with confidence gating."""
    trains = load_trains()
    timestamp = now_str()

    for train in trains:
        if train["train_id"] != train_id:
            continue

        previous = train.get("current_mileage", train.get("total_distance", 0))
        needs_review = False
        scan_validity = "Valid"
        result = "Accepted"
        reason = "Mileage updated successfully"

        if ocr_confidence < OCR_CONFIDENCE_THRESHOLD:
            needs_review = True
            scan_validity = "Low Confidence"
            result = "Needs Review"
            reason = f"OCR confidence {ocr_confidence:.0%} below threshold"

        if qr_confidence < QR_CONFIDENCE_THRESHOLD:
            needs_review = True
            scan_validity = "Needs Manual Review"
            result = "Needs Review"
            reason = f"QR confidence {qr_confidence:.0%} below threshold"

        if mileage <= previous:
            append_history({
                "timestamp": timestamp, "train_id": train_id,
                "scanned_mileage": mileage, "previous_mileage": previous,
                "ocr_confidence": ocr_confidence, "qr_confidence": qr_confidence,
                "result": "Rejected", "reason": "Mileage not higher than previous value",
            })
            return False, f"Rejected: {mileage} is not greater than existing {previous}"

        if not needs_review:
            train["current_mileage"] = mileage
            train["total_distance"] = mileage
            train["last_updated"] = timestamp
            train["ocr_confidence"] = ocr_confidence
            train["qr_confidence"] = qr_confidence
            train["scan_validity"] = scan_validity
            train["needs_manual_review"] = False
            save_trains(trains)
            log_audit("OCR Correction", user_id or "system", user_role or "system",
                      affected_id=train_id, old_value=previous, new_value=mileage)
        else:
            train["ocr_confidence"] = ocr_confidence
            train["qr_confidence"] = qr_confidence
            train["scan_validity"] = scan_validity
            train["needs_manual_review"] = True
            train["last_updated"] = timestamp
            save_trains(trains)

        append_history({
            "timestamp": timestamp, "train_id": train_id,
            "scanned_mileage": mileage, "previous_mileage": previous,
            "ocr_confidence": ocr_confidence, "qr_confidence": qr_confidence,
            "result": result, "reason": reason,
        })
        return True, reason if needs_review else f"{train_id} updated to {mileage:,} km"

    append_history({
        "timestamp": timestamp, "train_id": train_id,
        "scanned_mileage": mileage, "previous_mileage": None,
        "result": "Rejected", "reason": "Train ID not found",
    })
    return False, f"Train ID {train_id} not found"


def manual_edit_train(train_id, field, new_value, reason, user_id, user_role):
    """Manual override for train fields."""
    trains = load_trains()
    for train in trains:
        if train["train_id"] != train_id:
            continue
        old_value = train.get(field)
        if field in ("current_mileage", "total_distance"):
            new_mileage = int(new_value)
            train["current_mileage"] = new_mileage
            train["total_distance"] = new_mileage
            synced = sync_milestones_for_mileage(
                train.get("completed_milestones", []), new_mileage,
            )
            train["completed_milestones"] = synced
            train["last_pm_completed"] = synced[-1] if synced else None
            field = "current_mileage"
        elif field == "serviceability_status":
            train["serviceability_status"] = new_value
        elif field == "pm_status":
            train["pm_status_override"] = new_value
        elif field == "needs_manual_review":
            train["needs_manual_review"] = new_value in (True, "true", "1")
            if not train["needs_manual_review"]:
                train["scan_validity"] = "Valid"
        else:
            train[field] = new_value

        train["last_updated"] = now_str()
        if reason:
            train["remarks"] = reason
        save_trains(trains)

        override = {
            "override_id": new_id("OVR-"),
            "train_id": train_id,
            "field_changed": field,
            "old_value": str(old_value),
            "new_value": str(new_value),
            "changed_by": user_id,
            "changed_at": now_str(),
            "reason": reason,
        }
        append_override(override)

        action_map = {
            "current_mileage": "Manual Mileage Edit",
            "serviceability_status": "Manual Serviceability Change",
            "pm_status": "PM Status Update",
            "needs_manual_review": "Manual Review Confirmation",
        }
        log_audit(action_map.get(field, "Admin Override"), user_id, user_role,
                  affected_id=train_id, old_value=old_value, new_value=new_value, remarks=reason)
        return True, "Record updated successfully"
    return False, "Train not found"


def mark_maintenance_completed(train_id, remarks, user_id, user_role):
    """Complete current PM milestone and advance."""
    trains = load_trains()
    for train in trains:
        if train["train_id"] != train_id:
            continue

        mileage = train.get("current_mileage", train.get("total_distance", 0))
        completed = train.get("completed_milestones", [])
        milestone = get_next_pm_milestone(completed, mileage)

        old_completed = list(completed)
        train = complete_maintenance(train, milestone, user_id, remarks)
        train["last_updated"] = now_str()
        save_trains(trains)

        record = {
            "record_id": new_id("MNT-"),
            "train_id": train_id,
            "pm_milestone": milestone,
            "completed_by": user_id,
            "completed_at": now_str(),
            "remarks": remarks,
            "status": "completed",
            "mileage_at_completion": mileage,
        }
        append_maintenance_record(record)
        log_audit("Maintenance Completed", user_id, user_role,
                  affected_id=train_id, old_value=old_completed,
                  new_value=train["completed_milestones"], remarks=remarks)

        enriched = enrich_train(train)
        return True, enriched
    return False, None


def get_maintenance_history(train_id=None):
    records = load_json(MAINTENANCE_FILE, default=[])
    if train_id:
        return [r for r in records if r.get("train_id") == train_id]
    return records


def get_override_records(train_id=None):
    records = load_json(OVERRIDE_FILE, default=[])
    if train_id:
        return [r for r in records if r.get("train_id") == train_id]
    return records


def get_review_queue():
    trains = load_trains()
    return [enrich_train(t) for t in trains if t.get("needs_manual_review")]


def create_train(data, user_id, user_role):
    """Admin: manually add a new train record."""
    train_id = data.get("train_id", "").strip().upper()
    if not train_id:
        return False, "Train ID is required"
    if find_train(train_id):
        return False, f"Train ID {train_id} already exists"

    try:
        mileage = int(data.get("current_mileage", 0))
    except (ValueError, TypeError):
        return False, "Mileage must be a valid number"

    completed = data.get("completed_milestones", [])
    if isinstance(completed, str):
        completed = [int(x.strip()) for x in completed.split(",") if x.strip().isdigit()]

    train = {
        "train_id": train_id,
        "model": data.get("model", "2-Car LRV").strip() or "2-Car LRV",
        "current_mileage": mileage,
        "total_distance": mileage,
        "completed_milestones": sorted(completed),
        "serviceability_status": data.get("serviceability_status", "Serviceable"),
        "ocr_confidence": 1.0,
        "qr_confidence": 1.0,
        "scan_validity": "Valid",
        "needs_manual_review": False,
        "last_updated": now_str(),
        "last_pm_completed": completed[-1] if completed else None,
        "remarks": data.get("remarks", "Manually added by admin"),
    }

    trains = load_trains()
    trains.append(train)
    save_trains(trains)
    log_audit("Admin Override", user_id, user_role,
              affected_id=train_id, new_value=f"New train {train_id} at {mileage:,} km",
              remarks=data.get("remarks", "Manually added train record"))
    return True, f"Train {train_id} added successfully"


def delete_train(train_id, user_id, user_role):
    """Admin: remove a train record."""
    trains = load_trains()
    original_len = len(trains)
    trains = [t for t in trains if t["train_id"] != train_id]
    if len(trains) == original_len:
        return False, "Train not found"
    save_trains(trains)
    log_audit("Admin Override", user_id, user_role,
              affected_id=train_id, remarks="Train record deleted")
    return True, f"Train {train_id} deleted"

