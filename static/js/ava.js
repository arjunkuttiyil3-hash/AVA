(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const chatLog = $("#chat-log");
  const input = $("#message-input");
  const form = $("#chat-form");
  const welcome = $("#welcome-panel");
  const modeHint = $("#mode-hint");
  const eventCard = $("#event-card");
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
  let audioPlayback = Promise.resolve();
  let audioGeneration = 0;
  let continuous = false;
  let eventId = Number(localStorage.getItem("ava-event-id")) || 0;
  let studioSections = [];
  let selectedSectionId = "";
  let studioGenerationTarget = null;
  let nextSectionId = 1;
  let connectionState = "connecting";

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
        studioGenerationTarget = null;
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
    assistantBubble = bubble("assistant");
    $("#send-button").disabled = true;
  }

  function handleEvent(event) {
    if (event.type === "event_state_changed") {
      const liveEvent = event.event;
      if (!liveEvent) return;
      eventId = liveEvent.id;
      localStorage.setItem("ava-event-id", String(eventId));
      const current = liveEvent.items.find((item) => item.id === liveEvent.current_item_id);
      if (
        event.action === "speech_stop"
        || (event.changed_item_id && event.changed_item_id === liveEvent.current_item_id
          && ["absent", "removed", "skipped"].includes(event.changed_status))
      ) stopAudio();
      if (event.action !== "initial") toast("Live event schedule updated.");
      renderSchedule(
        liveEvent.items,
        current ? liveEvent.items.findIndex((item) => item.id === current.id) : -1,
      );
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
      if (studioGenerationTarget) return;
      pendingAudio.push(event.url);
      queueAudio(event.url);
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
      if (studioGenerationTarget) {
        if (event.backend === "template") {
          toast("Gemini did not generate this script; the template fallback was not saved to your draft.");
        } else {
          applyGeneratedScript(assistantText, studioGenerationTarget);
        }
        studioGenerationTarget = null;
      }
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
      return;
    }
    if (event.type === "error") {
      studioGenerationTarget = null;
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
      pendingAudio.forEach((url) => playedAudio.delete(url));
      void unlockAudio().then(playQueuedAudio);
    });
    actions.append(play);
    assistantBubble.append(actions);
  }

  async function playQueuedAudio() {
    const urls = pendingAudio.filter((url) => !playedAudio.has(url));
    if (!urls.length) return;
    for (const url of urls) {
      queueAudio(url);
    }
    await audioPlayback;
  }

  function queueAudio(url) {
    if (!audioUnlocked || playedAudio.has(url)) return;
    playedAudio.add(url);
    const generation = audioGeneration;
    audioPlayback = audioPlayback.then(() => new Promise((resolve) => {
      if (generation !== audioGeneration) {
        resolve();
        return;
      }
      const audio = new Audio(url);
      activeAudio = audio;
      finishActiveAudio = resolve;
      $("#robot-scene").classList.add("speaking");
      audio.addEventListener("ended", resolve, { once: true });
      audio.addEventListener("error", resolve, { once: true });
      audio.play().catch(() => {
        toast("Audio could not play. Your text response is still available.");
        resolve();
      });
    })).finally(() => {
      activeAudio = null;
      finishActiveAudio = null;
      $("#robot-scene").classList.remove("speaking");
    });
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
    activeAudio = null;
    finishActiveAudio = null;
    audioPlayback = Promise.resolve();
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
              if (!applied.ok) throw new Error("Could not apply the event update.");
            }
            input.value = "";
            input.style.height = "auto";
            return;
          }
        }
      } catch (error) {
        studioGenerationTarget = null;
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
  const studioForm = $("#studio-form");
  const scriptSectionList = $("#script-sections");
  const defaultScriptSections = [
    { id: "section-1", title: "Opening", text: "" },
    { id: "section-2", title: "Introduction", text: "" },
    { id: "section-3", title: "Main Segment", text: "" },
    { id: "section-4", title: "Transition", text: "" },
    { id: "section-5", title: "Announcement", text: "" },
    { id: "section-6", title: "Closing", text: "" },
  ];

  function selectedScriptSection() {
    return studioSections.find((section) => section.id === selectedSectionId) || null;
  }

  function updateScriptMetrics() {
    const fullText = studioSections.map((section) => section.text).join(" ").trim();
    const words = fullText ? fullText.split(/\s+/).length : 0;
    $("#script-word-count").textContent = `${words} ${words === 1 ? "word" : "words"}`;
    $("#script-character-count").textContent = `${fullText.length} characters`;
    $("#preview-duration").textContent = `Estimated time · ${words ? `${Math.max(1, Math.ceil(words / 130))} min` : "—"}`;
    $("#preview-language").textContent = $("#language-select").value === "ml" ? "മലയാളം" : "English";
    const section = selectedScriptSection();
    $("#active-segment-preview").textContent = section?.text.trim()
      ? `${section.title}\n\n${section.text.trim()}`
      : "Select a script segment to preview it here.";
    $("#speak-section").disabled = !section?.text.trim();
    $("#regenerate-section").disabled = !section;
  }

  function renderScriptSections() {
    scriptSectionList.replaceChildren();
    studioSections.forEach((section, index) => {
      const card = document.createElement("article");
      card.className = `script-section${section.id === selectedSectionId ? " selected" : ""}`;
      card.dataset.sectionId = section.id;
      const heading = document.createElement("div");
      heading.className = "script-section-heading";
      const title = document.createElement("input");
      title.className = "section-title-input";
      title.value = section.title;
      title.maxLength = 80;
      title.setAttribute("aria-label", `Section ${index + 1} title`);
      title.dataset.sectionTitle = section.id;
      const controls = document.createElement("div");
      controls.className = "section-controls";
      [
        ["up", "Move section up"],
        ["down", "Move section down"],
        ["duplicate", "Duplicate section"],
        ["delete", "Delete section"],
      ].forEach(([action, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "section-control";
        button.dataset.action = action;
        button.dataset.sectionId = section.id;
        button.textContent = { up: "↑", down: "↓", duplicate: "⧉", delete: "×" }[action];
        button.setAttribute("aria-label", label);
        button.disabled = (action === "up" && index === 0)
          || (action === "down" && index === studioSections.length - 1)
          || (action === "delete" && studioSections.length === 1);
        controls.append(button);
      });
      heading.append(title, controls);
      const editor = document.createElement("textarea");
      editor.className = "section-editor";
      editor.dataset.sectionText = section.id;
      editor.rows = 4;
      editor.maxLength = 5000;
      editor.placeholder = `Write the ${section.title.toLowerCase()} in spoken language…`;
      editor.value = section.text;
      editor.setAttribute("aria-label", `${section.title} script text`);
      card.append(heading, editor);
      scriptSectionList.append(card);
    });
    updateScriptMetrics();
  }

  function saveStudioDraft(showToast = true) {
    const values = Object.fromEntries(new FormData(studioForm).entries());
    try {
      localStorage.setItem("ava-anchoring-draft", JSON.stringify({
        values,
        sections: studioSections,
        selectedSectionId,
      }));
      $("#studio-draft-state").textContent = "Saved in this browser";
      if (showToast) toast("Anchoring draft saved in this browser.");
    } catch {
      $("#studio-draft-state").textContent = "Draft could not be saved";
      toast("Browser storage is unavailable. Copy your script before leaving this page.");
    }
  }

  function restoreStudioDraft() {
    try {
      const draft = JSON.parse(localStorage.getItem("ava-anchoring-draft") || "null");
      if (draft && Array.isArray(draft.sections) && draft.sections.length) {
        for (const [name, value] of Object.entries(draft.values || {})) {
          const field = studioForm.elements.namedItem(name);
          if (field) field.value = value;
        }
        studioSections = draft.sections.filter((section) => (
          typeof section.id === "string"
          && typeof section.title === "string"
          && typeof section.text === "string"
        ));
        selectedSectionId = draft.selectedSectionId || studioSections[0]?.id || "";
      }
    } catch {
      localStorage.removeItem("ava-anchoring-draft");
    }
    if (!studioSections.length) studioSections = defaultScriptSections.map((section) => ({ ...section }));
    if (!studioSections.some((section) => section.id === selectedSectionId)) {
      selectedSectionId = studioSections[0]?.id || "";
    }
    nextSectionId = Math.max(
      0,
      ...studioSections.map((section) => Number(section.id.match(/^section-(\d+)$/)?.[1] || 0)),
    ) + 1;
    renderScriptSections();
  }

  function applyGeneratedScript(text, target) {
    const generated = text.trim();
    if (!generated) {
      toast("Gemini returned an empty script. Your draft was left unchanged.");
      return;
    }
    if (target.sectionId) {
      const section = studioSections.find((item) => item.id === target.sectionId);
      if (section) section.text = generated;
    } else {
      const blocks = generated.split(/\n\s*\n/).map((block) => block.trim()).filter(Boolean);
      if (blocks.length <= 1) {
        const section = studioSections[0];
        studioSections.slice(1).forEach((item) => {
          item.text = "";
        });
        if (section) section.text = generated;
        selectedSectionId = section?.id || selectedSectionId;
      } else {
        studioSections.forEach((section) => {
          section.text = "";
        });
        blocks.forEach((block, index) => {
          const section = studioSections[index] || {
            id: `section-${nextSectionId++}`,
            title: `Segment ${index + 1}`,
            text: "",
          };
          section.text = block
            .replace(/^#{1,3}\s*/, "")
            .replace(/^(opening|introduction|main segment|transition|announcement|closing)\s*[:—-]\s*/i, "");
          if (!studioSections.includes(section)) studioSections.push(section);
        });
      }
    }
    renderScriptSections();
    saveStudioDraft(false);
    toast("Generated script added to your editable draft.");
  }

  function studioPrompt(targetSection = null) {
    const data = new FormData(studioForm);
    const language = $("#language-select").value === "ml"
      ? "natural spoken Malayalam in Malayalam script"
      : "natural spoken English";
    const parts = [
      `Create ${targetSection ? `only the ${targetSection.title} section` : "a concise, stage-ready anchoring script"} in ${language}.`,
      `Event: ${data.get("title") || "Not specified"} (${data.get("event_type")}).`,
      `Audience: ${data.get("audience") || "Not specified"}.`,
      `Tone: ${data.get("tone")}; presenter style: ${data.get("style")}.`,
      `Topic: ${data.get("topic") || "Not specified"}.`,
      `Key points: ${data.get("key_points") || "None supplied"}.`,
      `Confirmed names and designations: ${data.get("names") || "None supplied"}.`,
      `Additional instructions: ${data.get("instructions") || "None"}.`,
      "Use only the supplied facts. Do not invent people, titles, dates, or claims. Return spoken script text only.",
      "For a full script, provide Opening, Introduction, Main Segment, Transition, Announcement, and Closing as separate paragraphs with a short heading for each.",
    ];
    if (targetSection) parts.push(`Current section text to replace, if any: ${targetSection.text || "empty"}`);
    return parts.join("\n");
  }

  scriptSectionList.addEventListener("input", (event) => {
    const sectionId = event.target.dataset.sectionText || event.target.dataset.sectionTitle;
    if (!sectionId) return;
    const section = studioSections.find((item) => item.id === sectionId);
    if (!section) return;
    if (event.target.dataset.sectionText) section.text = event.target.value;
    else section.title = event.target.value;
    $("#studio-draft-state").textContent = "Unsaved changes";
    updateScriptMetrics();
  });
  scriptSectionList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    const card = event.target.closest(".script-section");
    if (button) {
      const index = studioSections.findIndex((item) => item.id === button.dataset.sectionId);
      if (index < 0) return;
      const action = button.dataset.action;
      if (action === "up" && index > 0) {
        [studioSections[index - 1], studioSections[index]] = [studioSections[index], studioSections[index - 1]];
      } else if (action === "down" && index < studioSections.length - 1) {
        [studioSections[index + 1], studioSections[index]] = [studioSections[index], studioSections[index + 1]];
      } else if (action === "duplicate") {
        const copy = { ...studioSections[index], id: `section-${nextSectionId++}`, title: `${studioSections[index].title} copy` };
        studioSections.splice(index + 1, 0, copy);
        selectedSectionId = copy.id;
      } else if (action === "delete" && studioSections.length > 1) {
        if (!window.confirm(`Delete the "${studioSections[index].title}" section?`)) return;
        studioSections.splice(index, 1);
        if (selectedSectionId === button.dataset.sectionId) selectedSectionId = studioSections[Math.max(0, index - 1)].id;
      }
      $("#studio-draft-state").textContent = "Unsaved changes";
      renderScriptSections();
      return;
    }
    if (card) {
      selectedSectionId = card.dataset.sectionId;
      renderScriptSections();
      scriptSectionList.querySelector(`[data-section-id="${CSS.escape(selectedSectionId)}"] .section-editor`)?.focus();
    }
  });

  $("#studio-save-draft").addEventListener("click", () => saveStudioDraft());
  $("#add-script-section").addEventListener("click", () => {
    const section = { id: `section-${nextSectionId++}`, title: `Segment ${studioSections.length + 1}`, text: "" };
    studioSections.push(section);
    selectedSectionId = section.id;
    renderScriptSections();
  });
  $("#preview-script").addEventListener("click", () => {
    $("#script-preview-text").textContent = studioSections
      .map((section) => `${section.title}\n${section.text.trim()}`.trim())
      .filter(Boolean)
      .join("\n\n");
    $("#script-preview-dialog").showModal();
  });
  $("#reset-script").addEventListener("click", () => {
    if (!window.confirm("Reset all script segments and the brief?")) return;
    studioForm.reset();
    localStorage.removeItem("ava-anchoring-draft");
    studioSections = defaultScriptSections.map((section) => ({ ...section }));
    selectedSectionId = studioSections[0].id;
    $("#studio-draft-state").textContent = "Draft not saved";
    renderScriptSections();
  });
  const markStudioDraftDirty = () => {
    $("#studio-draft-state").textContent = "Unsaved changes";
  };
  studioForm.addEventListener("input", markStudioDraftDirty);
  studioForm.addEventListener("change", markStudioDraftDirty);
  studioForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (currentMessage || outboundMessages.length) {
      toast("Wait for the current AVA response before generating another script.");
      return;
    }
    studioGenerationTarget = {};
    document.querySelector('[data-mode="anchoring"]').click();
    sendMessage(studioPrompt());
  });
  $("#regenerate-section").addEventListener("click", () => {
    const section = selectedScriptSection();
    if (!section) return;
    if (currentMessage || outboundMessages.length) {
      toast("Wait for the current AVA response before regenerating a section.");
      return;
    }
    studioGenerationTarget = { sectionId: section.id };
    document.querySelector('[data-mode="anchoring"]').click();
    sendMessage(studioPrompt(section));
  });
  $("#speak-section").addEventListener("click", async () => {
    const section = selectedScriptSection();
    if (!section?.text.trim()) return;
    if (!eventId) {
      toast("Create and save an event before sending a segment to AVA for speech.");
      return;
    }
    try {
      const response = await fetch(`/api/live-events/${eventId}/announcement`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: section.text.trim(), polish: false }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "AVA could not speak this segment.");
      if (result.notice) toast(result.notice);
      pendingAudio.push(result.url);
      queueAudio(result.url);
      $("#robot-caption").textContent = "Speaking selected script segment";
    } catch (error) {
      toast(error.message || "AVA could not speak this segment.");
    }
  });
  $("#studio-stop").addEventListener("click", async () => {
    stopAudio();
    if (!eventId) {
      toast("Browser playback stopped. No live event is connected for server-side cancellation.");
      return;
    }
    try {
      const response = await fetch(`/api/live-events/${eventId}/stop-speaking`, { method: "POST" });
      if (!response.ok) throw new Error("Server-side speech could not be stopped.");
      toast("Current speech stop requested.");
    } catch (error) {
      toast(error.message || "Current speech stop failed.");
    }
  });
  restoreStudioDraft();

  document.querySelectorAll(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      mode = tab.dataset.mode;
      document.querySelectorAll(".mode-tab").forEach((item) => {
        const active = item === tab;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      modeHint.textContent = modes[mode];
      eventCard.hidden = mode !== "anchoring";
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
        const schedule = document.createElement("span");
        schedule.textContent = `${JSON.parse(data.event.schedule_json || "[]").length} scheduled items`;
        details.append(title, schedule);
        state.append(details);
        if (eventId) {
          fetch(`/api/live-events/${eventId}`)
            .then((response) => response.ok ? response.json() : null)
            .then((liveEvent) => {
              if (!liveEvent) return;
              title.textContent = liveEvent.name;
              schedule.textContent = `${liveEvent.items.length} items · ${liveEvent.status.replaceAll("_", " ")}`;
              renderSchedule(
                liveEvent.items,
                liveEvent.items.findIndex((item) => item.id === liveEvent.current_item_id),
              );
            })
            .catch(() => {
              schedule.textContent = "Could not refresh the live schedule.";
            });
        }
      } else {
        state.innerHTML = '<span class="state-empty">No story or event yet</span>';
      }
      if (data.event) renderSchedule(JSON.parse(data.event.schedule_json || "[]"), data.event.current_item_index || 0);
    } catch {
      $("#memory-copy").textContent = "Saved conversation context will appear here.";
    }
  }

  function renderSchedule(schedule, currentIndex) {
    const target = $("#schedule-display");
    target.replaceChildren();
    schedule.forEach((item, index) => {
      const line = document.createElement("div");
      line.className = `schedule-line${index === currentIndex ? " current" : ""}`;
      line.textContent = `${item.time || ""} ${item.title || "Schedule item"}${item.person_name || item.participant ? ` · ${item.person_name || item.participant}` : ""}${item.status ? ` · ${item.status}` : ""}`;
      target.append(line);
    });
    $("#next-item").hidden = !schedule.length;
  }

  $("#event-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const schedule = String(data.get("schedule") || "").split(/\r?\n/)
      .map((line) => line.split("|").map((part) => part.trim()))
      .filter((parts) => parts.some(Boolean))
      .map(([time, title, participant]) => ({ time: time || "", title: title || "", participant: participant || "", type: "other" }));
    try {
      const response = await fetch("/api/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          name: data.get("name"),
          venue: data.get("venue"),
          event_date: data.get("event_date"),
          chief_guest: data.get("chief_guest"),
          speakers: String(data.get("speakers") || "").split(",").map((name) => name.trim()).filter(Boolean),
          tone: data.get("tone"),
          schedule,
        }),
      });
      if (!response.ok) throw new Error("Could not save event details.");
      const saved = await response.json();
      eventId = saved.id;
      localStorage.setItem("ava-event-id", String(eventId));
      renderSchedule(schedule, 0);
      await refreshMemory();
      toast("Event details saved. You can now ask AVA for an announcement.");
    } catch (error) {
      toast(error.message || "Could not save the event.");
    }
  });

  $("#next-item").addEventListener("click", async () => {
    if (!eventId) return toast("Save an event first.");
    try {
      const response = await fetch(`/api/events/${eventId}/next`, { method: "POST" });
      if (!response.ok) throw new Error("Could not advance the event schedule.");
      const result = await response.json();
      await refreshMemory();
      if (result.current && result.advanced) {
        mode = "anchoring";
        document.querySelector('[data-mode="anchoring"]').click();
        sendMessage(`Announce the next item: ${result.current.title}${result.current.participant ? ` by ${result.current.participant}` : ""}`);
      } else {
        toast(result.current ? "You have reached the final schedule item." : "There are no items in this schedule yet.");
      }
    } catch (error) {
      toast(error.message || "Could not advance the schedule.");
    }
  });

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
      $("#event-form").reset();
      $("#schedule-display").replaceChildren();
      $("#next-item").hidden = true;
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
          $("#language-select").value = preferences.language;
          voices.value = preferences.voice;
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
    if (!sessionId) return;
    try {
      await fetch(`/api/preferences/${encodeURIComponent(sessionId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: event.target.value, style: "Professional", voice: "" }),
      });
    } catch {
      toast("Language preference could not be saved.");
    }
  });

  ensureSession().then(() => {
    connect();
    void refreshMemory();
    void refreshHealth();
    window.setInterval(() => void refreshHealth(), 30000);
    if (eventId) {
      eventCard.hidden = false;
      document.querySelector('[data-mode="anchoring"]').click();
    }
  }).catch((error) => toast(error.message));
})();
