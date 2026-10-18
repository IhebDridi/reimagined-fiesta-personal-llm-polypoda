const fields = ["model", "mmproj", "device", "gpu_layers", "fit", "ctx", "temp", "top_p", "top_k", "threads", "flash_attn", "host", "port", "system", "extra"];
const $ = (id) => document.getElementById(id);

let booted = false;
let modelKey = "";
let deviceKey = "";
let logKey = "";
let useMmproj = false;
let saveTimer = 0;
let saving = false;

function collect() {
  const config = { use_mmproj: useMmproj };
  for (const id of fields) config[id] = $(id).value.trim();
  return config;
}

function setSwitch(on) {
  useMmproj = on;
  const button = $("use-mmproj");
  button.setAttribute("aria-checked", on ? "true" : "false");
  $("mmproj-wrap").hidden = !on;
  const hint = $("mmproj-hint");
  if (hint) hint.textContent = on ? "The projector loads with the model." : "Leave this off for a text model.";
}

function setSeg(name, value) {
  const hidden = $(name);
  if (document.activeElement !== hidden) hidden.value = value;
  document.querySelectorAll(`.seg[data-target="${name}"] button`).forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.value === value ? "true" : "false");
  });
}

function revealPath(el) {
  if (document.activeElement === el) return;
  requestAnimationFrame(() => {
    el.scrollLeft = el.scrollWidth;
  });
}

function setIfIdle(id, value) {
  const el = $(id);
  if (document.activeElement === el) return;
  const next = String(value ?? "");
  if (el.value !== next) el.value = next;
  if (id === "model" || id === "mmproj") {
    el.title = next;
    revealPath(el);
  }
}

function showNotice(text) {
  const notice = $("notice");
  notice.hidden = !text;
  notice.textContent = text || "";
}

function modelLabel(path) {
  if (!path) return { name: "No model selected", meta: "Choose a GGUF file to load on the GPU." };
  const parts = path.split(/[\\/]/);
  const name = parts.pop() || path;
  const folder = parts.pop() || "";
  return { name: name.replace(/\.gguf$/i, ""), meta: folder || path };
}

function paintModel(path) {
  const label = modelLabel(path);
  $("model-name").textContent = label.name;
  const match = [...$("known").options].find((option) => option.value === path);
  $("model-meta").textContent = match ? match.textContent : label.meta;
}

function fillSelect(select, items, keyOf, labelOf, current) {
  const key = JSON.stringify(items);
  if (select.dataset.key === key) {
    if (document.activeElement !== select && current && [...select.options].some((option) => option.value === current)) {
      select.value = current;
    }
    return;
  }
  select.dataset.key = key;
  const previous = document.activeElement === select ? select.value : current || select.value;
  select.replaceChildren();
  for (const item of items) {
    const option = document.createElement("option");
    option.value = keyOf(item);
    option.textContent = labelOf(item);
    select.appendChild(option);
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return response.json();
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    saveTimer = 0;
    saving = true;
    try {
      await post("/api/config", collect());
    } catch (error) {}
    saving = false;
  }, 250);
}

function apply(state) {
  const config = state.config || {};
  if (!saveTimer && !saving) {
    for (const id of fields) {
      if (id === "fit" || id === "flash_attn") setSeg(id, config[id] || (id === "fit" ? "on" : "auto"));
      else setIfIdle(id, config[id] || "");
    }
    if (document.activeElement !== $("use-mmproj")) setSwitch(Boolean(config.use_mmproj));
  }
  paintModel($("model").value);

  const models = state.models || [];
  fillSelect($("known"), models, (item) => item.path, (item) => item.label, $("model").value);
  const devices = state.devices || [];
  const deviceItems = devices.length ? devices : [{ id: config.device || "", label: config.device || "No device yet" }];
  fillSelect($("device"), deviceItems, (item) => item.id, (item) => item.label, config.device || "");

  const nextModels = JSON.stringify(models.map((item) => item.path));
  const nextDevices = JSON.stringify(devices.map((item) => item.id));
  if (nextModels !== modelKey) modelKey = nextModels;
  if (nextDevices !== deviceKey) deviceKey = nextDevices;

  const hint = $("mmproj-hint");
  if (useMmproj) hint.textContent = "The projector loads with the model.";
  else if (state.suggestion) hint.textContent = "A projector sits next to this model. Turn this on to use it.";
  else hint.textContent = "Leave this off for a text model.";

  $("version").textContent = state.version || "";
  const network = $("network");
  const links = state.network || [];
  if (network) {
    network.innerHTML = links.length
      ? "On this network: " + links.map((item) => `<a href="${escapeHtml(item.url)}">${escapeHtml(item.label)}</a>`).join(" · ")
      : "";
  }

  const gpu = devices.find((item) => /nvidia|radeon|arc|gpu/i.test(item.label)) || devices[0];
  $("device-chip").textContent = gpu ? gpu.label.split("(")[0].replace(/^.*:\s*/, "").trim() || gpu.id : "No GPU reported";

  const status = state.status || "Ready";
  $("status").textContent = status;
  const dot = $("dot");
  dot.className = "dot";
  if (/serving|ready at/i.test(status)) dot.classList.add("live");
  else if (/loading|starting/i.test(status)) dot.classList.add("busy");
  else if (/not found|stopped|error|fail/i.test(status)) dot.classList.add("bad");

  $("start").disabled = Boolean(state.server && state.server.running);
  $("start").textContent = state.server && state.server.running ? "Server running" : "Start server";
  const open = $("open");
  if (state.server_url) {
    open.href = state.server_url;
    open.classList.remove("disabled");
  } else {
    open.href = "#";
  }

  const log = (state.log || []).join("\n") || "Waiting for a launch.";
  if (log !== logKey) {
    const view = $("log");
    const follow = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
    logKey = log;
    view.textContent = log;
    if (follow) view.scrollTop = view.scrollHeight;
  }
  paintDownload(state.download);
  paintDevices(state.fleet, state.network);
  paintAgent(state.agent);
  booted = true;
}

async function refresh() {
  try {
    const response = await fetch("/api/state");
    apply(await response.json());
  } catch (error) {
    if (!booted) $("status").textContent = "Launcher is not responding";
  }
}

async function browse(kind) {
  showNotice("");
  const result = await post("/api/browse", { kind, current: kind === "mmproj" ? $("mmproj").value : $("model").value });
  if (!result.ok) {
    showNotice(result.error || "Could not open the file dialog.");
    return;
  }
  if (!result.path) return;
  if (kind === "model") {
    $("model").value = result.path;
    paintModel(result.path);
  } else if (kind === "mmproj") {
    $("mmproj").value = result.path;
    setSwitch(true);
  }
  scheduleSave();
  refresh();
}

document.querySelectorAll(".seg").forEach((group) => {
  group.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || !group.dataset.target) return;
    setSeg(group.dataset.target, button.dataset.value);
    scheduleSave();
  });
});

for (const id of fields) {
  $(id).addEventListener("change", () => {
    if (id === "model") paintModel($("model").value);
    scheduleSave();
  });
  $(id).addEventListener("input", scheduleSave);
}

$("known").addEventListener("change", () => {
  if (!$("known").value) return;
  $("model").value = $("known").value;
  paintModel($("known").value);
  scheduleSave();
  refresh();
});

$("use-mmproj").addEventListener("click", () => {
  const next = !useMmproj;
  setSwitch(next);
  if (next && !$("mmproj").value) refresh();
  scheduleSave();
});

$("browse-model").addEventListener("click", () => browse("model"));
$("browse-mmproj").addEventListener("click", () => browse("mmproj"));
$("runtime").addEventListener("click", () => browse("runtime"));
$("rescan").addEventListener("click", async () => {
  await post("/api/rescan");
  refresh();
});

$("start").addEventListener("click", async () => {
  showNotice("");
  $("start").disabled = true;
  const result = await post("/api/start", collect());
  if (!result.ok) showNotice(result.error || "Could not start the server.");
  refresh();
});

$("chat").addEventListener("click", async () => {
  showNotice("");
  const result = await post("/api/chat", collect());
  if (!result.ok) showNotice(result.error || "Could not open the terminal.");
  refresh();
});

$("stop").addEventListener("click", async () => {
  showNotice("");
  await post("/api/stop");
  refresh();
});

$("open").addEventListener("click", (event) => {
  if ($("open").getAttribute("href") === "#") event.preventDefault();
});

const hfCatalog = new Map();
let hfSeq = 0;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function formatBytes(size) {
  const value = Number(size) || 0;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${Math.round(value / 1024 ** 2)} MB`;
  if (value >= 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}

function paintDownload(download) {
  const box = $("hf-progress");
  if (!box) return;
  if (!download || download.status === "idle") {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const label = $("hf-progress-label");
  const bar = $("hf-bar");
  $("hf-cancel").hidden = !download.active;
  const total = Number(download.total) || 0;
  const received = Number(download.received) || 0;
  if (download.status === "error") {
    label.textContent = download.error || "Download failed.";
    bar.style.width = "0%";
    return;
  }
  if (download.status === "done") {
    const current = $("model").value || "";
    const projector = $("mmproj").value || "";
    const matches = download.path ? current === download.path || projector === download.path : current.endsWith(download.label);
    if (!matches) {
      box.hidden = true;
      return;
    }
    label.textContent = `Saved and selected · ${download.label}`;
    bar.style.width = "100%";
    return;
  }
  const pct = total > 0 ? Math.min(100, Math.round((received / total) * 100)) : 0;
  const amount = total > 0 ? `${formatBytes(received)} / ${formatBytes(total)}` : formatBytes(received);
  label.textContent = total ? `${download.label} · ${amount} · ${pct}%` : `${download.label} · ${amount}`;
  bar.style.width = total ? `${pct}%` : "35%";
}

async function searchHub() {
  const box = $("hf-results");
  const button = $("hf-search");
  button.disabled = true;
  box.innerHTML = `<p class="hint">Searching Hugging Face…</p>`;
  let result;
  try {
    result = await post("/api/hf/search", { query: $("hf-query").value.trim(), mode: hfMode });
  } catch (error) {
    result = { ok: false, error: "Could not reach the launcher." };
  }
  button.disabled = false;
  if (!result.ok) {
    box.innerHTML = `<p class="hint">${escapeHtml(result.error || "Search failed.")}</p>`;
    return;
  }
  if (!result.models.length) {
    box.innerHTML = `<p class="hint">${hfMode === "projector" ? "No projector files matched that search." : "No GGUF models matched that search."}</p>`;
    return;
  }
  box.innerHTML = result.models.map((model) => `
    <div class="hf-repo">
      <button type="button" class="hf-repo-btn" data-repo="${escapeHtml(model.repo)}">
        <strong>${escapeHtml(model.repo)}</strong>
        <span class="hf-size">${escapeHtml(model.size_label || "size unknown")}</span>
        <span>${escapeHtml(model.detail)}</span>
      </button>
      <div class="hf-files" hidden></div>
    </div>
  `).join("");
}

async function openRepo(button) {
  const panel = button.parentElement.querySelector(".hf-files");
  const already = !panel.hidden && panel.dataset.loaded === "1";
  document.querySelectorAll(".hf-files").forEach((el) => { el.hidden = true; });
  if (already) return;
  panel.hidden = false;
  panel.innerHTML = `<p class="hint">Loading GGUF files…</p>`;
  let result;
  try {
    result = await post("/api/hf/files", { repo: button.dataset.repo, mode: hfMode });
  } catch (error) {
    result = { ok: false, error: "Could not reach the launcher." };
  }
  if (!result.ok) {
    panel.innerHTML = `<p class="hint">${escapeHtml(result.error || "Could not list files.")}</p>`;
    return;
  }
  panel.dataset.loaded = "1";
  if (!result.files.length) {
    panel.innerHTML = `<p class="hint">${hfMode === "projector" ? "This repository has no projector files on the main branch." : "This repository has no model files on the main branch."}</p>`;
    return;
  }
  panel.innerHTML = result.files.map((file) => {
    const id = String(++hfSeq);
    hfCatalog.set(id, { repo: result.repo, files: file.files, label: file.label, kind: file.kind });
    const badge = file.recommended ? `<em>Recommended</em>` : "";
    const parts = file.parts > 1 ? ` · ${file.parts} parts` : "";
    return `<div class="hf-file">
      <div class="name"><strong>${escapeHtml(file.label)}${badge}</strong><small>${escapeHtml(file.size_label)}${parts}</small></div>
      <button type="button" class="ghost hf-get" data-id="${id}">${file.local ? "Use" : "Download"}</button>
    </div>`;
  }).join("");
}

async function startDownload(id) {
  const item = hfCatalog.get(id);
  if (!item) return;
  showNotice("");
  const result = await post("/api/hf/download", { repo: item.repo, files: item.files, label: item.label });
  if (!result.ok) showNotice(result.error || "Could not start the download.");
  refresh();
}

let hfMode = "model";

function setHubMode(mode) {
  hfMode = mode === "projector" ? "projector" : "model";
  document.querySelectorAll("#hf-mode button").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.value === hfMode ? "true" : "false");
  });
  $("hf-query").placeholder = hfMode === "projector"
    ? "Search projector files, for example Gemma"
    : "Search models, for example Qwen or Llama";
  $("hf-results").innerHTML = "";
}

$("hf-mode").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  setHubMode(button.dataset.value);
});

$("hf-search").addEventListener("click", searchHub);
$("hf-query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") searchHub();
});
$("hf-results").addEventListener("click", (event) => {
  const repo = event.target.closest(".hf-repo-btn");
  if (repo) {
    openRepo(repo);
    return;
  }
  const get = event.target.closest(".hf-get");
  if (get) startDownload(get.dataset.id);
});
$("hf-cancel").addEventListener("click", () => post("/api/hf/cancel"));

let agentLogKey = "";
let devicesKey = "";
let selectedDevice = "local";

function paintDevices(devices, network) {
  const info = devices || {};
  const list = $("device-list");
  const select = $("agent-device");
  if (!list || !select) return;
  const items = info.devices || [];
  selectedDevice = info.selected || selectedDevice || "local";
  const key = JSON.stringify(items) + "|" + selectedDevice + "|" + (info.adb || "");
  if (key !== devicesKey) {
    devicesKey = key;
    list.replaceChildren();
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "device-row" + (item.online ? "" : " offline");
      row.dataset.id = item.id;
      row.setAttribute("aria-pressed", item.id === selectedDevice ? "true" : "false");
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "device-pick";
      pick.dataset.id = item.id;
      pick.innerHTML = `<span class="device-meta"><div class="device-name">${escapeHtml(item.name)}</div><div class="device-detail">${escapeHtml(item.detail || "")}</div></span><span class="device-kind">${escapeHtml(item.kind || "")}</span>`;
      row.appendChild(pick);
      if (item.id.startsWith("remote:")) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "ghost danger device-remove";
        remove.dataset.id = item.id;
        remove.textContent = "Remove";
        row.appendChild(remove);
      }
      list.appendChild(row);
    }
    fillSelect(
      select,
      items,
      (item) => item.id,
      (item) => item.name + (item.online ? "" : " (offline)"),
      selectedDevice
    );
  } else if (document.activeElement !== select) {
    select.value = selectedDevice;
  }
  const adb = $("adb-status");
  if (adb) adb.textContent = info.adb ? `adb found: ${info.adb}` : "adb not found. Install Android platform-tools and add adb to PATH.";
  const hub = (network && network[0] && network[0].url) || `${location.origin}`;
  const install = `pip install "git+https://github.com/IhebDridi/reimagined-fiesta-personal-llm-polypoda.git"`;
  const win = $("companion-cmd-windows");
  if (win) {
    win.textContent = `${install}\npolypoda-companion --hub ${hub} --name Laptop`;
  }
  const linux = $("companion-cmd-linux");
  if (linux) {
    linux.textContent = `${install}\npolypoda-companion --hub ${hub} --name Linux-Laptop`;
  }
}

function paintAgent(agent) {
  const box = $("agent-log");
  if (!box || !agent) return;
  const running = Boolean(agent.active);
  $("agent-start").disabled = running;
  $("agent-device").disabled = running;
  const label = agent.device_name || "device";
  $("agent-start").textContent = running ? `Using ${label}` : "Use selected device";
  $("agent-stop").hidden = !running;
  const status = $("agent-status");
  if (agent.status === "running") {
    const where = agent.device_name ? `${agent.device_name} · ` : "";
    status.textContent = agent.max_steps
      ? `${where}Step ${agent.step || 1} of ${agent.max_steps}`
      : `${where}Step ${agent.step || 1}`;
  } else if (agent.error) status.textContent = agent.error;
  else if (agent.status === "done") status.textContent = "Finished";
  else if (agent.status === "stopped") status.textContent = "Stopped";
  else status.textContent = "";
  const text = (agent.log || []).join("\n") || "Waiting for a task.";
  if (text !== agentLogKey) {
    const follow = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    agentLogKey = text;
    box.textContent = text;
    if (follow) box.scrollTop = box.scrollHeight;
  }
}

$("device-list").addEventListener("click", async (event) => {
  const remove = event.target.closest(".device-remove");
  if (remove) {
    event.preventDefault();
    event.stopPropagation();
    showNotice("");
    const result = await post("/api/devices/remove", { id: remove.dataset.id });
    if (!result.ok) showNotice(result.error || "Could not remove the companion.");
    devicesKey = "";
    refresh();
    return;
  }
  const pick = event.target.closest(".device-pick");
  if (!pick) return;
  showNotice("");
  const result = await post("/api/devices/select", { id: pick.dataset.id });
  if (!result.ok) showNotice(result.error || "Could not select that device.");
  devicesKey = "";
  refresh();
});

$("agent-device").addEventListener("change", async () => {
  showNotice("");
  const result = await post("/api/devices/select", { id: $("agent-device").value });
  if (!result.ok) showNotice(result.error || "Could not select that device.");
  devicesKey = "";
  refresh();
});

async function addCompanion(nameId, urlId, tokenId, kind) {
  showNotice("");
  const result = await post("/api/devices/add", {
    name: $(nameId).value,
    url: $(urlId).value,
    token: $(tokenId).value,
    kind,
  });
  if (!result.ok) showNotice(result.error || "Could not add the companion.");
  else {
    $(nameId).value = "";
    $(urlId).value = "";
    $(tokenId).value = "";
  }
  devicesKey = "";
  refresh();
}

$("remote-add").addEventListener("click", () => addCompanion("remote-name", "remote-url", "remote-token", "windows"));
$("remote-add-linux").addEventListener("click", () => addCompanion("remote-name-linux", "remote-url-linux", "remote-token-linux", "linux"));

$("adb-pair").addEventListener("click", async () => {
  showNotice("");
  const result = await post("/api/devices/adb-pair", {
    address: $("adb-pair-addr").value,
    code: $("adb-pair-code").value,
  });
  showNotice(result.ok ? (result.message || "Paired.") : (result.error || "Pairing failed."));
  devicesKey = "";
  refresh();
});

$("adb-connect").addEventListener("click", async () => {
  showNotice("");
  const result = await post("/api/devices/adb-connect", { address: $("adb-connect-addr").value });
  showNotice(result.ok ? (result.message || "Connected.") : (result.error || "Connect failed."));
  devicesKey = "";
  refresh();
});

$("agent-start").addEventListener("click", async () => {
  showNotice("");
  const result = await post("/api/agent/start", {
    goal: $("agent-goal").value,
    max_steps: agentStepLimit(),
    device_id: $("agent-device").value || selectedDevice || "local",
  });
  if (!result.ok) showNotice(result.error || "Could not give the model control.");
  refresh();
});

let agentUnlimited = false;

function agentStepLimit() {
  if (agentUnlimited) return 0;
  const value = Number($("agent-steps").value);
  if (!Number.isFinite(value) || value < 1) return 20;
  return Math.round(value);
}

$("agent-unlimited").addEventListener("click", () => {
  agentUnlimited = !agentUnlimited;
  $("agent-unlimited").setAttribute("aria-pressed", agentUnlimited ? "true" : "false");
  $("agent-steps").disabled = agentUnlimited;
});

$("agent-stop").addEventListener("click", async () => {
  await post("/api/agent/stop");
  refresh();
});

setSeg("fit", "on");
setSeg("flash_attn", "auto");
setSwitch(false);
refresh();
setInterval(refresh, 800);
