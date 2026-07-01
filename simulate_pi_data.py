"""Capture a train scan on Raspberry Pi and send it to the Flask server.

The script prefers Picamera2 on Raspberry Pi OS and falls back to OpenCV video
capture if Picamera2 is unavailable. It extracts the hubometer train ID from
the upper display area and the mileage from the lower counter, then posts the
JSON payload to the Flask API.

Usage examples:
    python simulate_pi_data.py --server-url http://10.132.31.118:5000
    python simulate_pi_data.py --server-url http://10.132.31.118:5000 --once --display
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import time
from datetime import datetime

import cv2
import requests
from PIL import Image
import pytesseract

try:
    Picamera2 = getattr(importlib.import_module("picamera2"), "Picamera2")
except Exception:  # pragma: no cover - optional on non-Pi systems
    Picamera2 = None


DEFAULT_SERVER_URL = os.environ.get("FLASK_API_URL", "http://127.0.0.1:5000")
TRAIN_ID_PATTERN = re.compile(r"\b(?:\d{3}[-\s]?\d{4})\b")
MILEAGE_PATTERN = re.compile(r"\b\d{7}\b")
TRAIN_ROI_FRACTION = (0.32, 0.58, 0.72, 0.92)
MILEAGE_ROI_FRACTION = (0.50, 0.74, 0.62, 0.94)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Pi OCR results to Flask")
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help="Flask server base URL, for example http://192.168.1.20:5000",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between capture attempts",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Capture one frame, send once, then exit",
    )
    parser.add_argument(
        "--display",
        action="store_true",
        help="Show live preview windows while capturing",
    )
    parser.add_argument(
        "--camera-index",
        type=int,
        default=0,
        help="OpenCV camera index used when Picamera2 is unavailable",
    )
    parser.add_argument(
        "--tesseract-cmd",
        default=os.environ.get("TESSERACT_CMD", ""),
        help="Optional path to the Tesseract executable",
    )
    return parser.parse_args()


def configure_tesseract(tesseract_cmd: str) -> None:
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd


def create_camera(camera_index: int):
    if Picamera2 is not None:
        camera = Picamera2()
        config = camera.create_preview_configuration(main={"size": (1280, 720), "format": "RGB888"})
        camera.configure(config)
        camera.start()
        return camera, "picamera2"

    capture = cv2.VideoCapture(camera_index)
    if not capture.isOpened():
        raise RuntimeError("Could not open camera")
    return capture, "opencv"


def read_frame(camera, backend: str):
    if backend == "picamera2":
        frame = camera.capture_array()
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    ok, frame = camera.read()
    if not ok:
        raise RuntimeError("Could not read frame")
    return frame


def preprocess_for_ocr(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, threshold = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return threshold


def ocr_text(image, numeric_only=False, whitelist=None, psm=None):
    processed = preprocess_for_ocr(image)
    if psm is None:
        psm = 7 if numeric_only or whitelist else 6

    config = f"--oem 3 --psm {psm}"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    elif numeric_only:
        config += " -c tessedit_char_whitelist=0123456789"

    text = pytesseract.image_to_string(Image.fromarray(processed), config=config)
    return text.strip()


def extract_train_id(text_candidates):
    for text in text_candidates:
        match = TRAIN_ID_PATTERN.search(text or "")
        if match:
            digits = re.sub(r"\D", "", match.group(0))
            if len(digits) == 7:
                return f"{digits[:3]}-{digits[3:]}"
            return match.group(0).upper().replace(" ", "-")
    return None


def extract_mileage(text_candidates):
    best_value = None
    best_length = 0

    for text in text_candidates:
        for match in MILEAGE_PATTERN.finditer(text or ""):
            token = match.group(0).lstrip("0") or "0"
            if len(token) > best_length:
                best_value = int(token)
                best_length = len(token)
            elif len(token) == best_length and best_value is not None and int(token) > best_value:
                best_value = int(token)

    return best_value


def build_train_id_roi(frame):
    height, width = frame.shape[:2]
    y1, y2, x1, x2 = TRAIN_ROI_FRACTION
    return frame[
        int(height * y1):int(height * y2),
        int(width * x1):int(width * x2),
    ]


def build_mileage_roi(frame):
    height, width = frame.shape[:2]
    y1, y2, x1, x2 = MILEAGE_ROI_FRACTION
    return frame[
        int(height * y1):int(height * y2),
        int(width * x1):int(width * x2),
    ]


def roi_bounds(frame, fractions):
    height, width = frame.shape[:2]
    y1, y2, x1, x2 = fractions
    return (
        int(width * x1),
        int(height * y1),
        int(width * x2),
        int(height * y2),
    )


def send_payload(server_url, payload):
    response = requests.post(f"{server_url}/api/submit-scan", json=payload, timeout=10)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    return response.status_code, body


def main() -> int:
    args = parse_args()
    configure_tesseract(args.tesseract_cmd)

    camera, backend = create_camera(args.camera_index)
    last_sent = None
    last_attempt = 0.0

    try:
        while True:
            frame = read_frame(camera, backend)
            train_roi = build_train_id_roi(frame)
            mileage_roi = build_mileage_roi(frame)

            train_id_raw = ocr_text(train_roi, whitelist="0123456789-", psm=7)
            train_id_fallback = ocr_text(frame, whitelist="0123456789-", psm=6)
            mileage_raw = ocr_text(mileage_roi, numeric_only=True, psm=7)
            mileage_fallback = ocr_text(frame, numeric_only=True, psm=6)

            train_id = extract_train_id([train_id_raw, train_id_fallback])
            mileage = extract_mileage([mileage_raw, mileage_fallback])

            now = time.time()
            if now - last_attempt >= args.interval:
                last_attempt = now

                if train_id and mileage is not None:
                    payload = {
                        "train_id": train_id,
                        "mileage": mileage,
                        "source": "pi_ocr",
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    current_key = (train_id, mileage)

                    if current_key != last_sent:
                        status_code, body = send_payload(args.server_url, payload)
                        print("Sent:", payload)
                        print("Status:", status_code)
                        print("Response:", body)
                        print("-" * 50)

                        if status_code == 200:
                            last_sent = current_key
                else:
                    print("Waiting for valid OCR:", {"train_id": train_id, "mileage": mileage})

            if args.display:
                display_frame = frame.copy()
                train_box = roi_bounds(display_frame, TRAIN_ROI_FRACTION)
                mileage_box = roi_bounds(display_frame, MILEAGE_ROI_FRACTION)
                cv2.rectangle(display_frame, (train_box[0], train_box[1]), (train_box[2], train_box[3]), (0, 255, 0), 2)
                cv2.rectangle(display_frame, (mileage_box[0], mileage_box[1]), (mileage_box[2], mileage_box[3]), (0, 255, 255), 2)

                label = train_id if train_id else (train_id_raw or "Scanning...")
                cv2.putText(display_frame, f"Hubometer ID: {label}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                cv2.putText(display_frame, f"Mileage: {mileage if mileage is not None else '...'}", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
                cv2.putText(display_frame, f"Train OCR: {train_id_raw or '-'}", (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(display_frame, f"Mileage OCR: {mileage_raw or '-'}", (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                cv2.imshow("Pi Sender", display_frame)

                train_preview = cv2.resize(train_roi, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                mileage_preview = cv2.resize(mileage_roi, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                cv2.imshow("Train ROI", train_preview)
                cv2.imshow("Mileage ROI", mileage_preview)

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            if args.once:
                break

    except KeyboardInterrupt:
        pass
    finally:
        if backend == "picamera2":
            camera.stop()
        else:
            camera.release()

        if args.display:
            cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())