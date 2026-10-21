// Permanent Render backend URL for LadleFlow.
const API_BASE = "https://ladle-flow-smart-ladle-scheduling-system.onrender.com";

const API_URL = `${API_BASE}/api/live`;
const POLL_INTERVAL_MS = 1000;


const PROCESS_MINUTES = { BOF: 40, LRF: 49, VD: 49, Caster: 62, Inspection: 10 };
const $ = (id) => document.getElementById(id);

function safe(value) {
  return String(value ?? "—").replace(/[&<>"']/g, ch => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  }[ch]));
}
function statusType(status) {
  const s = String(status || "").toUpperCase();
  if (s === "READY") return "ready";
  if (s === "PROCESSING") return "processing";
  if (s === "IN TRANSIT") return "transit";
  if (s.includes("WAITING")) return "waiting";
  return "waiting";
}
function colorFor(status) {
  return ({processing:"#ff9d19", transit:"#168fff", ready:"#14d18b", waiting:"#91a9c2"})[statusType(status)];
}
function value(row, ...keys) {
  for (const key of keys) if (row && row[key] !== undefined && row[key] !== null) return row[key];
  return "—";
}
function percentFor(row, status, countdown) {
  if (statusType(status) === "ready") return 100;
  if (statusType(status) === "waiting") return 0;
  const match = String(countdown).match(/^(\d+):(\d{2})$/);
  if (!match) return 0;
  const remainingSeconds = Number(match[1]) * 60 + Number(match[2]);
  if (statusType(status) === "processing") {
    const location = String(value(row, "Current Location / Route", "location"));
    const duration = (PROCESS_MINUTES[location] || 0) * 60;
    if (duration > 0) return Math.max(0, Math.min(100, Math.round((1 - remainingSeconds / duration) * 100)));
  }
  // For transit, the API does not currently provide total transfer duration.
  // Avoid fabricating a progress percentage.
  return null;
}
function renderLadles(ladles) {
  $("ladleGrid").innerHTML = ladles.map(row => {
    const id = value(row, "Ladle", "ladle", "id");
    const location = value(row, "Current Location / Route", "Location / Route", "location");
    const status = value(row, "Status", "status");
    const nextEvent = value(row, "Next Event", "next_event");
    const countdown = value(row, "Ladle Availability (min:sec)", "availability", "countdown");
    const type = statusType(status);
    const pct = percentFor(row, status, countdown);
    const progressWidth = pct === null ? 0 : pct;
    return `<article class="ladle-card" style="--status-color:${colorFor(status)}">
      <div class="ladle-top">
        <div class="ladle-icon">♨</div>
        <div><div class="ladle-name">${safe(id)}</div><div class="location"><span class="pin">♦</span>${safe(location)}</div></div>
        <span class="status ${type}">${safe(status)}</span>
      </div>
      <div class="progress-line"><div class="progress-track"><div class="progress-fill ${type}" style="width:${progressWidth}%;${pct===null?'opacity:.35':''}"></div></div><span class="progress-value">${pct===null?'—':pct+'%'}</span></div>
      <div class="card-details">
        <div class="detail"><span class="detail-icon">◴</span><div><span class="detail-label">Time Remaining</span><strong>${safe(countdown)}</strong></div></div>
        <div class="detail"><span class="detail-icon">▣</span><div><span class="detail-label">Next Event</span><strong style="font-size:14px">${safe(nextEvent)}</strong><small>${safe(eventTimeFromNext(nextEvent))}</small></div></div>
      </div>
    </article>`;
  }).join("");
}
function eventTimeFromNext(nextEvent) {
  const match = String(nextEvent).match(/\b\d{1,2}:\d{2}:\d{2}\s*[AP]M\b/i);
  if (match) return match[0];
  if (String(nextEvent).toLowerCase().includes("available now")) return "Available now";
  return "";
}
function renderEvents(events) {
  const body = $("eventsBody");
  if (!Array.isArray(events) || events.length === 0) {
    body.innerHTML = '<tr><td colspan="5">No recent events returned by the API.</td></tr>';
    return;
  }
  // API returns newest events last; show newest first, just like the Colab dashboard.
  body.innerHTML = [...events].reverse().slice(0, 10).map(e => {
    const status = value(e, "Status", "status");
    return `<tr>
      <td>${safe(value(e,"Time","time"))}</td>
      <td><strong>${safe(value(e,"Ladle","ladle"))}</strong></td>
      <td>${safe(value(e,"Location / Route","Location","location","route"))}</td>
      <td><span class="table-status ${statusType(status)}">${safe(status)}</span></td>
      <td>${safe(value(e,"Event","event"))}</td>
    </tr>`;
  }).join("");
}
function renderSummary(ladles) {
  const count = {ready:0, processing:0, transit:0, waiting:0};
  ladles.forEach(l => count[statusType(value(l,"Status","status"))]++);
  const items = [
    {label:"Total Ladles",count:String(ladles.length).padStart(2,"0"),icon:"♨"},
    {label:"Ready",count:String(count.ready).padStart(2,"0"),icon:"✓"},
    {label:"Processing",count:String(count.processing).padStart(2,"0"),icon:"◷"},
    {label:"In Transit",count:String(count.transit).padStart(2,"0"),icon:"▰"},
    {label:"Waiting",count:String(count.waiting).padStart(2,"0"),icon:"Ⅱ"}
  ];
  $("summaryCards").innerHTML = items.map(i => `<div class="summary-card"><div class="summary-icon">${i.icon}</div><div><span>${i.label}</span><strong>${i.count}</strong></div></div>`).join("");
}
function setConnection(online, message) {
  const el = $("connectionStatus");
  el.innerHTML = `<i></i> ${online ? "LIVE" : "OFFLINE"}`;
  el.style.borderColor = online ? "#00b982" : "#ff654e";
  el.style.background = online ? "rgba(0,70,66,.88)" : "rgba(90,25,25,.9)";
  el.title = message || "";
}
function updateClockFromApi(apiTime) {
  // Use the simulation's own clock rather than browser-local or hardcoded sample time.
  if (apiTime && $("currentTime")) $("currentTime").textContent = apiTime;
  const date = new Intl.DateTimeFormat("en-IN", {timeZone:"Asia/Kolkata",day:"2-digit",month:"short",year:"numeric"}).format(new Date());
  if ($("currentDate")) $("currentDate").textContent = date + " (IST)";
}
let requestInFlight = false;
async function refreshLiveData() {
  if (requestInFlight) return;
  requestInFlight = true;
  try {
    const response = await fetch(`${API_URL}?_=${Date.now()}`, {cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!Array.isArray(data.ladles) || !Array.isArray(data.recent_events)) {
      throw new Error("API response shape differs from expected /api/live data");
    }
    renderLadles(data.ladles);
    renderEvents(data.recent_events);
    renderSummary(data.ladles);
    updateClockFromApi(data.current_time);
    setConnection(true, `Last response received at ${data.checked_at || data.current_time || "now"}`);
  } catch (err) {
    setConnection(false, err.message);
    // Do not replace existing live values with made-up sample data when API is unavailable.
    if (!$("ladleGrid").children.length) {
      $("ladleGrid").innerHTML = `<div class="loading panel">Unable to load live data: ${safe(err.message)}. Check Colab, the tunnel URL, and API CORS settings.</div>`;
    }
    console.error("Live ladle API error:", err);
  } finally {
    requestInFlight = false;
  }
}
refreshLiveData();
setInterval(refreshLiveData, POLL_INTERVAL_MS);
