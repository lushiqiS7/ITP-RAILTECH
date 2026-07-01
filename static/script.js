async function loadDashboardData() {
    try {
        const response = await fetch("/api/dashboard-data");
        const data = await response.json();

        updateLatestScan(data.latest_scan);
        updateTrainTable(data.trains);
        updateScanHistory(data.scan_history);
    } catch (error) {
        console.error("Failed to load dashboard data:", error);
    }
}

function updateLatestScan(latestScan) {
    const latestScanDiv = document.getElementById("latestScan");

    if (!latestScan || !latestScan.train_id) {
        latestScanDiv.innerHTML = "<p>No scan data yet.</p>";
        return;
    }

    latestScanDiv.innerHTML = `
        <p><strong>Train ID:</strong> ${latestScan.train_id}</p>
        <p><strong>Mileage:</strong> ${latestScan.mileage}</p>
        <p><strong>Source:</strong> ${latestScan.source}</p>
        <p><strong>Timestamp:</strong> ${latestScan.timestamp}</p>
        <p><strong>Status:</strong> ${latestScan.status}</p>
    `;
}

function updateTrainTable(trains) {
    const tbody = document.getElementById("trainTableBody");
    tbody.innerHTML = "";

    trains.forEach(train => {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td>${train.train_id}</td>
            <td>${train.mileage}</td>
            <td>${train.last_updated || "-"}</td>
            <td>${train.source || "-"}</td>
        `;
        tbody.appendChild(row);
    });
}

function updateScanHistory(history) {
    const tbody = document.getElementById("scanHistoryBody");
    tbody.innerHTML = "";

    history.slice().reverse().forEach(record => {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td>${record.train_id}</td>
            <td>${record.mileage}</td>
            <td>${record.source}</td>
            <td>${record.timestamp}</td>
            <td>${record.status}</td>
        `;
        tbody.appendChild(row);
    });
}

loadDashboardData();
setInterval(loadDashboardData, 3000);