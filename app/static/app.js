const CHANNEL_PAGE_SIZE = 150;

const state = {
  categories: [],
  channels: [],
  channelTotal: 0,
  channelOffset: 0,
  channelLoading: false,
  channelDone: false,
  channelLoadSerial: 0,
  categoryId: null,
  sessionId: null,
  streamId: null,
  hls: null,
  configSource: null,
  starting: false,
  pendingStreamId: null,
  playSerial: 0,
  currentChannel: null,
  lastPlayResult: null,
  autoFallbackAttempted: false,
  hlsNetworkRecoveries: 0,
  baseStreamStatus: "",
  catalogPollTimer: null,
  epgPollTimer: null,
  epgLoadSerial: 0,
  searchTimer: null,
};

const $ = (id) => document.getElementById(id);
const setupPanel = $("setupPanel");
const appPanel = $("appPanel");
const setupForm = $("setupForm");
const setupMessage = $("setupMessage");
const categoriesEl = $("categories");
const channelList = $("channelList");
const channelCount = $("channelCount");
const catalogStatus = $("catalogStatus");
const search = $("search");
const video = $("video");
const nowPlaying = $("nowPlaying");
const streamStatus = $("streamStatus");
const sessionInfo = $("sessionInfo");
const stopBtn = $("stopBtn");
const playbackMode = $("playbackMode");
const settingsBtn = $("settingsBtn");
const refreshBtn = $("refreshBtn");
const updateBadge = $("updateBadge");
const popoutBtn = $("popoutBtn");
const epgList = $("epgList");
const epgStatus = $("epgStatus");
const epgRefreshBtn = $("epgRefreshBtn");

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

async function reportClientEvent(event, detail = "", level = "warning") {
  if (!state.sessionId) return;
  try {
    await fetch(`/api/session/${state.sessionId}/client-event`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, detail: String(detail || "").slice(0, 2000), level }),
      keepalive: true,
    });
  } catch (_) {}
}

function mediaErrorText() {
  const error = video.error;
  if (!error) return "unknown media element error";
  const labels = {
    1: "MEDIA_ERR_ABORTED",
    2: "MEDIA_ERR_NETWORK",
    3: "MEDIA_ERR_DECODE",
    4: "MEDIA_ERR_SRC_NOT_SUPPORTED",
  };
  return `${labels[error.code] || `MEDIA_ERR_${error.code}`}${error.message ? `: ${error.message}` : ""}`;
}

async function attemptVideoPlay() {
  try {
    await video.play();
    return true;
  } catch (error) {
    const detail = `${error?.name || "PlayError"}: ${error?.message || error}`;
    await reportClientEvent("video-play-rejected", detail);
    if (error?.name === "NotAllowedError") {
      streamStatus.textContent = `${state.baseStreamStatus} • ready — press ▶ Play`;
      toast("The browser blocked automatic playback. Press Play in the video controls.", "success");
    } else {
      streamStatus.textContent = `${state.baseStreamStatus} • browser play error`;
      toast(`Browser could not start video: ${detail}`);
    }
    return false;
  }
}

async function forceBrowserSafeTranscode(reason) {
  const channel = state.currentChannel;
  if (!channel || state.starting || state.autoFallbackAttempted) return;
  if (!state.lastPlayResult || state.lastPlayResult.mode !== "copy") return;
  if (playbackMode.value !== "auto") return;

  state.autoFallbackAttempted = true;
  await reportClientEvent("auto-transcode-fallback", reason, "warning");
  toast("Browser rejected the remuxed stream; retrying in browser-safe transcode mode.", "success");
  await playChannel(channel, { modeOverride: "transcode", forceRestart: true, isFallback: true });
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

function updateCatalogStatus(status) {
  if (!status) return;
  if (status.refreshing) {
    catalogStatus.textContent = "Syncing provider in background…";
    refreshBtn.classList.add("spinning");
  } else if (status.ready) {
    const age = status.age_seconds == null ? "" : ` • ${Math.round(status.age_seconds)}s old`;
    catalogStatus.textContent = `${status.channel_count.toLocaleString()} cached channels${age}`;
    refreshBtn.classList.remove("spinning");
  } else if (status.last_error) {
    catalogStatus.textContent = `Sync failed: ${status.last_error}`;
    refreshBtn.classList.remove("spinning");
  } else {
    catalogStatus.textContent = "Waiting for provider catalogue…";
    refreshBtn.classList.remove("spinning");
  }
}

async function getCatalogStatus() {
  const status = await api("/api/catalog/status");
  updateCatalogStatus(status);
  return status;
}

function scheduleCatalogPoll(delay = 750) {
  clearTimeout(state.catalogPollTimer);
  state.catalogPollTimer = setTimeout(pollCatalog, delay);
}

async function pollCatalog() {
  try {
    const status = await getCatalogStatus();
    if (!status.ready && !status.refreshing) {
      await api("/api/catalog/refresh", { method: "POST" });
      scheduleCatalogPoll(status.last_error ? 3000 : 750);
      return;
    }
    if (status.refreshing) {
      scheduleCatalogPoll(750);
      return;
    }
    await Promise.all([loadCategories(), loadChannels(state.categoryId, { reset: true })]);
  } catch (error) {
    catalogStatus.textContent = `Catalogue status unavailable: ${error.message}`;
    scheduleCatalogPoll(3000);
  }
}

async function ensureCatalog() {
  const status = await getCatalogStatus();
  if (!status.ready && !status.refreshing) {
    await api("/api/catalog/refresh", { method: "POST" });
    updateCatalogStatus({ ...status, refreshing: true });
  }
  if (status.refreshing || !status.ready) scheduleCatalogPoll();
}

function humanAge(seconds) {
  if (seconds == null) return "never";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(seconds < 7200 ? 1 : 0)}h ago`;
}

function humanInterval(seconds) {
  if (!seconds) return "";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const hours = seconds / 3600;
  return `${Number.isInteger(hours) ? hours : hours.toFixed(1)}h`;
}

function formatEpgTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function updateEpgStatus(status) {
  if (!status) return;
  const interval = humanInterval(status.refresh_interval);
  if (status.refreshing) {
    epgStatus.textContent = `EPG syncing in background${interval ? ` • every ${interval}` : ""}`;
    epgRefreshBtn.classList.add("spinning");
  } else if (status.ready) {
    epgStatus.textContent = `${status.programme_count.toLocaleString()} programmes • ${humanAge(status.age_seconds)}${interval ? ` • every ${interval}` : ""}`;
    epgRefreshBtn.classList.remove("spinning");
  } else if (status.last_error) {
    epgStatus.textContent = `EPG sync failed: ${status.last_error}`;
    epgRefreshBtn.classList.remove("spinning");
  } else {
    epgStatus.textContent = `Waiting for EPG cache${interval ? ` • every ${interval}` : ""}`;
    epgRefreshBtn.classList.remove("spinning");
  }
}

async function getEpgStatus() {
  const status = await api("/api/epg/status");
  updateEpgStatus(status);
  return status;
}

function scheduleEpgPoll(delay = 1500) {
  clearTimeout(state.epgPollTimer);
  state.epgPollTimer = setTimeout(pollEpg, delay);
}

async function pollEpg() {
  try {
    const status = await getEpgStatus();
    if (!status.ready && !status.refreshing) {
      const result = await api("/api/epg/refresh", { method: "POST" });
      updateEpgStatus({ ...result, refreshing: result.started || result.refreshing });
      scheduleEpgPoll(result.last_error ? 5000 : 1500);
      return;
    }
    if (status.refreshing) {
      scheduleEpgPoll(1500);
      return;
    }
    if (state.currentChannel) await loadEpg(state.currentChannel.stream_id);
    await loadChannels(state.categoryId, { reset: true });
  } catch (error) {
    epgStatus.textContent = `EPG status unavailable: ${error.message}`;
    scheduleEpgPoll(5000);
  }
}

async function ensureEpg() {
  const status = await getEpgStatus();
  if (!status.ready && !status.refreshing) {
    const result = await api("/api/epg/refresh", { method: "POST" });
    updateEpgStatus({ ...result, refreshing: result.started || result.refreshing });
  }
  if (!status.ready || status.refreshing) scheduleEpgPoll();
}

function renderEpg(items, mapped = true) {
  epgList.innerHTML = "";
  if (!mapped) {
    epgList.innerHTML = '<div class="epg-empty muted">This channel has no EPG mapping from the provider.</div>';
    return;
  }
  if (!items.length) {
    epgList.innerHTML = '<div class="epg-empty muted">No cached programme data for this channel yet.</div>';
    return;
  }

  const now = Date.now() / 1000;
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const current = item.start_ts <= now && item.stop_ts > now;
    const row = document.createElement("div");
    row.className = `epg-row ${current ? "current" : ""}`;

    const time = document.createElement("div");
    time.className = "epg-time";
    time.textContent = current ? `NOW • ${formatEpgTime(item.stop_ts)}` : `${formatEpgTime(item.start_ts)}–${formatEpgTime(item.stop_ts)}`;

    const info = document.createElement("div");
    const title = document.createElement("div");
    title.className = "epg-title";
    title.textContent = item.title || "Untitled";
    info.appendChild(title);

    if (item.description) {
      const desc = document.createElement("div");
      desc.className = "epg-desc";
      desc.textContent = item.description;
      desc.title = item.description;
      info.appendChild(desc);
    }

    if (current) {
      const total = Math.max(1, item.stop_ts - item.start_ts);
      const pct = Math.max(0, Math.min(100, ((now - item.start_ts) / total) * 100));
      const progress = document.createElement("div");
      progress.className = "epg-progress";
      const fill = document.createElement("span");
      fill.style.width = `${pct}%`;
      progress.appendChild(fill);
      info.appendChild(progress);
    }

    row.append(time, info);
    fragment.appendChild(row);
  }
  epgList.appendChild(fragment);
}

async function loadEpg(streamId) {
  const serial = ++state.epgLoadSerial;
  try {
    const result = await api(`/api/epg/channel/${streamId}`);
    if (serial !== state.epgLoadSerial || !state.currentChannel || state.currentChannel.stream_id !== streamId) return;
    updateEpgStatus(result.status);
    renderEpg(result.items || [], result.mapped);
    if (!result.status?.ready && !result.status?.refreshing) ensureEpg().catch(() => {});
  } catch (error) {
    if (serial !== state.epgLoadSerial) return;
    epgList.innerHTML = `<div class="epg-empty muted">EPG unavailable: ${error.message}</div>`;
  }
}

async function loadUpdateStatus() {
  try {
    const info = await api("/api/update/status");
    updateBadge.classList.toggle("hidden", !info.available);
    if (info.available) {
      updateBadge.textContent = info.message || "Update available";
      updateBadge.href = info.url || `https://github.com/${info.repository}`;
      const latest = info.latest_version ? `latest v${info.latest_version}` : info.latest_commit ? `latest ${info.latest_commit.slice(0, 7)}` : "newer build available";
      updateBadge.title = `Running v${info.current_version} • ${latest}`;
    }
  } catch (_) {
    updateBadge.classList.add("hidden");
  }
}

async function bootstrap() {
  try {
    const status = await api("/api/status");
    $("version").textContent = `v${status.version}`;
    $("version").title = status.channel === "edge" && status.commit ? `Edge build ${status.commit.slice(0, 7)}` : `Xtream Online v${status.version}`;
    loadUpdateStatus();
    setInterval(loadUpdateStatus, 15 * 60 * 1000);
    state.configSource = status.configuration_source;
    if (status.configured) {
      setConfigured(true);
      playbackMode.value = ["auto", "copy", "transcode"].includes(status.default_ffmpeg_mode)
        ? status.default_ffmpeg_mode
        : "auto";
      // These endpoints read only the local SQLite cache and never contact the provider.
      await Promise.all([loadCategories(), loadChannels(null, { reset: true })]);
      ensureCatalog().catch((error) => toast(`Catalogue sync failed: ${error.message}`));
      ensureEpg().catch((error) => { epgStatus.textContent = `EPG sync failed: ${error.message}`; });
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
    setupMessage.textContent = "Connected. Catalogue syncing in background…";
    setConfigured(true);
    state.categories = [];
    state.channels = [];
    renderCategories();
    renderChannels();
    await ensureCatalog();
    toast("Provider connected — catalogue sync started", "success");
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
  const items = await api("/api/categories");
  state.categories = items;
  renderCategories();
}

function renderCategories() {
  categoriesEl.innerHTML = "";
  const fragment = document.createDocumentFragment();
  fragment.appendChild(categoryButton(null, "All channels"));
  for (const category of state.categories) {
    fragment.appendChild(categoryButton(category.category_id, category.category_name));
  }
  if (!state.categories.length) {
    const note = document.createElement("div");
    note.className = "muted list-note";
    note.textContent = "Catalogue is syncing in the background…";
    fragment.appendChild(note);
  }
  categoriesEl.appendChild(fragment);
}

function categoryButton(id, name) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `category-button ${state.categoryId === id ? "active" : ""}`;
  button.textContent = name;
  button.addEventListener("click", () => {
    state.categoryId = id;
    renderCategories();
    loadChannels(id, { reset: true }).catch((error) => toast(error.message));
  });
  return button;
}

function channelQueryUrl(categoryId, offset) {
  const params = new URLSearchParams({
    offset: String(offset),
    limit: String(CHANNEL_PAGE_SIZE),
  });
  if (categoryId) params.set("category_id", categoryId);
  const needle = search.value.trim();
  if (needle) params.set("q", needle);
  return `/api/channels?${params.toString()}`;
}

async function loadChannels(categoryId, { reset = false } = {}) {
  if (state.channelLoading && !reset) return;

  if (reset) {
    state.channelLoadSerial += 1;
    state.channels = [];
    state.channelTotal = 0;
    state.channelOffset = 0;
    state.channelDone = false;
    channelList.scrollTop = 0;
    renderChannels();
  }
  if (state.channelDone) return;

  const serial = state.channelLoadSerial;
  const offset = state.channelOffset;
  state.channelLoading = true;
  renderChannelFooter();
  try {
    const result = await api(channelQueryUrl(categoryId, offset));
    if (serial !== state.channelLoadSerial) return;
    state.channels.push(...result.items);
    state.channelTotal = result.total;
    state.channelOffset = state.channels.length;
    state.channelDone = state.channelOffset >= state.channelTotal;
    renderChannels();
    if (!result.ready && !result.refreshing) ensureCatalog().catch(() => {});
  } finally {
    if (serial === state.channelLoadSerial) {
      state.channelLoading = false;
      renderChannelFooter();
    }
  }
}

function renderChannels() {
  channelList.innerHTML = "";
  channelCount.textContent = `${state.channelTotal.toLocaleString()} channel${state.channelTotal === 1 ? "" : "s"}`;

  const fragment = document.createDocumentFragment();
  for (const channel of state.channels) {
    const button = document.createElement("button");
    button.type = "button";
    const isCurrent = state.streamId === channel.stream_id;
    const isPending = state.starting && state.pendingStreamId === channel.stream_id;
    button.className = `channel-row ${isCurrent || isPending ? "active" : ""}`;
    const name = document.createElement("span");
    name.className = "channel-name";
    name.textContent = isPending ? `${channel.name} …` : channel.name;
    button.appendChild(name);
    if (channel.now?.title) {
      const now = document.createElement("span");
      now.className = "channel-now";
      now.textContent = `Now: ${channel.now.title}`;
      button.appendChild(now);
    }
    button.disabled = state.starting;
    button.addEventListener("click", () => playChannel(channel));
    fragment.appendChild(button);
  }
  channelList.appendChild(fragment);
  renderChannelFooter();
}

function renderChannelFooter() {
  channelList.querySelector(".channel-load-state")?.remove();
  if (!state.channels.length || state.channelLoading || !state.channelDone) {
    const footer = document.createElement("div");
    footer.className = "channel-load-state muted";
    if (state.channelLoading) footer.textContent = "Loading cached channels…";
    else if (!state.channels.length) footer.textContent = "No cached channels yet.";
    else footer.textContent = `${state.channels.length.toLocaleString()} of ${state.channelTotal.toLocaleString()} loaded`;
    channelList.appendChild(footer);
  }
}

channelList.addEventListener("scroll", () => {
  const remaining = channelList.scrollHeight - channelList.scrollTop - channelList.clientHeight;
  if (remaining < 500 && !state.channelLoading && !state.channelDone) {
    loadChannels(state.categoryId).catch((error) => toast(error.message));
  }
});

search.addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    loadChannels(state.categoryId, { reset: true }).catch((error) => toast(error.message));
  }, 250);
});

refreshBtn.addEventListener("click", async () => {
  if (refreshBtn.disabled) return;
  refreshBtn.disabled = true;
  try {
    const result = await api("/api/catalog/refresh", { method: "POST" });
    updateCatalogStatus({ ...result, refreshing: true });
    toast(result.started ? "Provider catalogue refresh started" : "Catalogue refresh already running", "success");
    scheduleCatalogPoll(400);
  } catch (error) {
    toast(error.message);
  } finally {
    refreshBtn.disabled = false;
  }
});

async function playChannel(channel, options = {}) {
  if (state.starting) return;
  if (state.sessionId && state.streamId === channel.stream_id && !options.forceRestart) return;

  if (!options.isFallback) state.autoFallbackAttempted = false;
  state.currentChannel = channel;
  loadEpg(channel.stream_id).catch(() => {});
  state.lastPlayResult = null;
  state.hlsNetworkRecoveries = 0;

  const serial = ++state.playSerial;
  const previousSession = state.sessionId;
  state.starting = true;
  state.pendingStreamId = channel.stream_id;
  state.sessionId = null;
  state.streamId = null;

  nowPlaying.textContent = channel.name;
  streamStatus.textContent = options.isFallback ? "Retrying with browser-safe transcode…" : "Starting FFmpeg session…";
  sessionInfo.textContent = "Opening provider stream…";
  stopBtn.disabled = true;
  popoutBtn.disabled = true;
  renderChannels();

  destroyHls();
  video.pause();
  video.removeAttribute("src");
  video.load();

  try {
    // Explicitly release the old provider connection before starting a new one.
    if (previousSession) {
      try {
        await api(`/api/session/${previousSession}`, { method: "DELETE" });
      } catch (_) {}
    }

    const result = await api(`/api/play/${channel.stream_id}`, {
      method: "POST",
      body: JSON.stringify({ mode: options.modeOverride || playbackMode.value }),
    });

    // A stale response should never steal playback from a newer request.
    if (serial !== state.playSerial) {
      try {
        await api(`/api/session/${result.session_id}`, { method: "DELETE" });
      } catch (_) {}
      return;
    }

    state.sessionId = result.session_id;
    state.streamId = channel.stream_id;
    state.lastPlayResult = result;

    const codecs = result.source_codecs || {};
    const codecText = [codecs.video, codecs.audio].filter(Boolean).join(" / ");
    state.baseStreamStatus = `${result.mode === "copy" ? "Remuxing" : "Transcoding"}${codecText ? ` • source ${codecText}` : ""}`;
    streamStatus.textContent = `${state.baseStreamStatus} • loading player…`;
    sessionInfo.textContent = `Session ${result.session_id.slice(0, 8)} • stream ${channel.stream_id}`;
    stopBtn.disabled = false;
    popoutBtn.disabled = false;
    attachPlayer(result.playlist, channel, result);
  } catch (error) {
    if (serial === state.playSerial) {
      state.sessionId = null;
      state.streamId = null;
      state.lastPlayResult = null;
      streamStatus.textContent = "Playback failed";
      sessionInfo.textContent = error.message;
      toast(error.message);
    }
  } finally {
    if (serial === state.playSerial) {
      state.starting = false;
      state.pendingStreamId = null;
      renderChannels();
    }
  }
}

function hlsErrorDetail(data) {
  return [
    `type=${data.type || "unknown"}`,
    `details=${data.details || "unknown"}`,
    data.reason ? `reason=${data.reason}` : "",
    data.error?.message ? `error=${data.error.message}` : "",
    data.response?.code ? `http=${data.response.code}` : "",
  ].filter(Boolean).join(" ");
}

function attachPlayer(url, channel, result) {
  // Prefer hls.js anywhere MediaSource is available. Some Chromium builds
  // report a truthy native-HLS canPlayType() result but then hand MPEG-TS HLS
  // to the platform media pipeline, which can fail with
  // DEMUXER_ERROR_COULD_NOT_PARSE. hls.js reliably transmuxes TS to fMP4/MSE.
  if (window.Hls && Hls.isSupported()) {
    reportClientEvent("player-path", "hls.js/MSE", "info");
    state.hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 30,
      liveSyncDurationCount: 3,
    });

    state.hls.on(Hls.Events.MEDIA_ATTACHED, () => {
      reportClientEvent("hls-media-attached", "MediaSource attached", "info");
      state.hls?.loadSource(url);
    });
    state.hls.on(Hls.Events.MANIFEST_PARSED, (_, data) => {
      const levels = data?.levels?.length ?? 0;
      reportClientEvent("hls-manifest-parsed", `levels=${levels}`, "info");
      streamStatus.textContent = `${state.baseStreamStatus} • buffered`;
      attemptVideoPlay();
    });
    state.hls.on(Hls.Events.ERROR, async (_, data) => {
      const detail = hlsErrorDetail(data);
      if (data.fatal) {
        await reportClientEvent("hls-fatal", detail);
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR && state.hlsNetworkRecoveries < 2) {
          state.hlsNetworkRecoveries += 1;
          streamStatus.textContent = `${state.baseStreamStatus} • recovering network (${state.hlsNetworkRecoveries}/2)…`;
          state.hls?.startLoad();
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          streamStatus.textContent = `${state.baseStreamStatus} • browser media error`;
          await forceBrowserSafeTranscode(detail);
          if (!state.autoFallbackAttempted) {
            state.hls?.recoverMediaError();
          }
        } else {
          streamStatus.textContent = `${state.baseStreamStatus} • fatal HLS error`;
          toast(`Fatal HLS error: ${data.details}`);
        }
      } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        reportClientEvent("hls-media-warning", detail, "info");
      }
    });

    // Load the playlist after MediaSource attachment so player startup follows
    // the canonical hls.js lifecycle and gives us deterministic diagnostics.
    state.hls.attachMedia(video);
    return;
  }

  // Native HLS is the fallback path for browsers such as Safari where MSE /
  // hls.js is unavailable but the media element has a real native HLS stack.
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    reportClientEvent("player-path", "native-HLS fallback", "info");
    video.src = url;
    video.addEventListener("loadedmetadata", () => attemptVideoPlay(), { once: true });
    return;
  }

  streamStatus.textContent = "Browser has no HLS/MSE support";
  toast("This browser does not support HLS playback.");
  reportClientEvent("hls-unsupported", navigator.userAgent || "unknown browser");
}

function destroyHls() {
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
}

async function stopPlayback() {
  state.playSerial += 1;
  state.starting = false;
  state.pendingStreamId = null;
  const sessionId = state.sessionId;
  state.sessionId = null;
  state.streamId = null;
  state.currentChannel = null;
  state.lastPlayResult = null;
  state.autoFallbackAttempted = false;
  state.baseStreamStatus = "";
  destroyHls();
  video.pause();
  video.removeAttribute("src");
  video.load();
  stopBtn.disabled = true;
  popoutBtn.disabled = true;
  nowPlaying.textContent = "Nothing playing";
  streamStatus.textContent = "Choose a channel to start a session.";
  epgList.innerHTML = '<div class="epg-empty muted">Choose a channel to view its schedule.</div>';
  sessionInfo.textContent = "No active session";
  renderChannels();
  if (sessionId) {
    try {
      await api(`/api/session/${sessionId}`, { method: "DELETE" });
    } catch (_) {}
  }
}

async function togglePictureInPicture() {
  if (!state.sessionId) return;
  if (!document.pictureInPictureEnabled || typeof video.requestPictureInPicture !== "function") {
    toast("Picture-in-Picture is not supported by this browser.");
    return;
  }
  try {
    if (document.pictureInPictureElement) {
      await document.exitPictureInPicture();
    } else {
      if (video.readyState < 1) {
        toast("Wait for the channel to begin playing before popping it out.");
        return;
      }
      await video.requestPictureInPicture();
    }
  } catch (error) {
    toast(`Could not pop out player: ${error?.message || error}`);
  }
}

popoutBtn.addEventListener("click", togglePictureInPicture);
video.addEventListener("enterpictureinpicture", () => {
  popoutBtn.textContent = "Dock player";
  reportClientEvent("picture-in-picture", "entered", "info");
});
video.addEventListener("leavepictureinpicture", () => {
  popoutBtn.textContent = "Pop out";
  reportClientEvent("picture-in-picture", "left", "info");
});

video.addEventListener("playing", () => {
  if (state.sessionId) {
    streamStatus.textContent = `${state.baseStreamStatus} • playing`;
    reportClientEvent("video-playing", `readyState=${video.readyState}`, "info");
  }
});

video.addEventListener("waiting", () => {
  if (state.sessionId) {
    streamStatus.textContent = `${state.baseStreamStatus} • buffering…`;
  }
});

video.addEventListener("stalled", () => {
  if (state.sessionId) reportClientEvent("video-stalled", `readyState=${video.readyState} networkState=${video.networkState}`);
});

video.addEventListener("error", async () => {
  if (!state.sessionId) return;
  const detail = mediaErrorText();
  streamStatus.textContent = `${state.baseStreamStatus} • ${detail}`;
  await reportClientEvent("video-error", detail);
  await forceBrowserSafeTranscode(detail);
});

epgRefreshBtn.addEventListener("click", async () => {
  if (epgRefreshBtn.disabled) return;
  epgRefreshBtn.disabled = true;
  try {
    const result = await api("/api/epg/refresh", { method: "POST" });
    updateEpgStatus({ ...result, refreshing: result.started || result.refreshing });
    toast(result.started ? "EPG refresh started in background" : "EPG refresh already running", "success");
    scheduleEpgPoll(500);
  } catch (error) {
    toast(error.message);
  } finally {
    epgRefreshBtn.disabled = false;
  }
});

stopBtn.addEventListener("click", stopPlayback);
window.addEventListener("beforeunload", () => {
  if (state.sessionId) navigator.sendBeacon(`/api/session/${state.sessionId}/stop`);
});

bootstrap();
