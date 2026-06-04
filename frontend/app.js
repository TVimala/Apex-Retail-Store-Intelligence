const API = "http://localhost:8000";
const STORE = "STORE_BLR_002";

async function fetchData(endpoint) {
    try {
        const res = await fetch(`${API}/stores/${STORE}/${endpoint}`);
        return await res.json();
    } catch (e) {
        return null;
    }
}

function renderMetrics(data) {
    if (!data) return "No data";

    return `
        <div class="metric">Visitors: ${data.unique_visitors}</div>
        <div class="metric">Conversion: ${(data.conversion_rate * 100).toFixed(2)}%</div>
        <div class="metric">Queue: ${data.queue_depth_current}</div>
        <div class="metric">Transactions: ${data.total_transactions}</div>
        <div class="metric">Abandonment: ${(data.abandonment_rate * 100).toFixed(2)}%</div>
    `;
}

function renderFunnel(data) {
    if (!data) return "No data";

    return data.stages.map(s => `
        <div class="metric">
            ${s.stage}: ${s.count} (${s.drop_off_pct}% drop)
        </div>
    `).join("");
}

function renderHeatmap(data) {
    if (!data || !data.zones) return "No data";

    return data.zones.slice(0, 6).map(z => `
        <div class="metric">
            ${z.zone_id} — Score: ${z.normalised_score}
        </div>
    `).join("");
}

function renderAnomalies(data) {
    if (!data || !data.anomalies) return "No data";

    return data.anomalies.map(a => `
        <div class="metric warn">
            ${a.severity} — ${a.anomaly_type}
        </div>
    `).join("");
}

async function updateDashboard() {
    const metrics = await fetchData("metrics");
    const funnel = await fetchData("funnel");
    const heatmap = await fetchData("heatmap");
    const anomalies = await fetchData("anomalies");

    document.getElementById("metrics").innerHTML = renderMetrics(metrics);
    document.getElementById("funnel").innerHTML = renderFunnel(funnel);
    document.getElementById("heatmap").innerHTML = renderHeatmap(heatmap);
    document.getElementById("anomalies").innerHTML = renderAnomalies(anomalies);
}

setInterval(updateDashboard, 2000);
updateDashboard();