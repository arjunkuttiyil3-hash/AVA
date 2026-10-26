(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const chatLog = $("#chat-log");
  const input = $("#message-input");
  const form = $("#chat-form");
  const welcome = $("#welcome-panel");
  const modeHint = $("#mode-hint");
  const socketProtocol = location.protocol === "https:" ? "wss:" : "ws:";
  const modes = {
    chat: "Ask anything, in either language",
    story: "Build a story that remembers its characters",
    anchoring: "Prepare and guide your event, one moment at a time",
  };
  let mode = "chat";
  let sessionId = localStorage.getItem("ava-session-id") || "";
  let socket = null;
  let socketSessionId = "";
  let reconnectAttempts = 0;
  let heartbeatTimer = null;
  let lastPongAt = 0;
  const outboundMessages = [];
  let currentMessage = null;
  let assistantBubble = null;
  let assistantText = "";
  let pendingAudio = [];
  const playedAudio = new Set();
  let audioUnlocked = false;
  let recorder = null;
  let mediaStream = null;
  let activeAudio = null;
  let finishActiveAudio = null;
  let activeUtterance = null;
  let audioPlayback = Promise.resolve();
  let audioGeneration = 0;
  let continuous = false;
  let eventId = Number(localStorage.getItem("ava-event-id")) || 0;
  let connectionState = "connecting";
  const languageVoices = { en: "en-IN-NeerjaNeural", ml: "ml-IN-SobhanaNeural" };

  function syncLanguageToggle(language) {
    const selectedLanguage = language === "ml" ? "ml" : "en";
    $("#language-select").value = selectedLanguage;
    $("#language-en").setAttribute("aria-pressed", String(selectedLanguage === "en"));
    $("#language-ml").setAttribute("aria-pressed", String(selectedLanguage === "ml"));
    document.documentElement.lang = selectedLanguage === "ml" ? "ml" : "en";
    document.body.classList.toggle("language-ml", selectedLanguage === "ml");
    const voiceSetting = $("#voice-setting");
    if (voiceSetting && [...voiceSetting.options].some((option) => option.value === languageVoices[selectedLanguage])) {
      voiceSetting.value = languageVoices[selectedLanguage];
    }
  }

  function toast(message) {
    const item = document.createElement("div");
    item.className = "toast";
    item.textContent = message;
    $("#toast-region").append(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function bubble(role, text = "") {
    const row = document.createElement("div");
    row.className = `message-row ${role}`;
    if (role === "assistant") {
      const badge = document.createElement("span");
      badge.className = "message-avatar";
      badge.textContent = "A";
      row.append(badge);
    }
    const content = document.createElement("div");
    content.className = "message-bubble";
    content.textContent = text;
    row.append(content);
    chatLog.append(row);
    chatLog.scrollTop = chatLog.scrollHeight;
    return content;
  }

  function setConnectionState(state, reason = "") {
    connectionState = state;
    const status = $("#theme-status");
    const label = $("#connection-label");
    status.classList.remove("connected", "connecting", "error");
    status.classList.add(state);
    status.title = reason || state;
    status.setAttribute("aria-label", `Connection ${state}${reason ? `: ${reason}` : ""}`);
    label.textContent = state === "connected"
      ? "Connected"
      : state === "connecting"
        ? "Connecting"
        : "Connection error";
    $("#session-short").textContent = state === "connected"
      ? `Session ${sessionId.slice(0, 8)}`
      : reason || (state === "connecting" ? "Connecting…" : "Connection issue");
    const sessionDot = $("#session-dot");
    sessionDot.classList.remove("connected", "connecting", "error");
    sessionDot.classList.add(state);
    $("#connection-dot").classList.toggle("disconnected", state !== "connected");
    updateDiagnostic("websocket", state === "connected" ? "ready" : state, reason || state);
  }

  function updateDiagnostic(key, state, detail) {
    const row = document.querySelector(`[data-diagnostic="${key}"]`);
    if (!row) return;
    const status = row.querySelector(".diagnostic-state");
    status.className = `diagnostic-state ${state}`;
    status.textContent = state === "ready" ? "Ready"
      : state === "missing" ? "Needs setup"
        : state === "error" ? "Error"
          : state === "connecting" ? "Connecting"
            : state === "configured" ? "Configured" : "Unavailable";
    row.querySelector(".diagnostic-detail").textContent = detail;
  }

  const appShell = $(".app-shell");
  const sidebarToggle = $("#sidebar-toggle");

  function setSidebarCollapsed(collapsed) {
    appShell.classList.toggle("sidebar-collapsed", collapsed);
    sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
    sidebarToggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    sidebarToggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    sidebarToggle.textContent = collapsed ? "⇥" : "⇤";
  }

  try {
    setSidebarCollapsed(localStorage.getItem("ava-sidebar-collapsed") === "true");
  } catch {
    setSidebarCollapsed(false);
  }
  sidebarToggle.addEventListener("click", () => {
    const collapsed = !appShell.classList.contains("sidebar-collapsed");
    setSidebarCollapsed(collapsed);
    try {
      localStorage.setItem("ava-sidebar-collapsed", String(collapsed));
    } catch {
      toast("Sidebar state could not be saved in this browser.");
    }
  });

  function renderDiagnostics(health, runtimeSettings = null) {
    const checks = health.checks || {};
    const list = $("#diagnostics-list");
    list.replaceChildren();
    const rows = [
      ["api", "Backend API", health.api?.available ? "ready" : "error", health.api?.message || "API status unavailable"],
      ["gemini", "Gemini", checks.gemini?.available ? "configured" : "missing", checks.gemini?.message || "Gemini configuration unavailable"],
      ["database", "SQLite memory", checks.database?.available ? "ready" : "error", checks.database?.message || "Database status unavailable"],
      ["websocket", "WebSocket", connectionState === "connected" ? "ready" : connectionState, connectionState === "connected" ? "Connected to this session" : "Waiting for a live connection"],
      ["microphone", "Host microphone", checks.microphone?.available ? "ready" : "unavailable", checks.microphone?.message || "Microphone status unavailable"],
      ["ffmpeg", "ffmpeg", checks.ffmpeg?.available ? "ready" : "unavailable", checks.ffmpeg?.message || "ffmpeg status unavailable"],
      ["speech", "Speech output", "unknown", "Checked when a response is synthesized; fallback depends on installed voices/models"],
    ];
    if (runtimeSettings) {
      const isSimulator = runtimeSettings.robot_mode === "simulated";
      rows.push([
        "robot",
        "Robot",
        isSimulator ? "ready" : "unknown",
        isSimulator ? "Virtual simulator; no physical robot is connected" : `Serial mode configured${runtimeSettings.robot_port ? ` on ${runtimeSettings.robot_port}` : " without a port"}; live connection is not probed`,
      ]);
    }
    rows.forEach(([key, label, state, detail]) => {
      const row = document.createElement("div");
      row.className = "diagnostic-row";
      row.dataset.diagnostic = key;
      const title = document.createElement("strong");
      title.textContent = label;
      const status = document.createElement("span");
      status.className = `diagnostic-state ${state}`;
      status.textContent = state === "ready" ? "Ready"
        : state === "missing" ? "Needs setup"
          : state === "error" ? "Error"
            : state === "connecting" ? "On demand"
              : state === "configured" ? "Configured"
              : state === "unknown" ? "Not probed" : "Unavailable";
      const description = document.createElement("small");
      description.className = "diagnostic-detail";
      description.textContent = detail;
      row.append(title, status, description);
      list.append(row);
    });
  }

  function connect() {
    if (!sessionId) return;
    if (
      socket
      && socketSessionId === sessionId
      && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)
    ) return;
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "Session changed");
    const targetSessionId = sessionId;
    setConnectionState("connecting", "Opening WebSocket");
    const currentSocket = new WebSocket(`${socketProtocol}//${location.host}/ws/${encodeURIComponent(targetSessionId)}`);
    socket = currentSocket;
    socketSessionId = targetSessionId;
    currentSocket.addEventListener("open", () => {
      if (socket !== currentSocket) return;
      reconnectAttempts = 0;
      lastPongAt = Date.now();
      setConnectionState("connected");
      clearInterval(heartbeatTimer);
      heartbeatTimer = window.setInterval(() => {
        if (socket !== currentSocket || currentSocket.readyState !== WebSocket.OPEN) return;
        if (Date.now() - lastPongAt > 45000) {
          setConnectionState("error", "Heartbeat timed out");
          currentSocket.close(4000, "Heartbeat timed out");
          return;
        }
        currentSocket.send(JSON.stringify({ type: "ping", time: Date.now() }));
      }, 15000);
        if (eventId) {
          currentSocket.send(JSON.stringify({ type: "subscribe_event", event_id: eventId }));
        }
        flushMessageQueue();
    });
    currentSocket.addEventListener("message", (event) => {
      if (socket !== currentSocket) return;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        toast("AVA received an unreadable response. Please reconnect.");
        return;
      }
      handleEvent(payload);
    });
    currentSocket.addEventListener("close", (event) => {
      if (socket !== currentSocket) return;
      clearInterval(heartbeatTimer);
      socket = null;
      socketSessionId = "";
      if (currentMessage) {
        currentMessage = null;
        $("#typing-indicator").hidden = true;
        $("#send-button").disabled = false;
        toast("Connection dropped before AVA finished. Please resend your last message.");
      }
      const reason = event.reason || (event.code === 1000 ? "Connection closed" : `Socket closed (${event.code})`);
      setConnectionState(event.code === 1000 ? "connecting" : "error", reason);
      if (sessionId === targetSessionId) {
        const delay = Math.min(500 * (2 ** reconnectAttempts), 30000);
        reconnectAttempts = Math.min(reconnectAttempts + 1, 6);
        window.setTimeout(connect, delay);
      }
    });
    currentSocket.addEventListener("error", () => {
      if (socket !== currentSocket) return;
      setConnectionState("error", "WebSocket transport error");
    });
  }

  function beginAssistant() {
    stopAudio();
    welcome.hidden = true;
    $("#typing-indicator").hidden = false;
    assistantText = "";
    pendingAudio = [];
    playedAudio.clear();
    assistantBubble = bubble("assistant");
    $("#send-button").disabled = true;
  }

  function handleEvent(event) {
    if (event.type === "event_state_changed") {
      const liveEvent = event.event;
      if (!liveEvent) return;
      eventId = liveEvent.id;
      localStorage.setItem("ava-event-id", String(eventId));
      if (
        event.action === "speech_stop"
        || (event.changed_item_id && event.changed_item_id === liveEvent.current_item_id
          && ["absent", "removed", "skipped"].includes(event.changed_status))
      ) stopAudio();
      if (event.action !== "initial") toast("Live event schedule updated.");
      window.dispatchEvent(new CustomEvent("ava-live-event-updated", { detail: liveEvent }));
      const activeState = $("#active-state");
      activeState.replaceChildren();
      const card = document.createElement("div");
      card.className = "state-item";
      const title = document.createElement("strong");
      title.textContent = liveEvent.name;
      const detail = document.createElement("span");
      detail.textContent = `${liveEvent.items.length} items · ${liveEvent.status.replaceAll("_", " ")}`;
      card.append(title, detail);
      activeState.append(card);
      return;
    }
    if (event.type === "speech_stop") {
      stopAudio();
      $("#robot-caption").textContent = "Speech stopped by operator";
      toast("AVA stopped speaking.");
      return;
    }
    if (event.type === "pong") {
      lastPongAt = Date.now();
      return;
    }
    if (event.type === "decision") {
      if (event.language) syncLanguageToggle(event.language);
      $("#think-intent").textContent = event.intent.replaceAll("_", " ");
      $("#think-model").textContent = event.model || "template";
      const snippets = event.retrieved_context || [];
      $("#think-context").textContent = snippets.length ? snippets.join(" · ") : "No older context needed.";
      return;
    }
    if (event.type === "model") {
      $("#think-model").textContent = event.model || event.backend;
      return;
    }
    if (event.type === "token") {
      $("#typing-indicator").hidden = true;
      assistantText += event.text;
      if (assistantBubble) assistantBubble.textContent = assistantText;
      chatLog.scrollTop = chatLog.scrollHeight;
      $("#robot-scene").classList.add("speaking");
      $("#robot-caption").textContent = "AVA is speaking";
      return;
    }
    if (event.type === "reset") {
      stopAudio();
      assistantText = "";
      if (assistantBubble) assistantBubble.textContent = "";
      pendingAudio = [];
      return;
    }
    if (event.type === "audio") {
      const source = event.audio_base64
        ? `data:${event.mime_type || "audio/mpeg"};base64,${event.audio_base64}`
        : event.url;
      const audioItem = {
        source,
        text: event.text || "",
        language: event.language || $("#language-select").value,
      };
      pendingAudio.push(audioItem);
      queueAudio(audioItem);
      return;
    }
    if (event.type === "robot") {
      const scene = $("#robot-scene");
      scene.classList.remove("gesture-wave", "gesture-bow", "gesture-expressive");
      scene.classList.add(`gesture-${event.gesture}`);
      $("#robot-caption").textContent = event.connected
        ? `Gesture: ${event.gesture}`
        : "Simulated gesture";
      return;
    }
    if (event.type === "complete") {
      $("#typing-indicator").hidden = true;
      $("#send-button").disabled = false;
      currentMessage = null;
      $("#think-latency").textContent = `${event.latency_ms} ms`;
      $("#think-model").textContent = event.model || event.backend || "template";
      $("#robot-scene").classList.remove("speaking");
      $("#robot-caption").textContent = "Ready to greet";
      attachAudioButtons();
      flushMessageQueue();
      void refreshMemory();
      return;
    }
    if (event.type === "audio_notice") {
      toast(event.message);
      return;
    }
    if (event.type === "audio_unavailable") {
      $("#robot-scene").classList.remove("speaking");
      if (event.text) {
        toast("Edge TTS is unavailable; using browser speech.");
        const audioItem = {
          source: `speech:${pendingAudio.length}:${event.text}`,
          text: event.text,
          language: event.language || $("#language-select").value,
        };
        pendingAudio.push(audioItem);
        queueSpeech(audioItem);
      } else if (event.message) {
        toast(event.message);
      }
      return;
    }
    if (event.type === "error") {
      $("#typing-indicator").hidden = true;
      $("#send-button").disabled = false;
      currentMessage = null;
      $("#robot-scene").classList.remove("speaking");
      toast(event.message || "AVA could not complete that request.");
      flushMessageQueue();
    }
  }

  function attachAudioButtons() {
    if (!assistantBubble || !pendingAudio.length) return;
    const actions = document.createElement("div");
    actions.className = "message-actions";
    const play = document.createElement("button");
    play.type = "button";
    play.className = "play-audio";
    play.textContent = "▶ Play voice";
    play.addEventListener("click", () => {
      pendingAudio.forEach((item) => playedAudio.delete(item.source));
      void unlockAudio().then(playQueuedAudio);
    });
    actions.append(play);
    assistantBubble.append(actions);
  }

  async function playQueuedAudio() {
    const queued = pendingAudio.filter((item) => !playedAudio.has(item.source));
    if (!queued.length) return;
    for (const item of queued) {
      queueAudio(item);
    }
    await audioPlayback;
  }

  function queueAudio(item) {
    if (!audioUnlocked || playedAudio.has(item.source)) return;
    playedAudio.add(item.source);
    const generation = audioGeneration;
    audioPlayback = audioPlayback.then(() => new Promise((resolve) => {
      if (generation !== audioGeneration) {
        resolve();
        return;
      }
      let fallbackStarted = false;
      const fallback = () => {
        if (fallbackStarted) return;
        fallbackStarted = true;
        speakFallback(item, resolve, generation);
      };
      if (item.source.startsWith("speech:")) {
        fallback();
        return;
      }
      const audio = new Audio(item.source);
      activeAudio = audio;
      finishActiveAudio = resolve;
      $("#robot-scene").classList.add("speaking");
      audio.addEventListener("ended", resolve, { once: true });
      audio.addEventListener("error", () => {
        toast("MP3 playback failed; using browser speech.");
        fallback();
      }, { once: true });
      audio.play().catch(() => {
        toast("MP3 playback failed; using browser speech.");
        fallback();
      });
    })).finally(() => {
      activeAudio = null;
      finishActiveAudio = null;
      $("#robot-scene").classList.remove("speaking");
    });
  }

  function queueSpeech(item) {
    if (!audioUnlocked || playedAudio.has(item.source)) return;
    playedAudio.add(item.source);
    const generation = audioGeneration;
    audioPlayback = audioPlayback.then(() => new Promise((resolve) => {
      speakFallback(item, resolve, generation);
    })).finally(() => {
      activeUtterance = null;
      $("#robot-scene").classList.remove("speaking");
    });
  }

  function speakFallback(item, resolve, generation) {
    if (!audioUnlocked || generation !== audioGeneration || !("speechSynthesis" in window)) {
      if (!("speechSynthesis" in window)) {
        toast("Browser speech synthesis is unavailable. The text response is still available.");
      }
      resolve();
      return;
    }
    const utterance = new SpeechSynthesisUtterance(item.text);
    utterance.lang = item.language === "ml" ? "ml-IN" : "en-IN";
    utterance.onend = resolve;
    utterance.onerror = resolve;
    activeUtterance = utterance;
    $("#robot-scene").classList.add("speaking");
    window.speechSynthesis.speak(utterance);
  }

  async function unlockAudio() {
    if (audioUnlocked) return;
    try {
      const AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
      if (AudioContextConstructor) {
        const audioContext = new AudioContextConstructor();
        await audioContext.resume();
        const oscillator = audioContext.createOscillator();
        const gain = audioContext.createGain();
        gain.gain.value = 0;
        oscillator.connect(gain);
        gain.connect(audioContext.destination);
        oscillator.start();
        oscillator.stop(audioContext.currentTime + 0.01);
      } else {
        const audio = new Audio();
        audio.src = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
        await audio.play();
      }
      audioUnlocked = true;
      $("#enable-audio").textContent = "Audio enabled";
      $("#enable-audio").classList.add("enabled");
      await playQueuedAudio();
    } catch {
      toast("Audio could not be unlocked. Use the Play voice button after allowing browser audio.");
    }
  }

  function stopAudio() {
    audioGeneration += 1;
    activeAudio?.pause();
    finishActiveAudio?.();
    window.speechSynthesis?.cancel();
    activeUtterance = null;
    activeAudio = null;
    finishActiveAudio = null;
    audioPlayback = Promise.resolve();
    pendingAudio = [];
    playedAudio.clear();
    $("#robot-scene").classList.remove("speaking");
  }

  async function sendMessage(text) {
    const message = (text ?? input.value).trim();
    if (!message) return;
    if (eventId) {
      try {
        const proposalResponse = await fetch(`/api/live-events/${eventId}/parse-command`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message }),
        });
        if (proposalResponse.ok) {
          const { proposal } = await proposalResponse.json();
          if (proposal) {
            if (window.confirm(`Mark ${proposal.person_name} as absent and remove them from upcoming announcements?`)) {
              const applied = await fetch(`/api/event/items/${proposal.item_id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ event_id: eventId, status: "absent" }),
              });
              const result = await applied.json().catch(() => ({}));
              if (!applied.ok) {
                throw new Error(result.detail || "Could not apply the event update.");
              }
              await refreshMemory();
              toast(`${proposal.person_name} marked absent. All open AVA pages were updated.`);
            }
            input.value = "";
            input.style.height = "auto";
            return;
          }
        }
      } catch (error) {
        toast(error.message || "Could not check this event command.");
        return;
      }
    }
    outboundMessages.push({
      message,
      mode,
      language: $("#language-select").value,
    });
    input.value = "";
    input.style.height = "auto";
    flushMessageQueue();
  }

  function flushMessageQueue() {
    if (
      currentMessage
      || !outboundMessages.length
      || !socket
      || socket.readyState !== WebSocket.OPEN
    ) return;
    currentMessage = outboundMessages.shift();
    bubble("user", currentMessage.message);
    beginAssistant();
    socket.send(JSON.stringify(currentMessage));
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  });
  document.querySelectorAll(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      mode = tab.dataset.mode;
      document.querySelectorAll(".mode-tab").forEach((item) => {
        const active = item === tab;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      modeHint.textContent = modes[mode];
      $("#anchoring-studio").hidden = mode !== "anchoring";
      $(".workspace").hidden = mode === "anchoring";
      document.querySelectorAll(".side-link[data-nav]").forEach((item) => {
        item.classList.toggle("active", item.dataset.nav === mode);
      });
      input.placeholder = mode === "story"
        ? "What should happen in the story?"
        : mode === "anchoring" ? "What should AVA announce?" : "Ask AVA anything…";
    });
  });
  document.querySelectorAll(".side-link[data-nav]").forEach((link) => {
    link.addEventListener("click", () => {
      const destination = link.dataset.nav;
      if (destination === "chat" || destination === "anchoring" || destination === "overview") {
        document.querySelector(`[data-mode="${destination === "overview" ? "chat" : destination}"]`).click();
      } else if (destination === "voice") {
        $("#open-settings").click();
      } else {
        const target = destination === "robot"
          ? $("#robot-card")
          : destination === "memory" ? $(".think-card") : $("#diagnostics-panel");
        if (destination === "diagnostics") $("#diagnostics-panel").open = true;
        target?.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      document.querySelectorAll(".side-link[data-nav]").forEach((item) => {
        item.classList.toggle("active", item === link);
      });
    });
  });
  document.querySelectorAll(".suggestion").forEach((button) => {
    button.addEventListener("click", () => sendMessage(button.dataset.prompt));
  });

  async function refreshMemory() {
    if (!sessionId) return;
    try {
      const response = await fetch(`/api/memory/${encodeURIComponent(sessionId)}`);
      if (!response.ok) return;
      const data = await response.json();
      $("#memory-count").textContent = `${data.message_count} messages`;
      $("#memory-copy").textContent = data.summary || "Conversation context is saved on this device.";
      const state = $("#active-state");
      state.replaceChildren();
      if (data.story) {
        const story = document.createElement("div");
        story.className = "state-item";
        const title = document.createElement("strong");
        title.textContent = data.story.title;
        const setting = document.createElement("span");
        setting.textContent = data.story.setting || "Story in progress";
        story.append(title, setting);
        state.append(story);
      } else if (data.event) {
        const details = document.createElement("div");
        details.className = "state-item";
        const title = document.createElement("strong");
        title.textContent = data.event.name;
        const detail = document.createElement("span");
        detail.textContent = "Anchoring event saved";
        details.append(title, detail);
        state.append(details);
        if (!eventId) eventId = Number(localStorage.getItem("ava-event-id")) || 0;
        if (window.AVAAnchoringStudio) {
          void window.AVAAnchoringStudio.loadSessionEvent(data.event.id);
        }
      } else {
        state.innerHTML = '<span class="state-empty">No story or event yet</span>';
      }
    } catch {
      $("#memory-copy").textContent = "Saved conversation context will appear here.";
    }
  }

  $("#new-session").addEventListener("click", async () => {
    try {
      const response = await fetch("/api/sessions", { method: "POST" });
      if (!response.ok) throw new Error("Could not create a new session.");
      const data = await response.json();
      sessionId = data.session_id;
      localStorage.setItem("ava-session-id", sessionId);
      localStorage.removeItem("ava-event-id");
      eventId = 0;
      chatLog.replaceChildren();
      window.AVAAnchoringStudio?.resetSession();
      welcome.hidden = false;
      stopAudio();
      connect();
      await refreshMemory();
    } catch (error) {
      toast(error.message || "Could not create a new session.");
    }
  });

  async function ensureSession() {
    if (sessionId) return;
    const response = await fetch("/api/sessions", { method: "POST" });
    if (!response.ok) throw new Error("AVA session could not be started.");
    const data = await response.json();
    sessionId = data.session_id;
    localStorage.setItem("ava-session-id", sessionId);
    window.AVAAnchoringStudio?.setSession(sessionId);
  }

  async function transcribe(blob) {
    if (!blob.size) return;
    $("#microphone-status").textContent = "Transcribing your speech…";
    const body = new FormData();
    body.append("audio", blob, "recording.webm");
    body.append("language", $("#language-select").value);
    try {
      const response = await fetch("/api/transcribe", { method: "POST", body });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Speech recognition is unavailable.");
      if (continuous) {
        if (result.text) sendMessage(result.text);
      } else {
        input.value = `${input.value}${input.value ? " " : ""}${result.text}`;
        input.dispatchEvent(new Event("input"));
        input.focus();
      }
      $("#microphone-status").textContent = continuous ? "Listening continuously…" : "Transcript ready. Review it, then send.";
    } catch (error) {
      $("#microphone-status").textContent = "Microphone is ready when you are.";
      toast(error.message || "Could not transcribe audio.");
    }
  }

  async function startRecording(isContinuous) {
    try {
      const selectedMic = $("#mic-setting").value || localStorage.getItem("ava-mic-id") || "";
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: selectedMic ? { deviceId: { exact: selectedMic } } : true,
      });
      continuous = isContinuous;
      recorder = new MediaRecorder(mediaStream);
      recorder.addEventListener("dataavailable", (event) => {
        void transcribe(event.data);
        if (continuous && recorder?.state === "recording") recorder.stop();
      });
      recorder.addEventListener("stop", () => {
        if (continuous && mediaStream) {
          recorder.start(5000);
          return;
        }
        mediaStream?.getTracks().forEach((track) => track.stop());
        mediaStream = null;
        if (!continuous) $("#microphone-status").textContent = "Transcript will appear in the composer.";
      });
      recorder.start(isContinuous ? 5000 : undefined);
      $("#mic-button").classList.add("recording");
      $("#microphone-status").textContent = isContinuous ? "Listening continuously…" : "Recording. Press the mic again to finish.";
    } catch {
      toast("Microphone permission or audio capture is unavailable.");
    }
  }

  $("#mic-button").addEventListener("click", () => {
    if (recorder?.state === "recording") {
      continuous = false;
      recorder.stop();
      $("#mic-button").classList.remove("recording");
      return;
    }
    void startRecording(false);
  });
  $("#continuous-button").addEventListener("click", (event) => {
    const button = event.currentTarget;
    if (recorder?.state === "recording" && continuous) {
      continuous = false;
      recorder.stop();
      button.setAttribute("aria-pressed", "false");
      button.classList.remove("recording");
      $("#microphone-status").textContent = "Continuous listening stopped.";
      return;
    }
    button.setAttribute("aria-pressed", "true");
    button.classList.add("recording");
    void startRecording(true);
  });

  $("#enable-audio").addEventListener("click", () => void unlockAudio());
  async function stopCurrentSpeech() {
    stopAudio();
    if (socket?.readyState === WebSocket.OPEN && currentMessage) {
      socket.send(JSON.stringify({ type: "stop" }));
    }
    if (eventId) {
      try {
        const response = await fetch(`/api/live-events/${eventId}/stop-speaking`, { method: "POST" });
        if (!response.ok) throw new Error("Server could not stop the event speech.");
      } catch (error) {
        toast(error.message || "AVA stopped browser audio; the event service could not be reached.");
        return;
      }
    }
    toast("AVA stopped speaking.");
  }
  $("#stop-speaking").addEventListener("click", () => void stopCurrentSpeech());

  async function refreshHealth() {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (!response.ok) throw new Error(`Health endpoint returned ${response.status}`);
      const health = await response.json();
      const problems = [];
      const checks = health.checks || {};
      const gemini = checks.gemini;
      const providerStatus = $("#provider-status");
      providerStatus.classList.remove("checking", "ready", "missing");
      providerStatus.classList.add(gemini?.available ? "ready" : "missing");
      providerStatus.textContent = gemini?.available
        ? "Gemini · key configured"
        : "Gemini · not configured";
      $("#think-model").textContent = gemini?.model || "Gemini";
      let runtimeSettings = null;
      try {
        const settingsResponse = await fetch("/api/settings", { cache: "no-store" });
        if (settingsResponse.ok) runtimeSettings = await settingsResponse.json();
      } catch {
        runtimeSettings = null;
      }
      renderDiagnostics(health, runtimeSettings);
      if (!gemini?.available) {
        problems.push(gemini?.message || "Gemini API key is not configured.");
      }
      if (!checks.ffmpeg?.available) problems.push(checks.ffmpeg?.message || "ffmpeg is unavailable.");
      if (!checks.microphone?.available) {
        problems.push(checks.microphone?.message || "Microphone is unavailable; text chat still works.");
      }
      if (!checks.database?.available) problems.push(`SQLite: ${checks.database?.message || "unavailable"}`);
      const banner = $("#health-banner");
      const details = $("#health-details");
      details.replaceChildren();
      for (const message of problems) {
        const item = document.createElement("li");
        item.textContent = message;
        details.append(item);
      }
      banner.hidden = problems.length === 0;
    } catch (error) {
      const providerStatus = $("#provider-status");
      providerStatus.classList.remove("ready", "missing");
      providerStatus.classList.add("checking");
      providerStatus.textContent = "Gemini · status unknown";
      const banner = $("#health-banner");
      const details = $("#health-details");
      details.replaceChildren();
      const item = document.createElement("li");
      item.textContent = `AVA health check failed: ${error.message}`;
      details.append(item);
      banner.hidden = false;
    }
  }

  window.addEventListener("online", () => {
    setConnectionState("connecting", "Network restored");
    connect();
  });

  const settingsDialog = $("#settings-dialog");
  $("#open-settings").addEventListener("click", async () => {
    settingsDialog.showModal();
    try {
      const [settingsResponse, preferencesResponse, devices] = await Promise.all([
        fetch("/api/settings"),
        fetch(`/api/preferences/${encodeURIComponent(sessionId)}`),
        navigator.mediaDevices?.enumerateDevices?.() ?? Promise.resolve([]),
      ]);
      if (settingsResponse.ok) {
        const settings = await settingsResponse.json();
        $("#model-setting").value = settings.model;
        $("#robot-setting").value = settings.robot_mode;
        $("#robot-port-setting").value = settings.robot_port || "Not configured";
        const voices = $("#voice-setting");
        voices.replaceChildren();
        Object.entries(settings.voices).forEach(([language, voice]) => {
          const option = document.createElement("option");
          option.value = voice;
          option.textContent = `${language.toUpperCase()} · ${voice}`;
          voices.append(option);
        });
        if (preferencesResponse.ok) {
          const preferences = await preferencesResponse.json();
          syncLanguageToggle(preferences.language);
          voices.value = preferences.voice || languageVoices[preferences.language];
        }
      }
      const microphones = $("#mic-setting");
      microphones.replaceChildren(new Option("Default microphone", ""));
      devices.filter((device) => device.kind === "audioinput").forEach((device, index) => {
        microphones.add(new Option(device.label || `Microphone ${index + 1}`, device.deviceId));
      });
      microphones.value = localStorage.getItem("ava-mic-id") || "";
    } catch {
      toast("Some settings are unavailable until AVA reconnects.");
    }
  });
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const response = await fetch(`/api/preferences/${encodeURIComponent(sessionId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          language: $("#language-select").value,
          style: "Professional",
          voice: $("#voice-setting").value,
        }),
      });
      if (!response.ok) throw new Error("Could not save preferences.");
      settingsDialog.close();
      toast("Your preferences are saved.");
    } catch (error) {
      toast(error.message || "Could not save preferences.");
    }
  });
  $("#mic-setting").addEventListener("change", (event) => {
    localStorage.setItem("ava-mic-id", event.target.value);
  });

  $("#language-select").addEventListener("change", async (event) => {
    const language = event.target.value === "ml" ? "ml" : "en";
    syncLanguageToggle(language);
    if (!sessionId) return;
    try {
      const response = await fetch(`/api/preferences/${encodeURIComponent(sessionId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          language,
          style: "Professional",
          voice: languageVoices[language],
        }),
      });
      if (!response.ok) throw new Error("Language preference could not be saved.");
    } catch {
      toast("Language preference could not be saved.");
    }
  });
  $("#language-en").addEventListener("click", () => {
    $("#language-select").value = "en";
    $("#language-select").dispatchEvent(new Event("change"));
  });
  $("#language-ml").addEventListener("click", () => {
    $("#language-select").value = "ml";
    $("#language-select").dispatchEvent(new Event("change"));
  });

  ensureSession().then(async () => {
    try {
      const response = await fetch(`/api/preferences/${encodeURIComponent(sessionId)}`);
      if (response.ok) {
        const preferences = await response.json();
        if (preferences.language) syncLanguageToggle(preferences.language);
      }
    } catch {
      toast("Saved language preference could not be loaded.");
    }
    connect();
    void refreshMemory();
    void refreshHealth();
    window.setInterval(() => void refreshHealth(), 30000);
    if (eventId) {
      document.querySelector('[data-mode="anchoring"]').click();
    }
  }).catch((error) => toast(error.message));
})();
