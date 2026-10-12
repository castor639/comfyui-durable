import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SEEN_KEY = "durable.lastEvent";
let lastSeen = Number(sessionStorage.getItem(SEEN_KEY) || 0);

const pill = document.createElement("div");
pill.style.cssText = [
  "position:fixed", "left:12px", "bottom:12px", "z-index:1000",
  "display:flex", "align-items:center", "gap:8px",
  "padding:4px 6px 4px 10px", "border-radius:999px",
  "font:12px system-ui,sans-serif", "color:#ddd",
  "background:rgba(30,30,30,.85)", "border:1px solid #444",
].join(";");
const label = document.createElement("span");
const button = document.createElement("button");
button.style.cssText = "font:inherit;color:#eee;background:#3a3a3a;border:1px solid #555;border-radius:999px;padding:2px 10px;cursor:pointer";
pill.append(label, button);

let state = null;

function toast(ev) {
  const severity = ev.kind === "gave_up" ? "error" : ev.kind === "retry" ? "warn" : "info";
  const t = app.extensionManager?.toast;
  if (t) t.add({ severity, summary: "Durable", detail: ev.message, life: 8000 });
  else console.info("[durable]", ev.message);
}

function showEvents(events) {
  for (const ev of events) {
    if (ev.id <= lastSeen) continue;
    lastSeen = ev.id;
    if (!ev.quiet && Date.now() / 1000 - ev.time < 600) toast(ev);
  }
  sessionStorage.setItem(SEEN_KEY, String(lastSeen));
}

function render() {
  if (!state) return;
  if (state.paused > 0) {
    label.textContent = `Paused: ${state.paused} job${state.paused > 1 ? "s" : ""}`;
    button.textContent = "Resume";
    button.style.display = "";
  } else if (state.pausing) {
    label.textContent = "Pausing at the next step...";
    button.style.display = "none";
  } else if (state.protected > 0) {
    label.textContent = `Protected: ${state.protected} job${state.protected > 1 ? "s" : ""}`;
    button.textContent = "Pause";
    button.style.display = "";
  } else {
    label.textContent = "Durable";
    button.style.display = "none";
  }
  pill.title = `Disk cache: ${state.cache.entries} entries, ${(state.cache.bytes / 1e9).toFixed(1)} GB\nWatchdog: ${state.watchdog ? "on" : "off"}`;
}

async function refresh() {
  try {
    const res = await api.fetchApi("/durable/status");
    if (!res.ok) return;
    state = await res.json();
    showEvents(state.events);
    render();
  } catch {}
}

button.addEventListener("click", async () => {
  const action = state?.paused > 0 ? "resume" : "pause";
  button.disabled = true;
  try {
    const res = await api.fetchApi(`/durable/${action}`, { method: "POST" });
    if (res.ok) state = { ...state, ...(await res.json()) };
    render();
  } finally {
    button.disabled = false;
  }
});

app.registerExtension({
  name: "comfyui.durable",
  setup() {
    document.body.append(pill);
    api.addEventListener("durable.event", ({ detail }) => showEvents([detail]));
    api.addEventListener("status", refresh);
    refresh();
    setInterval(refresh, 4000);
  },
});
