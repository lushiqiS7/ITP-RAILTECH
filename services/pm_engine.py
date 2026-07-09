from config import PM_CYCLES, DUE_SOON_THRESHOLD


def sync_milestones_for_mileage(completed_milestones, current_mileage):
    """Drop completed milestones the train has not physically reached."""
    return sorted(m for m in completed_milestones if m <= current_mileage)


def get_next_pm_by_mileage(current_mileage):
    """Return the next PM milestone threshold based on current mileage."""
    for cycle in PM_CYCLES:
        if current_mileage < cycle:
            return cycle
    cycles_beyond = current_mileage // 360000
    return 360000 * (cycles_beyond + 1)


def get_next_pm_milestone(completed_milestones, current_mileage):
    """Return the next uncompleted PM milestone for maintenance workflows."""
    for cycle in PM_CYCLES:
        if cycle not in completed_milestones:
            return cycle
    cycles_completed = len([m for m in completed_milestones if m >= 360000])
    return 360000 * (cycles_completed + 1)


def calculate_pm_status(current_mileage, next_pm_milestone):
    """Determine PM status based on mileage vs next milestone."""
    distance = next_pm_milestone - current_mileage
    if distance <= 0:
        return "Overdue", "red", distance
    elif distance <= DUE_SOON_THRESHOLD:
        return "Due Soon", "yellow", distance
    else:
        return "OK", "green", distance


def format_distance_message(status, distance, next_pm):
    """Operator-friendly distance wording."""
    abs_dist = abs(distance)
    formatted_pm = f"{next_pm:,} km"
    if status == "Overdue":
        return f"Overdue by {abs_dist:,} km past the {formatted_pm} PM milestone — service required"
    elif status == "Due Soon":
        return f"{abs_dist:,} km remaining to the {formatted_pm} PM milestone"
    else:
        return f"{abs_dist:,} km remaining to the {formatted_pm} PM milestone"


def get_next_action(status, next_pm, needs_review, scan_validity):
    """Determine the next required action for operators."""
    if needs_review or scan_validity == "Needs Manual Review":
        return "Awaiting manual verification"
    if status == "Overdue":
        return f"Immediate servicing required — {next_pm:,} km PM overdue"
    elif status == "Due Soon":
        return f"Schedule {next_pm:,} km inspection"
    return f"Continue monitoring — next PM at {next_pm:,} km"


def build_milestone_timeline(completed_milestones, current_mileage, next_pm):
    """Build milestone chips with completed/current/future states based on mileage."""
    timeline = []
    for cycle in PM_CYCLES:
        if current_mileage >= cycle:
            state = "completed"
        elif cycle == next_pm:
            state = "current"
        else:
            state = "future"
        timeline.append({
            "value": cycle,
            "label": _format_milestone_label(cycle),
            "state": state,
            "done": current_mileage >= cycle,
        })
    return timeline


def _format_milestone_label(value):
    if value >= 1000:
        return f"{value // 1000}K"
    return str(value)


def enrich_train(train):
    """Enrich a raw train record with computed PM fields."""
    current_mileage = train.get("current_mileage", train.get("total_distance", 0))
    completed = sync_milestones_for_mileage(
        train.get("completed_milestones", []), current_mileage,
    )
    next_pm = get_next_pm_by_mileage(current_mileage)
    status, color, distance = calculate_pm_status(current_mileage, next_pm)
    needs_review = train.get("needs_manual_review", False)
    scan_validity = train.get("scan_validity", "Valid")

    return {
        "train_id": train["train_id"],
        "model": train.get("model", "Unknown"),
        "current_mileage": current_mileage,
        "total_distance": current_mileage,
        "next_pm_milestone": next_pm,
        "next_pm": next_pm,
        "distance_to_pm": distance,
        "pm_status": status,
        "status": status,
        "color": color,
        "status_message": format_distance_message(status, distance, next_pm),
        "serviceability_status": train.get("serviceability_status", "Serviceable"),
        "ocr_confidence": train.get("ocr_confidence", 1.0),
        "qr_confidence": train.get("qr_confidence", 1.0),
        "scan_validity": scan_validity,
        "needs_manual_review": needs_review,
        "last_updated": train.get("last_updated", "N/A"),
        "last_pm_completed": train.get("last_pm_completed"),
        "completed_milestones": completed,
        "next_action": get_next_action(status, next_pm, needs_review, scan_validity),
        "checklist": build_milestone_timeline(completed, current_mileage, next_pm),
        "remarks": train.get("remarks", ""),
        "urgency_rank": {"Overdue": 0, "Due Soon": 1, "OK": 2}.get(status, 3),
    }


def enrich_trains(trains, sort_by="urgency"):
    """Enrich all trains and optionally sort."""
    enriched = [enrich_train(t) for t in trains]
    if sort_by == "urgency":
        enriched.sort(key=lambda t: (t["urgency_rank"], t["distance_to_pm"]))
    elif sort_by == "mileage":
        enriched.sort(key=lambda t: t["current_mileage"], reverse=True)
    elif sort_by == "last_updated":
        enriched.sort(key=lambda t: t["last_updated"], reverse=True)
    return enriched


def complete_maintenance(train, milestone, completed_by, remarks=""):
    """Mark a PM milestone as completed and advance to next."""
    completed = list(train.get("completed_milestones", []))
    if milestone not in completed:
        completed.append(milestone)
        completed.sort()
    train["completed_milestones"] = completed
    train["last_pm_completed"] = milestone
    if remarks:
        train["remarks"] = remarks
    return train
