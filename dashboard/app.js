/**
 * Rally OpenStack Monitor - Dashboard Logic
 * ===========================================
 * Fetches results from the exporter API, renders service cards,
 * 7-day status timeline, trend charts, and cleanup status.
 * Auto-refreshes every 60 seconds.
 */

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const REFRESH_INTERVAL = 60_000;  // 60 seconds

const SERVICE_ICONS = {
    keystone: '🔐',
    nova: '🖥️',
    neutron: '🌐',
    glance: '🖼️',
    cinder: '💾',
    swift: '📦',
};

const SERVICE_DESCRIPTIONS = {
    keystone: 'Identity & Authentication',
    nova: 'Compute (VMs)',
    neutron: 'Networking',
    glance: 'Image Service',
    cinder: 'Block Storage',
    swift: 'Object Storage',
};

let durationChart = null;
let successChart = null;

// Tracks which historical run is pinned (null = live/latest)
let selectedRunIndex = null;
let cachedHistory = { runs: [] };
let cachedResults = null;
let cachedHealth = null;

// ---------------------------------------------------------------------------
// Data Fetching
// ---------------------------------------------------------------------------
async function fetchResults() {
    try {
        const res = await fetch('/results.json');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return await res.json();
    } catch (err) {
        console.error('Failed to fetch results:', err);
        return null;
    }
}

async function fetchHistory() {
    try {
        const res = await fetch('/history.json');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return await res.json();
    } catch (err) {
        console.error('Failed to fetch history:', err);
        return { runs: [] };
    }
}

async function fetchHealth() {
    try {
        const res = await fetch('/health.json');
        if (!res.ok) return null;
        return await res.json();
    } catch (err) {
        console.error('Failed to fetch health:', err);
        return null;
    }
}

async function fetchHealthHistory() {
    try {
        const res = await fetch('/health_history.json');
        if (!res.ok) return { checks: [] };
        return await res.json();
    } catch (err) {
        console.error('Failed to fetch health history:', err);
        return { checks: [] };
    }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------
function formatTimestamp(ts) {
    if (!ts || ts === 'waiting_for_first_run' || ts === 'none') return 'Waiting for first run...';
    // Compact format: 20260220T143021Z
    const compact = ts.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
    if (compact) {
        const [, y, mo, d, h, mi, s] = compact;
        return new Date(Date.UTC(+y, +mo - 1, +d, +h, +mi, +s)).toLocaleString();
    }
    // ISO format: 2026-02-24T10:00:00Z
    const isoDate = new Date(ts);
    if (!isNaN(isoDate)) return isoDate.toLocaleString();
    return ts;
}

function formatDuration(seconds) {
    if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    return `${(seconds / 60).toFixed(1)}m`;
}

function getRunStatus(runData) {
    const services = runData.services || {};
    const statuses = Object.values(services).map(s => s.status);
    if (statuses.some(s => s === 'failed')) return 'failed';
    if (statuses.every(s => s === 'passed')) return 'passed';
    return 'pending';
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------
function updateHeader(summary, health) {
    const badge = document.getElementById('healthBadge');
    const text = badge.querySelector('.health-text');
    const lastRun = document.getElementById('lastRun');

    const rallyStatus = getRunStatus(summary);
    const apiDown = health && health.overall === 'down';

    // API health failure takes precedence over Rally run result
    const status = apiDown ? 'failed' : rallyStatus;

    badge.className = `health-badge ${status === 'passed' ? 'healthy' : status === 'failed' ? 'unhealthy' : ''}`;
    text.textContent = apiDown
        ? 'API Issues Detected'
        : status === 'passed' ? 'All Healthy'
        : status === 'failed' ? 'Issues Detected'
        : 'Pending';

    lastRun.textContent = `Last run: ${formatTimestamp(summary.timestamp)}`;
}

// ---------------------------------------------------------------------------
// 7-Day Timeline
// ---------------------------------------------------------------------------
function renderTimeline(history) {
    const container = document.getElementById('timeline');
    const countBadge = document.getElementById('timelineCount');

    const runs = history.runs || [];
    countBadge.textContent = `${runs.length} run${runs.length !== 1 ? 's' : ''}`;

    if (runs.length === 0) {
        container.innerHTML = '<div class="timeline-loading">No historical data yet</div>';
        return;
    }

    container.innerHTML = '';
    runs.forEach((run, i) => {
        const status = getRunStatus(run);
        const cell = document.createElement('div');
        cell.className = `timeline-cell ${status}`;
        if (selectedRunIndex === i) cell.classList.add('selected');

        // Tooltip content
        const failedSvcs = Object.entries(run.services || {})
            .filter(([, v]) => v.status === 'failed')
            .map(([k]) => k);
        const tooltipDetail = status === 'failed'
            ? `Failed: ${failedSvcs.join(', ')}`
            : Object.keys(run.services || {}).length === 0
                ? 'No service data'
                : 'All services passed';

        cell.innerHTML = `
            <div class="timeline-tooltip">
                <strong>${formatTimestamp(run.timestamp)}</strong><br>
                ${tooltipDetail}
            </div>
        `;

        // Click: pin this historical run into the Core Services view
        cell.addEventListener('click', () => selectHistoricalRun(run, i));

        container.appendChild(cell);
    });

    // Auto-scroll to latest (rightmost) run
    container.scrollLeft = container.scrollWidth;
}

// ---------------------------------------------------------------------------
// Historical Run Selection
// ---------------------------------------------------------------------------
function selectHistoricalRun(run, index) {
    selectedRunIndex = index;

    // Update selected highlight on all cells
    document.querySelectorAll('.timeline-cell').forEach((cell, i) => {
        cell.classList.toggle('selected', i === index);
    });

    // Show banner
    const banner = document.getElementById('historicalBanner');
    const bannerText = document.getElementById('historicalBannerText');
    bannerText.textContent = `Viewing run: ${formatTimestamp(run.timestamp)}`;
    banner.style.display = '';

    // Re-render service cards with historical data (keep live health indicators)
    renderServiceCards(run, cachedHistory, cachedHealth);

    // Update section title
    document.getElementById('servicesSectionTitle').textContent = 'Core Services (Historical)';
}

function backToLatest() {
    selectedRunIndex = null;

    // Remove selected highlight
    document.querySelectorAll('.timeline-cell').forEach(cell => {
        cell.classList.remove('selected');
    });

    // Hide banner
    document.getElementById('historicalBanner').style.display = 'none';

    // Restore section title
    document.getElementById('servicesSectionTitle').textContent = 'Core Services';

    // Re-render with live data
    if (cachedResults) {
        renderServiceCards(cachedResults.summary, cachedHistory, cachedHealth);
    }
}

// Wire up the back-to-latest button
document.getElementById('backToLatestBtn').addEventListener('click', backToLatest);

// ---------------------------------------------------------------------------
// Health Check Timeline
// ---------------------------------------------------------------------------
function renderHealthTimeline(healthHistory) {
    const container = document.getElementById('healthTimeline');
    const countBadge = document.getElementById('healthTimelineCount');

    const checks = (healthHistory && healthHistory.checks) || [];
    countBadge.textContent = `${checks.length} check${checks.length !== 1 ? 's' : ''}`;

    if (checks.length === 0) {
        container.innerHTML = '<div class="timeline-loading">No health check data yet</div>';
        return;
    }

    container.innerHTML = '';
    checks.forEach(check => {
        const status = check.overall || 'unknown';
        const cell = document.createElement('div');
        cell.className = `htl-cell ${status}`;

        const downSvcs = Object.entries(check.services || {})
            .filter(([, v]) => v.status === 'down')
            .map(([k]) => k);
        const detail = status === 'down'
            ? `Down: ${downSvcs.join(', ')}`
            : 'All services up';

        cell.innerHTML = `
            <div class="timeline-tooltip">
                <strong>${formatTimestamp(check.timestamp)}</strong><br>
                ${detail}
            </div>
        `;
        container.appendChild(cell);
    });

    // Auto-scroll to latest (rightmost)
    container.scrollLeft = container.scrollWidth;
}

// ---------------------------------------------------------------------------
// Service Cards
// ---------------------------------------------------------------------------
function renderServiceCards(summary, history, health) {
    const grid = document.getElementById('servicesGrid');
    const services = summary.services || {};

    grid.innerHTML = '';
    for (const [name, data] of Object.entries(services)) {
        const card = document.createElement('div');
        card.className = `service-card status-${data.status}`;
        card.onclick = () => openModal(name, data);

        // Build mini timeline from history
        const miniTimeline = (history.runs || []).map(run => {
            const svc = (run.services || {})[name];
            return svc ? svc.status : 'pending';
        });

        // Live API health indicator
        const svcHealth = health && health.services && health.services[name];
        const liveStatus = svcHealth ? svcHealth.status : 'unknown';
        const liveLatency = svcHealth ? `${svcHealth.latency_ms}ms` : '';
        const liveLabel = liveStatus === 'up'
            ? `API live${liveLatency ? ' · ' + liveLatency : ''}`
            : liveStatus === 'down' ? 'API down' : 'API …';

        card.innerHTML = `
            <div class="card-header">
                <div>
                    <div class="card-title">${SERVICE_ICONS[name] || '⚙️'} ${name}</div>
                    <div style="font-size: 0.7rem; color: var(--text-muted); margin-top: 0.15rem;">
                        ${SERVICE_DESCRIPTIONS[name] || ''}
                    </div>
                </div>
                <div style="display:flex;flex-direction:column;align-items:flex-end;gap:0.35rem;">
                    <span class="status-chip ${data.status}">${data.status}</span>
                    <div class="live-indicator ${liveStatus}">
                        <span class="live-dot"></span>
                        <span>${liveLabel}</span>
                    </div>
                </div>
            </div>
            <div class="card-metrics">
                <div class="metric">
                    <div class="metric-value">${formatDuration(data.duration || 0)}</div>
                    <div class="metric-label">Duration</div>
                </div>
                <div class="metric">
                    <div class="metric-value">${data.total_iterations || 0}</div>
                    <div class="metric-label">Iterations</div>
                </div>
                <div class="metric">
                    <div class="metric-value" style="color: ${(data.failed_iterations || 0) > 0 ? 'var(--failure)' : 'var(--success)'}">
                        ${data.failed_iterations || 0}
                    </div>
                    <div class="metric-label">Failures</div>
                </div>
            </div>
            <div class="card-timeline">
                ${miniTimeline.map(s => `<div class="card-timeline-cell ${s}"></div>`).join('')}
            </div>
        `;

        grid.appendChild(card);
    }
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------
function openModal(serviceName, data) {
    const overlay = document.getElementById('modalOverlay');
    const title = document.getElementById('modalTitle');
    const body = document.getElementById('modalBody');

    title.textContent = `${SERVICE_ICONS[serviceName] || ''} ${serviceName} — Scenario Details`;

    const scenarios = data.scenarios || [];
    if (scenarios.length === 0) {
        body.innerHTML = '<p style="color: var(--text-muted); text-align: center;">No scenario data available</p>';
    } else {
        body.innerHTML = scenarios.map(s => `
            <div class="scenario-row">
                <div class="scenario-name">${s.name}</div>
                <div class="scenario-stats">
                    <div class="scenario-stat">
                        <div class="scenario-stat-value">${formatDuration(s.duration || 0)}</div>
                        <div class="scenario-stat-label">Duration</div>
                    </div>
                    <div class="scenario-stat">
                        <div class="scenario-stat-value">${s.iterations || 0}</div>
                        <div class="scenario-stat-label">Iters</div>
                    </div>
                    <div class="scenario-stat">
                        <div class="scenario-stat-value" style="color: ${(s.failures || 0) > 0 ? 'var(--failure)' : 'var(--success)'}">${s.failures || 0}</div>
                        <div class="scenario-stat-label">Fails</div>
                    </div>
                    <span class="status-chip ${s.sla ? 'passed' : 'failed'}">${s.sla ? 'SLA OK' : 'SLA FAIL'}</span>
                </div>
            </div>
        `).join('');
    }

    overlay.classList.add('active');
}

function closeModal() {
    document.getElementById('modalOverlay').classList.remove('active');
}

document.getElementById('modalClose').addEventListener('click', closeModal);
document.getElementById('modalOverlay').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeModal();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
});

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
function renderCharts(history) {
    // Only use runs that have actual service data
    const runs = (history.runs || []).filter(r => Object.keys(r.services || {}).length > 0);
    if (runs.length < 2) return;

    // Derive service list from the first run that has data
    const services = Object.keys(runs[0].services || {});
    const labels = runs.map(r => {
        const t = formatTimestamp(r.timestamp);
        return t.length > 16 ? t.substring(0, 16) : t;
    });

    // Color palette
    const colors = [
        '#6366f1', '#06b6d4', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899'
    ];

    // Duration chart
    const durationCtx = document.getElementById('durationChart').getContext('2d');
    if (durationChart) durationChart.destroy();

    durationChart = new Chart(durationCtx, {
        type: 'line',
        data: {
            labels,
            datasets: services.map((svc, i) => ({
                label: svc,
                data: runs.map(r => ((r.services || {})[svc] || {}).duration || 0),
                borderColor: colors[i % colors.length],
                backgroundColor: colors[i % colors.length] + '15',
                borderWidth: 2,
                tension: 0.4,
                fill: true,
                pointRadius: 3,
                pointHoverRadius: 5,
            }))
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: '#94a3b8',
                        font: { family: 'Inter', size: 11 },
                        boxWidth: 12,
                        padding: 12,
                    }
                }
            },
            scales: {
                x: {
                    ticks: { color: '#64748b', font: { size: 10 } },
                    grid: { color: 'rgba(255,255,255,0.04)' }
                },
                y: {
                    title: { display: true, text: 'Seconds', color: '#64748b', font: { size: 11 } },
                    ticks: { color: '#64748b', font: { size: 10 } },
                    grid: { color: 'rgba(255,255,255,0.04)' }
                }
            }
        }
    });

    // Success rate chart
    const successCtx = document.getElementById('successChart').getContext('2d');
    if (successChart) successChart.destroy();

    successChart = new Chart(successCtx, {
        type: 'bar',
        data: {
            labels,
            datasets: services.map((svc, i) => ({
                label: svc,
                data: runs.map(r => {
                    const s = ((r.services || {})[svc] || {});
                    const total = s.total_iterations || 0;
                    const failed = s.failed_iterations || 0;
                    return total > 0 ? ((total - failed) / total * 100) : 100;
                }),
                backgroundColor: colors[i % colors.length] + '99',
                borderColor: colors[i % colors.length],
                borderWidth: 1,
                borderRadius: 3,
            }))
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: '#94a3b8',
                        font: { family: 'Inter', size: 11 },
                        boxWidth: 12,
                        padding: 12,
                    }
                }
            },
            scales: {
                x: {
                    ticks: { color: '#64748b', font: { size: 10 } },
                    grid: { color: 'rgba(255,255,255,0.04)' }
                },
                y: {
                    title: { display: true, text: 'Success %', color: '#64748b', font: { size: 11 } },
                    ticks: { color: '#64748b', font: { size: 10 } },
                    grid: { color: 'rgba(255,255,255,0.04)' },
                    min: 0,
                    max: 100,
                }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Cleanup Status
// ---------------------------------------------------------------------------
function renderCleanup(cleanup) {
    const grid = document.getElementById('cleanupGrid');
    const badge = document.getElementById('cleanupBadge');

    const details = cleanup.details || {};
    const hasFailed = cleanup.cleanup_failed === 1;

    badge.textContent = hasFailed ? 'ISSUES' : 'OK';
    badge.className = `section-badge ${hasFailed ? 'error' : 'ok'}`;

    const resourceTypes = [
        { key: 'servers', label: 'Servers', icon: '🖥️' },
        { key: 'networks', label: 'Networks', icon: '🌐' },
        { key: 'routers', label: 'Routers', icon: '🔀' },
        { key: 'volumes', label: 'Volumes', icon: '💾' },
        { key: 'images', label: 'Images', icon: '🖼️' },
        { key: 'users', label: 'Users', icon: '👤' },
        { key: 'projects', label: 'Projects', icon: '📁' },
        { key: 'security_groups', label: 'Sec Groups', icon: '🛡️' },
    ];

    grid.innerHTML = resourceTypes.map(rt => {
        const count = details[rt.key] || 0;
        return `
            <div class="cleanup-item ${count > 0 ? 'has-orphans' : ''}">
                <div class="cleanup-service">${rt.icon} ${rt.label}</div>
                <div class="cleanup-count ${count > 0 ? 'nonzero' : 'zero'}">${count}</div>
                <div class="cleanup-label">orphaned</div>
            </div>
        `;
    }).join('');
}

// ---------------------------------------------------------------------------
// Main Refresh Loop
// ---------------------------------------------------------------------------
async function refresh() {
    const [resultsData, historyData, healthData, healthHistoryData] = await Promise.all([
        fetchResults(),
        fetchHistory(),
        fetchHealth(),
        fetchHealthHistory(),
    ]);

    // Cache for use by historical selection and card re-renders
    if (historyData) cachedHistory = historyData;
    if (resultsData) cachedResults = resultsData;
    if (healthData)  cachedHealth  = healthData;

    // Always update timeline and charts
    if (historyData) {
        renderTimeline(historyData);
        renderCharts(historyData);
    }

    // Always update health check timeline
    renderHealthTimeline(healthHistoryData);

    // Always update service card live indicators (even when pinned to a historical run)
    if (selectedRunIndex !== null && cachedResults) {
        const pinnedRun = cachedHistory.runs[selectedRunIndex];
        if (pinnedRun) renderServiceCards(pinnedRun, cachedHistory, cachedHealth);
    }

    // Update header + service cards + cleanup if NOT pinned to a historical run
    if (selectedRunIndex === null) {
        if (resultsData) {
            updateHeader(resultsData.summary, cachedHealth);
            renderServiceCards(resultsData.summary, historyData || cachedHistory, cachedHealth);
            renderCleanup(resultsData.cleanup);
        }
    } else {
        // Still update header badge with latest health even when pinned
        if (cachedResults) updateHeader(cachedResults.summary, cachedHealth);
    }
}

// Initial load
refresh();

// Auto-refresh
setInterval(refresh, REFRESH_INTERVAL);

// Show when next refresh happens
(function updateTimerDisplay() {
    let remaining = REFRESH_INTERVAL / 1000;
    setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) remaining = REFRESH_INTERVAL / 1000;
    }, 1000);
})();
