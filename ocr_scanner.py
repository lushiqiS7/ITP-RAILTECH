import cv2
import pytesseract
from PIL import Image
import time
import re
import requests

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

FLASK_API_URL = "http://127.0.0.1:5000/api/update-mileage"
TRAINS_API_URL = "http://127.0.0.1:5000/api/trains"


def load_valid_trains():
    try:
        response = requests.get(TRAINS_API_URL, timeout=3)
        if response.status_code == 200:
            return {t["train_id"] for t in response.json()}
    except Exception:
        pass
    return {"PV101", "PV102", "PV103", "PV104", "PV105", "PV106"}


VALID_TRAINS = load_valid_trains()

cap = cv2.VideoCapture(0)
detector = cv2.QRCodeDetector()

if not cap.isOpened():
    print("Error: Could not open camera")
    exit()

last_qr_data = ""
last_scan_time = 0
scan_interval = 2
last_sent_result = ""

while True:
    ret, frame = cap.read()
    if not ret:
        print("Error: Could not read frame")
        break

    qr_data, points, _ = detector.detectAndDecode(frame)
    text_roi = None
    thresh = None

    if points is not None:
        pts = points[0].astype(int)

        for i in range(len(pts)):
            cv2.line(frame, tuple(pts[i]), tuple(pts[(i + 1) % len(pts)]), (0, 255, 0), 3)

        if qr_data:
            last_qr_data = qr_data

        x, y, w, h = cv2.boundingRect(pts)

        roi_x1 = max(x - 40, 0)
        roi_x2 = min(x + w + 40, frame.shape[1])
        roi_y1 = min(y + h + 10, frame.shape[0] - 1)
        roi_y2 = min(y + h + 140, frame.shape[0])

        if roi_y2 > roi_y1 and roi_x2 > roi_x1:
            text_roi = frame[roi_y1:roi_y2, roi_x1:roi_x2]
            cv2.rectangle(frame, (roi_x1, roi_y1), (roi_x2, roi_y2), (255, 255, 0), 2)

    current_time = time.time()

    if current_time - last_scan_time >= scan_interval:
        last_scan_time = current_time
        mileage_text = ""

        if text_roi is not None and text_roi.size > 0:
            gray = cv2.cvtColor(text_roi, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            blur = cv2.GaussianBlur(gray, (3, 3), 0)
            _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

            config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789'
            mileage_text = pytesseract.image_to_string(Image.fromarray(thresh), config=config).strip()
            mileage_text = re.sub(r'[^0-9]', '', mileage_text)

        if last_qr_data in VALID_TRAINS and mileage_text:
            result_key = f"{last_qr_data}:{mileage_text}"

            if result_key != last_sent_result:
                ocr_confidence = min(0.99, 0.70 + len(mileage_text) * 0.03)
                payload = {
                    "train_id": last_qr_data,
                    "mileage": mileage_text,
                    "ocr_confidence": ocr_confidence,
                    "qr_confidence": 0.98,
                }

                try:
                    response = requests.post(FLASK_API_URL, json=payload)
                    response_data = response.json()

                    print("Sent to dashboard:", payload)
                    print("Server response:", response_data)
                    print("-" * 50)

                    if response.status_code == 200:
                        last_sent_result = result_key

                except Exception as e:
                    print("Error sending to Flask:", e)

    cv2.putText(frame, f"QR: {last_qr_data if last_qr_data else 'Scanning...'}", (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    cv2.putText(frame, "Auto-scan every 2 sec | Press Q to quit", (20, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

    cv2.imshow("Live Camera Auto Scan", frame)

    if text_roi is not None:
        preview = cv2.resize(text_roi, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        cv2.imshow("Text ROI", preview)

    if thresh is not None:
        cv2.imshow("Threshold", thresh)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()