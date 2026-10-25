const state = {
  status: null,
  step: 1,
  maxStep: 1,
  faces: [],
  authTimer: null,
};

const $ = (id) => document.getElementById(id);

function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.className = "toast";
  }, 3500);
}

async function api(path, options) {
  const res = await fetch(path, options);
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = { ok: false, error: "The wizard returned an invalid response." };
  }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || "Request failed (" + res.status + ").");
  }
  return data;
}

function postJSON(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function go(step) {
  state.step = step;
  state.maxStep = Math.max(state.maxStep, step);
  document.querySelectorAll(".step-content").forEach((el) => {
    el.classList.toggle("active", Number(el.dataset.step) === step);
  });
  document.querySelectorAll(".step-item").forEach((el) => {
    const n = Number(el.dataset.step);
    el.classList.toggle("active", n === step);
    el.classList.toggle("completed", n < step);
    const num = el.querySelector(".step-number");
    num.textContent = n < step ? "\u2713" : String(n);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (step === 7) renderSummary();
  if (step === 6) renderTimerState();
}

function checkRow(label, ok, detail, optional) {
  const row = document.createElement("div");
  row.className = "check-row";
  const name = document.createElement("span");
  name.textContent = label;
  const st = document.createElement("span");
  if (ok) {
    st.className = "check-state ok";
    st.textContent = "OK" + (detail ? " - " + detail : "");
  } else if (optional) {
    st.className = "check-state warn";
    st.textContent = "Skipped";
  } else {
    st.className = "check-state bad";
    st.textContent = "Missing" + (detail ? " - " + detail : "");
  }
  row.append(name, st);
  return row;
}

function renderChecks() {
  const c = state.status.checks;
  const list = $("check-list");
  list.replaceChildren(
    checkRow("Python 3.12+", c.python),
    checkRow("ffmpeg on PATH", c.ffmpeg, c.ffmpeg ? "" : "install ffmpeg"),
    checkRow("ffprobe on PATH", c.ffprobe, c.ffprobe ? "" : "install ffmpeg"),
    checkRow("Google OAuth client", c.client_secret, c.client_secret ? "" : "upload it in step 2"),
    checkRow("Gemini API key", c.gemini, c.gemini ? "" : "upload it in step 2"),
    checkRow("YouTube token", c.token, c.token ? "" : "authorize in step 5", true)
  );
}

function setFileState(container, name, text, ok) {
  container.replaceChildren();
  if (!name) return;
  const row = document.createElement("div");
  row.className = "file-item";
  const info = document.createElement("div");
  info.className = "file-info";
  const icon = document.createElement("span");
  icon.textContent = "\ud83d\udcc4";
  const fname = document.createElement("span");
  fname.className = "file-name";
  fname.textContent = name;
  const st = document.createElement("span");
  st.className = "file-status";
  st.textContent = text;
  if (!ok) st.style.color = "#f87171";
  info.append(icon, fname, st);
  row.append(info);
  container.append(row);
}

async function uploadClientSecret(file) {
  const err = $("client-error");
  err.textContent = "";
  try {
    const content = await file.text();
    await postJSON("/api/client-secret", { content });
    setFileState($("client-state"), file.name, "Valid desktop OAuth client", true);
    toast("client_secret.json saved.");
    await refreshStatus();
  } catch (e) {
    setFileState($("client-state"), file.name, "Rejected", false);
    err.textContent = e.message;
  }
}

async function uploadGemini(content, label) {
  const err = $("gemini-error");
  err.textContent = "";
  try {
    await postJSON("/api/gemini", { content });
    setFileState($("gemini-state"), label, "Key saved", true);
    $("gemini-input").value = "";
    toast("Gemini API key saved.");
    await refreshStatus();
  } catch (e) {
    setFileState($("gemini-state"), label, "Rejected", false);
    err.textContent = e.message;
  }
}

async function uploadFaces(files) {
  const err = $("faces-error");
  err.textContent = "";
  const payload = [];
  for (const file of files) {
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
      err.textContent = "Only JPG, PNG and WebP images are accepted.";
      continue;
    }
    const data = await fileToBase64(file);
    payload.push({ name: file.name, data });
  }
  if (!payload.length) return;
  try {
    await postJSON("/api/faces", { files: payload });
    toast(payload.length + " image(s) saved.");
    await refreshStatus();
    renderFaces();
  } catch (e) {
    err.textContent = e.message;
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.onerror = () => reject(new Error("Could not read " + file.name));
    reader.readAsDataURL(file);
  });
}

function renderFaces() {
  const grid = $("faces-grid");
  const names = (state.status && state.status.faces) || [];
  grid.replaceChildren();
  names.forEach((name) => {
    const box = document.createElement("div");
    box.className = "image-preview";
    const img = document.createElement("img");
    img.src = "/api/faces/" + encodeURIComponent(name);
    img.alt = name;
    box.append(img);
    grid.append(box);
  });
}

function addLink(label, url) {
  const wrap = $("links");
  const row = document.createElement("div");
  row.className = "link-row";
  const l = document.createElement("input");
  l.className = "form-input link-label";
  l.placeholder = "Label (e.g. Discord)";
  l.value = label || "";
  const u = document.createElement("input");
  u.className = "form-input link-url";
  u.placeholder = "URL (https://...)";
  u.value = url || "";
  const rm = document.createElement("button");
  rm.className = "btn btn-secondary remove-link";
  rm.type = "button";
  rm.textContent = "Remove";
  rm.onclick = () => row.remove();
  row.append(l, u, rm);
  wrap.append(row);
}

function prefilled() {
  const e = (state.status && state.status.existing) || {};
  $("channel").value = e.channel || "";
  $("since").value = e.since || "";
  $("min-part").value = e.min_part_minutes || 15;
  $("work-dir").value = e.work_dir || defaultWorkDir();
  $("links").replaceChildren();
  const links = Array.isArray(e.links) ? e.links : [];
  links.forEach((pair) => {
    if (Array.isArray(pair) && pair.length >= 2) addLink(pair[0], pair[1]);
  });
  if (!links.length) addLink("", "");
  if (state.status.checks.client_secret) {
    setFileState($("client-state"), "client_secret.json", "Already saved", true);
  }
  if (state.status.checks.gemini) {
    setFileState($("gemini-state"), "gemini.key", "Already saved", true);
  }
}

function defaultWorkDir() {
  return state.status && state.status.config_dir
    ? state.status.config_dir.replace(/\/\.config\/k2y$/, "/.local/share/k2y")
    : "~/.local/share/k2y";
}

async function refreshStatus() {
  const data = await api("/api/status");
  state.status = data;
  renderChecks();
  renderFaces();
  if (data.auth_running) {
    $("auth-title").textContent = "Waiting for authorization";
    $("auth-copy").textContent =
      "Finish the Google consent screen in the browser window. This page updates automatically.";
    $("auth-btn").disabled = true;
    $("auth-error").textContent = "";
    $("auth-ok").textContent = "";
    startAuthPolling();
  } else {
    stopAuthPolling();
    $("auth-btn").disabled = false;
    if (data.auth_error) {
      $("auth-title").textContent = "Authorization failed";
      $("auth-error").textContent = data.auth_error;
    } else if (data.checks.token) {
      $("auth-title").textContent = "Authorized";
      $("auth-copy").textContent = "This machine already has a YouTube token.";
      $("auth-ok").textContent = "YouTube is connected. Uploads will be private.";
    } else {
      $("auth-title").textContent = "Ready to authorize";
    }
  }
  return data;
}

function startAuthPolling() {
  if (state.authTimer) return;
  state.authTimer = setInterval(() => {
    refreshStatus().catch(() => {});
  }, 2000);
}

function stopAuthPolling() {
  if (state.authTimer) {
    clearInterval(state.authTimer);
    state.authTimer = null;
  }
}

async function saveConfig() {
  const err = $("config-error");
  const channelErr = $("channel-error");
  err.textContent = "";
  channelErr.textContent = "";
  const links = [];
  document.querySelectorAll("#links .link-row").forEach((row) => {
    links.push({
      label: row.querySelector(".link-label").value,
      url: row.querySelector(".link-url").value,
    });
  });
  const body = {
    channel: $("channel").value,
    since: $("since").value,
    work_dir: $("work-dir").value,
    min_part_minutes: Number($("min-part").value || 15),
    links,
  };
  if (!body.channel.trim()) {
    channelErr.textContent = "Enter your Kick channel slug.";
    throw new Error("Enter your Kick channel slug.");
  }
  try {
    await postJSON("/api/config", body);
    await refreshStatus();
    toast("Configuration saved.");
  } catch (e) {
    err.textContent = e.message;
    throw e;
  }
}

async function checkChannel() {
  const slug = $("channel").value.trim();
  const err = $("channel-error");
  err.textContent = "";
  if (!slug) return;
  err.style.color = "#aaa";
  err.textContent = "Checking channel...";
  try {
    const data = await api("/api/check-channel?slug=" + encodeURIComponent(slug));
    err.style.color = "#4ade80";
    err.textContent = "Found " + data.count + " VOD(s) for " + data.slug + ".";
  } catch (e) {
    err.style.color = "#f87171";
    err.textContent = e.message;
  }
}

async function installTimer() {
  const err = $("timer-error");
  const out = $("timer-out");
  err.textContent = "";
  if (!$("install-timer").checked) {
    out.textContent = "Skipped. You can install it later with py -m k2y install-timer.";
    return;
  }
  try {
    const data = await postJSON("/api/timer", { work_dir: $("work-dir").value });
    out.textContent = (data.lines || []).join("\n");
    toast("Timer installed.");
  } catch (e) {
    out.textContent = "Not installed.";
    err.textContent = e.message;
    throw e;
  }
}

function renderTimerState() {
  if (state.status && state.status.timer_lines && state.status.timer_lines.length) {
    $("timer-out").textContent = state.status.timer_lines.join("\n");
  }
}

function renderSummary() {
  if (!state.status) return;
  const e = state.status.existing || {};
  const checks = state.status.checks;
  const rows = [
    ["Kick channel", e.channel || "-"],
    ["Config folder", state.status.config_dir],
    ["Work folder", e.work_dir || "-"],
    ["YouTube", checks.token ? "Authorized (private uploads)" : "Not authorized"],
    ["Reaction images", (state.status.faces || []).length + " file(s)"],
    ["Timer", (state.status.timer_lines || []).length ? "Installed" : "Skipped"],
  ];
  const box = $("summary");
  box.replaceChildren();
  rows.forEach(([label, value]) => {
    const row = document.createElement("div");
    row.className = "summary-item";
    const l = document.createElement("span");
    l.className = "summary-label";
    l.textContent = label;
    const v = document.createElement("span");
    v.className = "summary-value";
    v.textContent = value;
    row.append(l, v);
    box.append(row);
  });
  $("done-config").textContent = state.status.config_dir;
}

function wireDropZone(zoneId, inputId, onFiles) {
  const zone = $(zoneId);
  const input = $(inputId);
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (ev) => {
    ev.preventDefault();
    zone.classList.remove("dragover");
    if (ev.dataTransfer.files.length) onFiles(Array.from(ev.dataTransfer.files));
  });
  input.addEventListener("change", () => {
    if (input.files.length) onFiles(Array.from(input.files));
    input.value = "";
  });
}

function init() {
  wireDropZone("client-zone", "client-file", (files) => uploadClientSecret(files[0]));
  wireDropZone("gemini-zone", "gemini-file", (files) => {
    files[0].text().then((text) => uploadGemini(text, files[0].name));
  });
  wireDropZone("faces-zone", "faces-file", (files) => uploadFaces(files));

  $("gemini-input").addEventListener("change", () => {
    const value = $("gemini-input").value.trim();
    if (value) uploadGemini(value, "pasted key");
  });

  document.querySelectorAll("[data-back]").forEach((btn) => {
    btn.addEventListener("click", () => go(Number(btn.dataset.back)));
  });

  $("to-credentials").onclick = () => go(2);
  $("to-faces").onclick = async () => {
    const c = state.status.checks;
    if (!c.client_secret || !c.gemini) {
      toast("Upload both credentials first.", true);
      return;
    }
    go(3);
  };
  $("skip-faces").onclick = () => go(4);
  $("to-config").onclick = () => go(4);
  $("add-link").onclick = () => addLink("", "");
  $("channel").addEventListener("blur", checkChannel);
  $("to-auth").onclick = async () => {
    try {
      await saveConfig();
      go(5);
    } catch (e) {}
  };
  $("auth-btn").onclick = async () => {
    $("auth-error").textContent = "";
    try {
      await postJSON("/api/auth");
      await refreshStatus();
    } catch (e) {
      $("auth-error").textContent = e.message;
    }
  };
  $("to-timer").onclick = () => {
    if (!state.status.checks.token) {
      toast("Authorize YouTube first.", true);
      return;
    }
    go(6);
  };
  $("finish").onclick = async () => {
    try {
      await installTimer();
      await refreshStatus();
      go(7);
    } catch (e) {}
  };

  refreshStatus()
    .then(() => {
      prefilled();
      if (state.status.checks.client_secret && state.status.checks.gemini) {
        toast("Existing credentials detected.");
      }
    })
    .catch((e) => toast(e.message, true));
}

document.addEventListener("DOMContentLoaded", init);
