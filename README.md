# LRT Dashboard

## Run the server

```powershell
python app.py
```

The Flask app listens on `0.0.0.0:5000`, so a Raspberry Pi on the same network can post to `http://<server-ip>:5000`.

## Raspberry Pi sender

The sender script captures from the Pi camera, runs OCR on the hubometer ID and mileage windows, and posts JSON directly to the Flask API.

```powershell
python simulate_pi_data.py --server-url http://<server-ip>:5000 --display
```

For a one-shot test:

```powershell
python simulate_pi_data.py --server-url http://<server-ip>:5000 --once
```

## Full Pi setup

1. Update the Pi and install system packages.

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip tesseract-ocr
```

2. Install the Pi camera stack if you are using Picamera2.

```bash
sudo apt install -y python3-picamera2
```

3. Create a virtual environment in the project folder on the Pi.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

4. Install Python packages.

```bash
pip install -r requirements.txt
```

5. Set the Flask server URL to your laptop or server IP.

```bash
export FLASK_API_URL=http://<server-ip>:5000
```

6. Run the sender.

```bash
python simulate_pi_data.py --server-url http://<server-ip>:5000 --display
```

## Pi dependencies

Install the Python packages from `requirements.txt`, and make sure the Pi has camera support and Tesseract available on the system.

Suggested Raspberry Pi OS packages:

```bash
sudo apt update
sudo apt install -y tesseract-ocr
```

If you are using Picamera2, install the Raspberry Pi camera stack for your OS image as well.

If `pytesseract` cannot find the Tesseract binary on the Pi, set:

```bash
export TESSERACT_CMD=/usr/bin/tesseract
```

## Data flow

Pi camera -> OCR/regex on the Pi -> JSON POST to `/api/submit-scan` -> `trains.json` and `scan_history.json` update -> dashboard refresh.

## What the Pi sends

```json
{
	"train_id": "650-0610",
	"mileage": 1234567,
	"source": "pi_ocr",
	"timestamp": "2026-05-22 12:00:00"
}
```