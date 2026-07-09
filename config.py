import os

SECRET_KEY = os.environ.get("SECRET_KEY", "lrt-dashboard-dev-secret-key-change-in-prod")

PM_CYCLES = [2000, 13000, 40000, 120000, 360000]
DUE_SOON_THRESHOLD = 5000
OCR_CONFIDENCE_THRESHOLD = 0.75
QR_CONFIDENCE_THRESHOLD = 0.80

DATA_DIR = "data"
TRAINS_FILE = os.path.join(DATA_DIR, "trains.json")
HISTORY_FILE = os.path.join(DATA_DIR, "scan_history.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
MAINTENANCE_FILE = os.path.join(DATA_DIR, "maintenance_records.json")
AUDIT_FILE = os.path.join(DATA_DIR, "audit_logs.json")
OVERRIDE_FILE = os.path.join(DATA_DIR, "manual_override_records.json")

ROLES = ["admin", "maintenance", "operator", "viewer"]
EDIT_ROLES = ["admin", "maintenance"]
ADMIN_ROLES = ["admin"]
