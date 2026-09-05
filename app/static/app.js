const state = {
  categories: [],
  channels: [],
  filteredChannels: [],
  categoryId: null,
  sessionId: null,
  streamId: null,
  hls: null,
  configSource: null,
};

const $ = (id) => document.getElementById(id);
const setupPanel = $("setupPanel");
const appPanel = $("appPanel");
const setupForm = $("setupForm");
const setupMessage = $("setupMessage");
const categoriesEl = $("categories");
const channelList = $("channelList");
const channelCount = $("channelCount");
const search = $("search");
const video = $("video");
const nowPlaying = $("nowPlaying");
const streamStatus = $("streamStatus");
const sessionInfo = $("sessionInfo");
const stopBtn = $("stopBtn");
const playbackMode = $("playbackMode");
const settingsBtn = $("settingsBtn");

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, type = "error") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${type}`;
  setTimeout(() => el.classList.add("hidden"), 4500);
}

function setConfigured(configured) {
  setupPanel.classList.toggle("hidden", configured);
  appPanel.classList.toggle("hidden", !configured);
  settingsBtn.classList.toggle("hidden", !configured);
}

async function bootstrap() {
  try {
    const status = await api("/api/status");
    $("version").textContent = `v${status.version}`;
    state.configSource = status.configuration_source;
    if (status.configured) {
      setConfigured(true);
      playbackMode.value = ["auto", "copy", "transcode"].includes(status.default_ffmpeg_mode)
        ? status.default_ffmpeg_mode
        : "auto";
      await loadCategories();
      await loadChannels(null);
    } else {
      setConfigured(false);
    }
  } catch (error) {
    toast(`Could not initialise: ${error.message}`);
  }
}

setupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("connectBtn");
  button.disabled = true;
  setupMessage.textContent = "Testing credentials…";
  try {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        base_url: $("baseUrl").value.trim(),
        username: $("username").value,
        password: $("password").value,
        output: $("output").value,
      }),
    });
    $("password").value = "";
    setupMessage.textContent = "Connected.";
    setConfigured(true);
    await loadCategories();
    await loadChannels(null);
    toast("Provider connected", "success");
  } catch (error) {
    setupMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

settingsBtn.addEventListener("click", async () => {
  if (state.configSource === "environment") {
    toast("Provider settings are managed by container environment variables.");
    return;
  }
  if (!confirm("Disconnect the current provider and return to setup?")) return;
  try {
    await stopPlayback();
    await api("/api/config", { method: "DELETE" });
    setConfigured(false);
  } catch (error) {
    toast(error.message);
  }
});

async function loadCategories() {
  categoriesEl.innerHTML = '<div class="muted">Loading…</div>';
  state.categories = await api("/api/categories");
  renderCategories();
}

function renderCategories() {
  categoriesEl.innerHTML = "";
  const all = categoryButton(null, "All channels");
  categoriesEl.appendChild(all);
  for (const category of state.categories) {
    categoriesEl.appendChild(categoryButton(category.category_id, category.category_name));
  }
}

function categoryButton(id, name) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `category-button ${state.categoryId === id ? "active" : ""}`;
  button.textContent = name;
  button.addEventListener("click", async () => {
    state.categoryId = id;
    renderCategories();
    try {
      await loadChannels(id);
    } catch (error) {
      toast(error.message);
    }
  });
  return button;
}

async function loadChannels(categoryId) {
  channelList.innerHTML = '<div class="muted" style="padding:12px">Loading channels…</div>';
  const query = categoryId ? `?category_id=${encodeURIComponent(categoryId)}` : "";
  state.channels = await api(`/api/channels${query}`);
  filterChannels();
}

function filterChannels() {
  const needle = search.value.trim().toLowerCase();
  state.filteredChannels = needle
    ? state.channels.filter((channel) => channel.name.toLowerCase().includes(needle))
    : state.channels;
  renderChannels();
}

function renderChannels() {
  channelList.innerHTML = "";
  channelCount.textContent = `${state.filteredChannels.length} channel${state.filteredChannels.length === 1 ? "" : "s"}`;
  const fragment = document.createDocumentFragment();
  for (const channel of state.filteredChannels) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `channel-row ${state.streamId === channel.stream_id ? "active" : ""}`;
    button.textContent = channel.name;
    button.addEventListener("click", () => playChannel(channel));
    fragment.appendChild(button);
  }
  channelList.appendChild(fragment);
}

search.addEventListener("input", filterChannels);
$("refreshBtn").addEventListener("click", async () => {
  try {
    await loadCategories();
    await loadChannels(state.categoryId);
    toast("Channel list refreshed", "success");
  } catch (error) {
    toast(error.message);
  }
});

async function playChannel(channel) {
  nowPlaying.textContent = channel.name;
  streamStatus.textContent = "Starting FFmpeg session…";
  sessionInfo.textContent = "Waiting for HLS playlist…";
  stopBtn.disabled = true;

  destroyHls();
  video.removeAttribute("src");
  video.load();

  try {
    const result = await api(`/api/play/${channel.stream_id}`, {
      method: "POST",
      body: JSON.stringify({ mode: playbackMode.value }),
    });
    state.sessionId = result.session_id;
    state.streamId = channel.stream_id;
    renderChannels();

    const codecs = result.source_codecs || {};
    const codecText = [codecs.video, codecs.audio].filter(Boolean).join(" / ");
    streamStatus.textContent = `${result.mode === "copy" ? "Remuxing" : "Transcoding"}${codecText ? ` • source ${codecText}` : ""}`;
    sessionInfo.textContent = `Session ${result.session_id.slice(0, 8)} • stream ${channel.stream_id}`;
    stopBtn.disabled = false;
    attachPlayer(result.playlist);
  } catch (error) {
    state.sessionId = null;
    state.streamId = null;
    renderChannels();
    streamStatus.textContent = "Playback failed";
    sessionInfo.textContent = error.message;
    toast(error.message);
  }
}

function attachPlayer(url) {
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
    video.play().catch(() => {});
    return;
  }

  if (window.Hls && Hls.isSupported()) {
    state.hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 30,
      liveSyncDurationCount: 3,
    });
    state.hls.loadSource(url);
    state.hls.attachMedia(video);
    state.hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    state.hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) state.hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) state.hls.recoverMediaError();
        else toast(`Fatal HLS error: ${data.details}`);
      }
    });
    return;
  }

  toast("This browser does not support HLS playback.");
}

function destroyHls() {
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
}

async function stopPlayback() {
  const sessionId = state.sessionId;
  state.sessionId = null;
  state.streamId = null;
  destroyHls();
  video.pause();
  video.removeAttribute("src");
  video.load();
  stopBtn.disabled = true;
  nowPlaying.textContent = "Nothing playing";
  streamStatus.textContent = "Choose a channel to start a session.";
  sessionInfo.textContent = "No active session";
  renderChannels();
  if (sessionId) {
    try {
      await api(`/api/session/${sessionId}`, { method: "DELETE" });
    } catch (_) {}
  }
}

stopBtn.addEventListener("click", stopPlayback);
window.addEventListener("beforeunload", () => {
  if (state.sessionId) navigator.sendBeacon(`/api/session/${state.sessionId}/stop`);
});

bootstrap();
