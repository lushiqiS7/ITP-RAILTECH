/* Web Camera Scanner — QR + OCR mileage reading */

let videoStream = null;
let scanInterval = null;
let lastSentKey = "";
let facingMode = "environment";
let tesseractWorker = null;
let useServerOcr = false;

const SCAN_INTERVAL_MS = 2000;

async function initTesseract() {
    if (tesseractWorker) return tesseractWorker;
    try {
        tesseractWorker = await Tesseract.createWorker("eng", 1, {
            logger: () => {},
        });
        await tesseractWorker.setParameters({
            tessedit_char_whitelist: "0123456789",
            tessedit_pageseg_mode: "7",
        });
        return tesseractWorker;
    } catch (e) {
        console.warn("Tesseract init failed, will use server OCR:", e);
        useServerOcr = true;
        return null;
    }
}

async function startCamera() {
    const video = document.getElementById("cameraVideo");
    const overlay = document.getElementById("cameraOverlay");

    try {
        const constraints = {
            video: {
                facingMode: facingMode,
                width: { ideal: 1280 },
                height: { ideal: 720 },
            },
            audio: false,
        };

        if (videoStream) {
            videoStream.getTracks().forEach((t) => t.stop());
        }

        videoStream = await navigator.mediaDevices.getUserMedia(constraints);
        video.srcObject = videoStream;
        overlay.hidden = true;

        document.getElementById("startCameraBtn").hidden = true;
        document.getElementById("stopCameraBtn").hidden = false;
        document.getElementById("switchCameraBtn").hidden = false;

        await initTesseract();
        startScanLoop();
        setStatus("Camera active — scanning for QR and mileage...");
    } catch (err) {
        setStatus("Camera error: " + err.message + ". Check browser permissions.", "error");
        overlay.hidden = false;
        overlay.querySelector("p").textContent = "Camera access denied. Allow camera permission and try again.";
    }
}

function stopCamera() {
    if (scanInterval) {
        clearInterval(scanInterval);
        scanInterval = null;
    }
    if (videoStream) {
        videoStream.getTracks().forEach((t) => t.stop());
        videoStream = null;
    }
    document.getElementById("cameraVideo").srcObject = null;
    document.getElementById("startCameraBtn").hidden = false;
    document.getElementById("stopCameraBtn").hidden = true;
    document.getElementById("switchCameraBtn").hidden = true;
    document.getElementById("cameraOverlay").hidden = false;
    setStatus("Camera stopped");
}

async function switchCamera() {
    facingMode = facingMode === "environment" ? "user" : "environment";
    await startCamera();
}

function startScanLoop() {
    if (scanInterval) clearInterval(scanInterval);
    scanInterval = setInterval(processFrame, SCAN_INTERVAL_MS);
    processFrame();
}

async function processFrame() {
    const video = document.getElementById("cameraVideo");
    const canvas = document.getElementById("cameraCanvas");

    if (!videoStream || video.readyState < 2) return;

    const w = video.videoWidth;
    const h = video.videoHeight;
    if (!w || !h) return;

    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, w, h);
    const imageData = ctx.getImageData(0, 0, w, h);

    let trainId = "";
    let qrConfidence = 0;
    let mileage = "";
    let ocrConfidence = 0;

    if (typeof jsQR !== "undefined") {
        const qr = jsQR(imageData.data, w, h, { inversionAttempts: "attemptBoth" });
        if (qr && qr.data) {
            trainId = qr.data.trim().toUpperCase();
            qrConfidence = 0.98;

            const loc = qr.location;
            const xs = [loc.topLeftCorner.x, loc.topRightCorner.x, loc.bottomRightCorner.x, loc.bottomLeftCorner.x];
            const ys = [loc.topLeftCorner.y, loc.topRightCorner.y, loc.bottomRightCorner.y, loc.bottomLeftCorner.y];
            const qrBottom = Math.max(...ys);
            const qrLeft = Math.min(...xs);
            const qrRight = Math.max(...xs);

            const roiY1 = Math.min(qrBottom + 10, h - 1);
            const roiY2 = Math.min(qrBottom + Math.round(h * 0.15), h);
            const roiX1 = Math.max(qrLeft - 40, 0);
            const roiX2 = Math.min(qrRight + 40, w);

            if (roiY2 > roiY1 && roiX2 > roiX1) {
                const roi = ctx.getImageData(roiX1, roiY1, roiX2 - roiX1, roiY2 - roiY1);
                const ocrResult = await runOcr(roi);
                mileage = ocrResult.mileage;
                ocrConfidence = ocrResult.confidence;
            }
        }
    }

    if (!mileage) {
        const ocrResult = await runOcr(imageData);
        if (!trainId && ocrResult.mileage) {
            mileage = ocrResult.mileage;
            ocrConfidence = ocrResult.confidence * 0.7;
        }
    }

    updateScanDisplay(trainId, mileage, qrConfidence, ocrConfidence);

    if (trainId && mileage && VALID_TRAINS.has(trainId)) {
        const key = trainId + ":" + mileage;
        if (key !== lastSentKey) {
            await sendScan(trainId, mileage, ocrConfidence, qrConfidence);
            lastSentKey = key;
        }
    } else if (trainId && !VALID_TRAINS.has(trainId)) {
        setStatus("QR detected: " + trainId + " — not in fleet. Add via Admin → Train Management.", "warn");
    } else if (trainId && !mileage) {
        setStatus("QR found: " + trainId + " — reading mileage...", "info");
    }
}

async function runOcr(imageData) {
    if (useServerOcr || !tesseractWorker) {
        return await runServerOcr(imageData);
    }

    try {
        const offscreen = document.createElement("canvas");
        offscreen.width = imageData.width;
        offscreen.height = imageData.height;
        offscreen.getContext("2d").putImageData(imageData, 0, 0);

        const scaled = document.createElement("canvas");
        scaled.width = imageData.width * 2;
        scaled.height = imageData.height * 2;
        const sctx = scaled.getContext("2d");
        sctx.drawImage(offscreen, 0, 0, scaled.width, scaled.height);

        const { data } = await tesseractWorker.recognize(scaled);
        const digits = (data.text || "").replace(/[^0-9]/g, "");
        const conf = data.confidence ? data.confidence / 100 : 0.8;
        return { mileage: digits, confidence: digits ? Math.min(0.99, conf) : 0 };
    } catch (e) {
        return await runServerOcr(imageData);
    }
}

async function runServerOcr(imageData) {
    try {
        const offscreen = document.createElement("canvas");
        offscreen.width = imageData.width;
        offscreen.height = imageData.height;
        offscreen.getContext("2d").putImageData(imageData, 0, 0);
        const base64 = offscreen.toDataURL("image/png");

        const res = await fetch("/api/scan-ocr", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ image: base64 }),
        });
        const data = await res.json();
        if (data.success && data.mileage) {
            return { mileage: data.mileage, confidence: data.confidence || 0.85 };
        }
    } catch (e) {
        console.error("Server OCR failed:", e);
    }
    return { mileage: "", confidence: 0 };
}

function updateScanDisplay(trainId, mileage, qrConf, ocrConf) {
    document.getElementById("scanTrainId").textContent = trainId || "—";
    document.getElementById("scanMileage").textContent = mileage ? Number(mileage).toLocaleString() + " km" : "—";
    document.getElementById("scanQrConf").textContent = qrConf ? Math.round(qrConf * 100) + "%" : "—";
    document.getElementById("scanOcrConf").textContent = ocrConf ? Math.round(ocrConf * 100) + "%" : "—";
}

async function sendScan(trainId, mileage, ocrConf, qrConf) {
    setStatus("Sending " + trainId + " @ " + Number(mileage).toLocaleString() + " km...", "info");

    try {
        const res = await fetch("/api/update-mileage", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                train_id: trainId,
                mileage: parseInt(mileage, 10),
                ocr_confidence: ocrConf || 0.85,
                qr_confidence: qrConf || 0.95,
            }),
        });
        const data = await res.json();
        document.getElementById("scanLog").textContent = JSON.stringify(data, null, 2);

        if (data.success) {
            setStatus("✓ " + data.message, "success");
        } else {
            setStatus("✗ " + data.message, "error");
            lastSentKey = "";
        }
    } catch (e) {
        setStatus("Network error: " + e.message, "error");
        lastSentKey = "";
    }
}

async function submitManualScan(e) {
    e.preventDefault();
    const trainId = document.getElementById("manualTrainId").value.trim().toUpperCase();
    const mileage = document.getElementById("manualMileage").value;

    if (!VALID_TRAINS.has(trainId)) {
        setStatus("Train " + trainId + " not found. Add it via Admin → Train Management.", "error");
        return;
    }

    lastSentKey = "";
    await sendScan(trainId, mileage, 1.0, 1.0);
}

function setStatus(msg, type) {
    const el = document.getElementById("scanStatusText");
    el.textContent = msg;
    el.className = type ? "status-" + type : "";
}

if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    document.getElementById("cameraOverlay").querySelector("p").textContent =
        "Camera not supported in this browser. Use manual entry below.";
    document.getElementById("startCameraBtn").disabled = true;
}
