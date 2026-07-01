import re
import time
from urllib.parse import urlsplit, urlunsplit

import cv2
from PIL import Image
from pytesseract import Output
import pytesseract
import requests


pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

FLASK_API_URL = "http://127.0.0.1:5000"
TRAIN_ID_PATTERN = re.compile(r"\b(?:\d{3}[-\s]?\d{4})\b")
KNOWN_TRAIN_IDS = {"650-0610", "650-0611", "650-0612", "650-0613", "650-0614"}

SCAN_INTERVAL = 1.0
SCREEN_MIN_AREA_RATIO = 0.03

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Error: Could not open camera")
    raise SystemExit(1)

last_train_id = ""
last_train_ocr = ""
last_mileage_ocr = ""
last_scan_time = 0.0
last_sent_result = ""


def clamp_bounds(bounds, frame_shape, margin=0):
    x1, y1, x2, y2 = bounds
    height, width = frame_shape[:2]
    return (
        max(x1 - margin, 0),
        max(y1 - margin, 0),
        min(x2 + margin, width),
        min(y2 + margin, height),
    )


def find_screen_region(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    bright_mask = cv2.inRange(blurred, 140, 255)
    bright_mask = cv2.morphologyEx(
        bright_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)),
    )
    bright_mask = cv2.morphologyEx(
        bright_mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
    )

    contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    height, width = frame.shape[:2]
    min_area = height * width * SCREEN_MIN_AREA_RATIO
    best_bounds = None
    best_score = None

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue

        x, y, w, h = cv2.boundingRect(contour)
        aspect = w / max(h, 1)
        fill = area / max(w * h, 1)

        if aspect < 0.45 or aspect > 1.8:
            continue
        if fill < 0.30:
            continue

        score = area * fill
        if best_score is None or score > best_score:
            best_score = score
            best_bounds = (x, y, x + w, y + h)

    return best_bounds


def crop_region(frame, bounds, inset_x=0.06, inset_y=0.08):
    x1, y1, x2, y2 = bounds
    width = x2 - x1
    height = y2 - y1

    left = int(x1 + width * inset_x)
    right = int(x2 - width * inset_x)
    top = int(y1 + height * inset_y)
    bottom = int(y2 - height * inset_y)

    left = max(left, 0)
    top = max(top, 0)
    right = min(right, frame.shape[1])
    bottom = min(bottom, frame.shape[0])

    if right <= left or bottom <= top:
        return frame, (0, 0, frame.shape[1], frame.shape[0])

    return frame[top:bottom, left:right], (left, top, right, bottom)


def split_upper_lower(screen_roi):
    height, width = screen_roi.shape[:2]
    x1 = int(width * 0.16)
    x2 = int(width * 0.84)

    upper_y1 = int(height * 0.18)
    upper_y2 = int(height * 0.46)
    lower_y1 = int(height * 0.50)
    lower_y2 = int(height * 0.83)

    upper = screen_roi[upper_y1:upper_y2, x1:x2]
    lower = screen_roi[lower_y1:lower_y2, x1:x2]

    upper_bounds = (x1, upper_y1, x2, upper_y2)
    lower_bounds = (x1, lower_y1, x2, lower_y2)
    return upper, upper_bounds, lower, lower_bounds


def preprocess_variants(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return [binary, cv2.bitwise_not(binary)]


def extract_train_id(text):
    match = TRAIN_ID_PATTERN.search(text or "")
    if not match:
        return ""

    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) == 7:
        return f"{digits[:3]}-{digits[3:]}"
    return match.group(0).replace(" ", "-").upper()


def normalize_known_train_id(candidate):
    if candidate in KNOWN_TRAIN_IDS:
        return candidate

    candidate_digits = re.sub(r"\D", "", candidate or "")
    if len(candidate_digits) != 7:
        return ""

    best_match = ""
    best_distance = None

    for known_id in KNOWN_TRAIN_IDS:
        known_digits = re.sub(r"\D", "", known_id)
        distance = sum(1 for left, right in zip(candidate_digits, known_digits) if left != right)

        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_match = known_id

    if best_distance is not None and best_distance <= 1:
        return best_match

    return ""


def read_numeric_region(image, allow_hyphen=False):
    best_text = ""
    best_bounds = None
    best_clean = ""
    whitelist = "0123456789-" if allow_hyphen else "0123456789"

    for processed in preprocess_variants(image):
        data = pytesseract.image_to_data(
            Image.fromarray(processed),
            config=f"--oem 3 --psm 7 -c tessedit_char_whitelist={whitelist}",
            output_type=Output.DICT,
        )

        for index, text in enumerate(data["text"]):
            raw_text = text or ""
            cleaned = re.sub(r"[^0-9-]", "", raw_text)
            if not cleaned:
                continue

            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                confidence = -1.0

            if confidence < 20:
                continue

            x = int(data["left"][index] / 3)
            y = int(data["top"][index] / 3)
            w = int(data["width"][index] / 3)
            h = int(data["height"][index] / 3)

            if len(cleaned) < len(best_clean):
                continue

            best_text = raw_text.strip()
            best_clean = cleaned
            best_bounds = (x, y, x + w, y + h)

    return best_clean, best_text, best_bounds


def send_payload(server_url, payload):
    parsed = urlsplit(server_url)
    if parsed.scheme and parsed.netloc:
        server_base = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    else:
        server_base = server_url.rstrip("/")

    routes = ["/api/update-mileage", "/api/submit-scan"]
    last_response = None

    for route in routes:
        response = requests.post(f"{server_base}{route}", json=payload, timeout=10)
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}

        if response.status_code != 404:
            return response.status_code, body, route

        last_response = (response.status_code, body, route)

    return last_response


def draw_labeled_box(frame, bounds, color, label):
    x1, y1, x2, y2 = bounds
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    cv2.putText(
        frame,
        label,
        (x1, max(y1 - 8, 20)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2,
    )


def main():
    global last_train_id, last_train_ocr, last_mileage_ocr, last_scan_time, last_sent_result

    server_url = FLASK_API_URL

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Error: Could not read frame")
                break

            now = time.time()
            screen_bounds = find_screen_region(frame)

            if screen_bounds is None:
                screen_bounds = (0, 0, frame.shape[1], frame.shape[0])

            screen_roi, inner_bounds = crop_region(frame, screen_bounds, inset_x=0.05, inset_y=0.06)
            upper_roi, upper_rel_bounds, lower_roi, lower_rel_bounds = split_upper_lower(screen_roi)

            upper_abs_bounds = clamp_bounds(
                (
                    inner_bounds[0] + upper_rel_bounds[0],
                    inner_bounds[1] + upper_rel_bounds[1],
                    inner_bounds[0] + upper_rel_bounds[2],
                    inner_bounds[1] + upper_rel_bounds[3],
                ),
                frame.shape,
                margin=16,
            )
            lower_abs_bounds = clamp_bounds(
                (
                    inner_bounds[0] + lower_rel_bounds[0],
                    inner_bounds[1] + lower_rel_bounds[1],
                    inner_bounds[0] + lower_rel_bounds[2],
                    inner_bounds[1] + lower_rel_bounds[3],
                ),
                frame.shape,
                margin=16,
            )

            train_id = ""
            mileage_value = ""
            train_ocr = ""
            mileage_ocr = ""
            train_box = None
            mileage_box = None

            if now - last_scan_time >= SCAN_INTERVAL:
                last_scan_time = now

                train_ocr, train_ocr_raw, train_box = read_numeric_region(upper_roi, allow_hyphen=True)
                mileage_ocr, mileage_ocr_raw, mileage_box = read_numeric_region(lower_roi, allow_hyphen=False)

                train_id = normalize_known_train_id(extract_train_id(train_ocr))
                if not train_id:
                    train_id = normalize_known_train_id(extract_train_id(train_ocr_raw))

                mileage_value = re.sub(r"\D", "", mileage_ocr)
                if not mileage_value:
                    mileage_value = re.sub(r"\D", "", mileage_ocr_raw)

                if train_id:
                    last_train_id = train_id
                    last_train_ocr = train_ocr_raw or train_ocr
                if mileage_value:
                    last_mileage_ocr = mileage_ocr_raw or mileage_ocr

                if train_id and mileage_value:
                    current_key = (train_id, mileage_value)
                    if current_key != last_sent_result:
                        payload = {
                            "train_id": train_id,
                            "mileage": int(mileage_value),
                            "source": "pi_ocr",
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }

                        try:
                            result = send_payload(server_url, payload)
                            if result is None:
                                raise RuntimeError("No API route responded")

                            status_code, body, route_used = result
                            print("Sent:", payload)
                            print("Route:", route_used)
                            print("Status:", status_code)
                            print("Response:", body)
                            print("-" * 50)

                            if status_code == 200:
                                last_sent_result = current_key
                        except Exception as exc:
                            print("Error sending to Flask:", exc)

            display = frame.copy()
            draw_labeled_box(display, screen_bounds, (255, 0, 255), "Screen")
            draw_labeled_box(display, upper_abs_bounds, (0, 255, 0), "ID scan")
            draw_labeled_box(display, lower_abs_bounds, (0, 255, 255), "Mileage scan")

            cv2.putText(
                display,
                f"Hubometer ID: {last_train_id if last_train_id else 'Scanning...'}",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (0, 255, 0),
                2,
            )
            cv2.putText(
                display,
                f"Train OCR: {last_train_ocr if last_train_ocr else '-'}",
                (20, 80),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
            )
            cv2.putText(
                display,
                f"Mileage OCR: {last_mileage_ocr if last_mileage_ocr else '-'}",
                (20, 110),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
            )
            cv2.putText(
                display,
                "Upper band = ID | Lower band = mileage | Press Q to quit",
                (20, 140),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 0),
                2,
            )

            cv2.imshow("Live Camera Auto Scan", display)
            cv2.imshow("ID ROI", upper_roi)
            cv2.imshow("Mileage ROI", lower_roi)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()