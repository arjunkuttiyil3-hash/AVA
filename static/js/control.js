(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const sessionStorageKey = "ava-session-id";
  const eventStorageKey = "ava-event-id";
  let sessionId = localStorage.getItem(sessionStorageKey) || "";
  let eventId = Number(localStorage.getItem(eventStorageKey)) || 0;
  let eventState = null;
  let socket = null;
  let retryTimer = null;
  let retryCount = 0;
  let mediaStream = null;
  let recorder = null;
  let segmentTimer = null;
  let silenceTimer = null;
  let audioContext = null;
  let analyser = null;
  let captureEnabled = false;
  let captureQueue = Promise.resolve();
  let capturingItemId = 0;
  let captureSuppressedItemId = 0;
  let audioUnlocked = false;
  let activeAudio = null;
  let activeUtterance = null;
  let audioGeneration = 0;
  let silenceSuggestionSeconds = 8;

  function notify(message) {
    const node = document.createElement("div");
    node.className = "toast";
    node.textContent = message;
    $("#toast-region").append(node);
    window.setTimeout(() => node.remove(), 4500);
  }

  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status}).`);
    return data;
  }

  const post = (url, body = {}) => request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const patch = (url, body) => request(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  function currentItem() {
    return eventState?.items.find((item) => item.id === eventState.current_item_id) || null;
  }

  function render(event) {
    eventState = event;
    if (!event) return;
    eventId = event.id;
    localStorage.setItem(eventStorageKey, String(event.id));
    $("#empty-state").hidden = true;
    $("#control-content").hidden = false;
    $("#event-title").textContent = event.name;
    $("#event-status").textContent = event.status.replaceAll("_", " ");
    $("#version-label").textContent = `Version ${event.state_version}`;
    $("#field-name").value = event.name;
    $("#field-type").value = event.type;
    $("#field-date").value = event.date || "";
    $("#field-venue").value = event.venue || "";
    $("#field-tone").value = event.tone;
    $("#field-language").value = event.language;
    $("#field-chief-guest").value = event.chief_guest || "";
    $("#export-md").href = `/api/live-events/${event.id}/report?format=md`;
    $("#export-pdf").href = `/api/live-events/${event.id}/report?format=pdf`;
    $("#export-docx").href = `/api/live-events/${event.id}/report?format=docx`;
    const complete = event.items.filter((item) => ["done", "skipped", "removed"].includes(item.status)).length;
    $("#progress-label").textContent = `${complete} of ${event.items.length} items complete`;
    $("#progress-bar").style.width = `${event.items.length ? Math.round(complete / event.items.length * 100) : 0}%`;
    $("#schedule-count").textContent = `${event.items.length} items`;
    renderItems(event.items);
    renderLog(event.log);
    renderSummaries(event.summaries);
    const item = currentItem();
    if (!item || item.id !== captureSuppressedItemId || item.status !== "on_stage") {
      captureSuppressedItemId = 0;
    }
    $("#transcript-editor").value = item?.transcript || "";
    $("#summary-editor").value = item?.summary || "";
    $("#speech-finished").disabled = !item || item.type !== "speech" || item.status !== "on_stage";
    if (item?.id !== captureSuppressedItemId && item?.type === "speech"
      && item.status === "on_stage" && capturingItemId !== item.id) {
      if (captureEnabled) {
        capturingItemId = item.id;
        void stopCapture().then(() => beginCapture(item));
      } else {
        void beginCapture(item);
      }
    } else if ((!item || item.type !== "speech" || item.status !== "on_stage") && captureEnabled) {
      stopCapture();
    }
  }

  function renderItems(items) {
    const list = $("#schedule-list");
    list.replaceChildren();
    items.forEach((item, index) => {
      const card = document.createElement("article");
      card.className = `schedule-item ${item.status}${item.id === eventState.current_item_id ? " current" : ""}`;
      const head = document.createElement("div");
      head.className = "schedule-item-heading";
      const title = document.createElement("strong");
      title.textContent = `${index + 1}. ${item.title}`;
      const status = document.createElement("span");
      status.className = "item-status";
      status.textContent = item.status.replaceAll("_", " ");
      head.append(title, status);
      card.append(head);
      const details = document.createElement("div");
      details.className = "schedule-item-meta";
      details.textContent = [
        item.type,
        item.person_name,
        item.designation,
        item.planned_duration ? `${item.planned_duration} min` : "",
      ].filter(Boolean).join(" · ");
      card.append(details);
      const actions = document.createElement("div");
      actions.className = "item-actions";
      const button = (label, handler, kind = "secondary-button") => {
        const control = document.createElement("button");
        control.className = kind;
        control.type = "button";
        control.textContent = label;
        control.addEventListener("click", handler);
        actions.append(control);
      };
      if (["pending", "absent"].includes(item.status)) {
        button("Arrived", () => updateItem(item, { status: "arrived" }));
      }
      if (!["absent", "removed", "done", "skipped"].includes(item.status)) {
        button("Absent / not coming", () => {
          if (window.confirm(`Mark ${item.person_name || item.title} absent? Their name will be removed from upcoming announcements.`)) {
            void updateItem(item, { status: "absent" });
          }
        }, "danger-button");
      }
      if (["absent", "removed", "skipped"].includes(item.status)) {
        button("Restore", () => updateItem(item, { status: "pending" }));
      }
      if (!["done", "skipped", "removed"].includes(item.status)) {
        button("Skip", () => updateItem(item, { status: "skipped" }));
      }
      button("Edit", () => editItem(item));
      button("Call now", () => void callNow(item.id), "primary-button");
      button("↑", () => moveItem(index, -1));
      button("↓", () => moveItem(index, 1));
      button("Delete", () => {
        if (window.confirm(`Remove “${item.title}” from the live schedule?`)) {
          void removeItem(item);
        }
      }, "danger-button");
      card.append(actions);
      list.append(card);
    });
  }

  function renderLog(entries = []) {
    const list = $("#event-log");
    list.replaceChildren();
    entries.slice(0, 30).forEach((entry) => {
      const line = document.createElement("li");
      line.className = "log-entry";
      const title = document.createElement("strong");
      title.textContent = entry.action.replaceAll("_", " ");
      const time = document.createElement("time");
      time.textContent = new Date(`${entry.timestamp.replace(" ", "T")}Z`).toLocaleString();
      const detail = document.createElement("p");
      detail.textContent = `${entry.operator} · ${entry.new_value}`;
      line.append(title, time, detail);
      list.append(line);
    });
  }

  function renderSummaries(items = []) {
    const list = $("#summary-list");
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "muted-label";
      empty.textContent = "Summaries appear after a speech is finished.";
      list.append(empty);
      return;
    }
    items.forEach((item) => {
      const entry = document.createElement("article");
      entry.className = "summary-entry";
      const title = document.createElement("strong");
      title.textContent = item.person_name || item.title;
      const text = document.createElement("p");
      text.textContent = item.summary;
      entry.append(title, text);
      list.append(entry);
    });
  }

  async function loadEvent() {
    if (!sessionId) {
      const session = await request("/api/sessions", { method: "POST" });
      sessionId = session.session_id;
      localStorage.setItem(sessionStorageKey, sessionId);
    }
    let event;
    try {
      event = await request(`/api/live-events/session/${encodeURIComponent(sessionId)}`);
    } catch (error) {
      if (error.message.includes("No live event")) {
        $("#empty-state").hidden = false;
        $("#control-content").hidden = true;
        return;
      }
      throw error;
    }
    render(event);
    const settings = await request(`/api/event/settings/${encodeURIComponent(sessionId)}`);
    $("#auto-speak-summary").checked = settings.auto_speak_summary;
    const appSettings = await request("/api/settings");
    silenceSuggestionSeconds = Math.max(1, Number(appSettings.speaker_silence_suggestion_seconds) || 8);
    connectSocket();
  }

  function connectSocket() {
    if (!sessionId || (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState))) return;
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${location.host}/ws/${encodeURIComponent(sessionId)}`);
    $("#socket-state").textContent = "Connecting…";
    $("#socket-state").className = "socket-state";
    socket.addEventListener("open", () => {
      retryCount = 0;
      $("#socket-state").textContent = "Live sync connected";
      $("#socket-state").classList.add("connected");
      socket.send(JSON.stringify({ type: "subscribe_event", event_id: eventId }));
      socket.send(JSON.stringify({ type: "ping", time: Date.now() }));
    });
    socket.addEventListener("message", (message) => {
      let payload;
      try {
        payload = JSON.parse(message.data);
      } catch {
        return;
      }
      if (payload.type === "event_state_changed" && payload.event) {
        render(payload.event);
        if (payload.action === "speech_stop") {
          stopBrowserAudio();
          notify("AVA stopped speaking because the current item changed.");
        }
      } else if (payload.type === "speech_stop") {
        stopBrowserAudio();
        notify("Emergency stop received.");
      } else if (payload.type === "pong" && socket?.readyState === WebSocket.OPEN) {
        window.setTimeout(() => {
          if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping", time: Date.now() }));
        }, 15000);
      }
    });
    socket.addEventListener("close", () => {
      $("#socket-state").textContent = "Reconnecting…";
      $("#socket-state").className = "socket-state error";
      const delay = Math.min(500 * (2 ** retryCount), 30000);
      retryCount = Math.min(retryCount + 1, 6);
      retryTimer = window.setTimeout(connectSocket, delay);
    });
    socket.addEventListener("error", () => {
      $("#socket-state").textContent = "Sync error";
      $("#socket-state").className = "socket-state error";
    });
  }

  async function refresh() {
    if (!eventId) return;
    render(await request(`/api/live-events/${eventId}`));
  }

  async function afterMutation(operation) {
    const result = await operation();
    try {
      await refresh();
    } catch (error) {
      const updatedEvent = result?.items ? result : result?.event;
      if (updatedEvent?.items) render(updatedEvent);
      throw error;
    }
    return result;
  }

  async function runAction(url, action = {}) {
    try {
      await afterMutation(() => post(url, action));
    } catch (error) {
      notify(error.message);
    }
  }

  async function updateItem(item, changes) {
    try {
      await afterMutation(() => patch(`/api/event/items/${item.id}`, { event_id: eventId, ...changes }));
    } catch (error) {
      notify(error.message);
    }
  }

  async function removeItem(item) {
    try {
      await afterMutation(() => request(`/api/event/items/${item.id}?event_id=${eventId}`, { method: "DELETE" }));
    } catch (error) {
      notify(error.message);
    }
  }

  async function moveItem(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= eventState.items.length) return;
    const order = eventState.items.map((item) => item.id);
    [order[index], order[target]] = [order[target], order[index]];
    try {
      await afterMutation(() => post("/api/event/reorder", { event_id: eventId, item_ids: order }));
    } catch (error) {
      notify(error.message);
    }
  }

  async function editItem(item) {
    const title = window.prompt("Schedule item title", item.title);
    if (title === null) return;
    const personName = window.prompt("Person / speaker name", item.person_name || "");
    if (personName === null) return;
    const designation = window.prompt("Designation", item.designation || "");
    if (designation === null) return;
    await updateItem(item, { title, person_name: personName, designation });
  }

  async function unlockAudio() {
    if (audioUnlocked) return;
    const context = new (window.AudioContext || window.webkitAudioContext)();
    await context.resume();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    gain.gain.value = 0;
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.01);
    audioUnlocked = true;
    $("#audio-unlock").textContent = "Audio enabled";
  }

  async function playUrl(url) {
    if (!audioUnlocked) {
      notify("Enable audio first, then press Speak it again.");
      return false;
    }
    stopBrowserAudio();
    activeAudio = new Audio(url);
    try {
      await activeAudio.play();
      return true;
    } catch {
      notify("Audio could not play in this browser.");
      return false;
    }
  }

  function stopBrowserAudio() {
    audioGeneration += 1;
    window.speechSynthesis?.cancel();
    activeUtterance = null;
    if (activeAudio) {
      activeAudio.pause();
      activeAudio.currentTime = 0;
      activeAudio = null;
    }
  }

  function speakFallback(text, language) {
    if (!("speechSynthesis" in window) || !text) return false;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = language === "ml" ? "ml-IN" : "en-IN";
    activeUtterance = utterance;
    utterance.addEventListener("end", () => {
      if (activeUtterance === utterance) activeUtterance = null;
    }, { once: true });
    utterance.addEventListener("error", () => {
      if (activeUtterance === utterance) activeUtterance = null;
      notify("Browser speech could not play this announcement.");
    }, { once: true });
    window.speechSynthesis.speak(utterance);
    return true;
  }

  async function callNow(itemId = null) {
    if (!eventId) {
      notify("Set up an event before calling a schedule item.");
      return;
    }
    stopBrowserAudio();
    const generation = audioGeneration;
    try {
      try {
        await unlockAudio();
      } catch {
        notify("Audio unlock failed; trying browser speech after the announcement is prepared.");
      }
      const result = await afterMutation(() => post(
        `/api/live-events/${eventId}/call-now`,
        {
          ...(itemId === null ? {} : { item_id: itemId }),
          include_summary: $("#include-transition").checked,
        },
      ));
      if (generation !== audioGeneration) return;
      if (result.notice) notify(result.notice);
      const played = result.audio_url ? await playUrl(result.audio_url) : false;
      if (!played && !speakFallback(result.text, result.event.language)) {
        notify(result.text || "The announcement is ready, but no speech output is available.");
      }
    } catch (error) {
      notify(error.message);
    }
  }

  function startRecorderSegment(item) {
    if (!captureEnabled || !mediaStream || capturingItemId !== item.id) return;
    recorder = new MediaRecorder(mediaStream);
    const segment = recorder;
    segment.addEventListener("dataavailable", (event) => {
      if (!event.data.size) return;
      captureQueue = captureQueue.then(() => uploadSegment(event.data, item));
    });
    segment.addEventListener("stop", () => {
      if (captureEnabled && capturingItemId === item.id) startRecorderSegment(item);
    }, { once: true });
    segment.start();
    segmentTimer = window.setTimeout(() => {
      if (recorder === segment && segment.state === "recording") segment.stop();
    }, 25000);
  }

  async function uploadSegment(blob, item) {
    const body = new FormData();
    body.append("audio", blob, "speech-segment.webm");
    body.append("language", item.transcript_language || "auto");
    try {
      const result = await afterMutation(() => request(
        `/api/live-events/${eventId}/items/${item.id}/capture`,
        { method: "POST", body },
      ));
      if (result.text) {
        $("#transcript-editor").value = `${$("#transcript-editor").value} ${result.text}`.trim();
        if (/thank you|നന്ദി/i.test(result.text)) $("#silence-suggestion").hidden = false;
      }
      $("#capture-state").textContent = result.language === "ml"
        ? "Capturing · Malayalam transcription may need operator correction"
        : "Capturing · 25-second segments are transcribed in the background";
    } catch (error) {
      $("#capture-state").textContent = "Capture unavailable — paste notes or transcript below";
      notify(error.message);
    }
  }

  async function beginCapture(item) {
    capturingItemId = item.id;
    try {
      if (!mediaStream) {
        mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = audioContext.createMediaStreamSource(mediaStream);
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser);
        captureEnabled = true;
        monitorSilence();
      }
      captureEnabled = true;
      $("#capture-state").textContent = "Microphone active · recording 25-second segments";
      startRecorderSegment(item);
    } catch {
      captureEnabled = false;
      capturingItemId = 0;
      $("#capture-state").textContent = "Microphone unavailable — paste notes or transcript below";
      notify("Speaker capture needs microphone permission. You can still paste notes and summarize them.");
    }
  }

  function monitorSilence() {
    const samples = new Uint8Array(analyser.fftSize);
    let quietSeconds = 0;
    const check = () => {
      if (!captureEnabled || !analyser) return;
      analyser.getByteTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, sample) => sum + ((sample - 128) / 128) ** 2, 0) / samples.length);
      quietSeconds = rms < 0.014 ? quietSeconds + 1 : 0;
      if (quietSeconds >= silenceSuggestionSeconds) {
        $("#silence-suggestion").hidden = false;
        $("#silence-suggestion").textContent = `${silenceSuggestionSeconds} seconds of low audio detected. Confirm with “Speech finished” if the speaker is done.`;
      }
      silenceTimer = window.setTimeout(check, 1000);
    };
    check();
  }

  function stopCapture() {
    captureEnabled = false;
    capturingItemId = 0;
    window.clearTimeout(segmentTimer);
    window.clearTimeout(silenceTimer);
    $("#capture-state").textContent = "Capture stopped";
    const activeRecorder = recorder;
    const stopped = activeRecorder?.state === "recording"
      ? new Promise((resolve) => {
        activeRecorder.addEventListener("stop", resolve, { once: true });
        activeRecorder.stop();
      })
      : Promise.resolve();
    return stopped.then(async () => {
      await captureQueue;
      mediaStream?.getTracks().forEach((track) => track.stop());
      mediaStream = null;
      await audioContext?.close();
      audioContext = null;
      analyser = null;
    });
  }

  async function persistTranscript() {
    const item = currentItem();
    if (!item) throw new Error("Choose an active schedule item first.");
    await afterMutation(() => request(`/api/live-events/${eventId}/items/${item.id}/transcript`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("#transcript-editor").value }),
    }));
  }

  $("#save-event-fields").addEventListener("click", async () => {
    try {
      await afterMutation(() => patch(`/api/live-events/${eventId}`, {
        name: $("#field-name").value,
        type: $("#field-type").value,
        date: $("#field-date").value,
        venue: $("#field-venue").value,
        tone: $("#field-tone").value,
        language: $("#field-language").value,
        chief_guest: $("#field-chief-guest").value,
      }));
    } catch (error) {
      notify(error.message);
    }
  });
  $("#start-event").addEventListener("click", () => runAction(`/api/live-events/${eventId}/start`));
  $("#pause-event").addEventListener("click", () => runAction(`/api/live-events/${eventId}/pause`));
  $("#end-event").addEventListener("click", () => {
    if (window.confirm("End this event?")) void runAction(`/api/live-events/${eventId}/end`);
  });
  $("#previous-item").addEventListener("click", () => runAction(`/api/live-events/${eventId}/previous`));
  $("#next-item").addEventListener("click", () => void callNow());
  $("#undo-change").addEventListener("click", () => {
    if (window.confirm("Undo the most recent live event change?")) {
      void runAction(`/api/live-events/${eventId}/undo`);
    }
  });
  $("#stop-speaking").addEventListener("click", async () => {
    stopBrowserAudio();
    try {
      await post(`/api/live-events/${eventId}/stop-speaking`);
      notify("Stop command sent to AVA and the robot controller.");
    } catch (error) {
      notify(error.message);
    }
  });
  $("#add-item-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const rawPosition = String(data.get("position") || "").trim();
    const item = {
      event_id: eventId,
      title: String(data.get("title")),
      type: String(data.get("type")),
      person_name: String(data.get("person_name") || ""),
      designation: String(data.get("designation") || ""),
      planned_duration: Number(data.get("planned_duration") || 0),
      transcript_language: String(data.get("transcript_language") || "auto"),
    };
    if (rawPosition) item.position = Number(rawPosition) - 1;
    try {
      await afterMutation(() => post("/api/event/items", item));
      form.reset();
    } catch (error) {
      notify(error.message);
    }
  });
  $("#capture-toggle").addEventListener("click", () => {
    const item = currentItem();
    if (!item || item.type !== "speech" || item.status !== "on_stage") {
      notify("Start a speech item before capturing its transcript.");
      return;
    }
    captureSuppressedItemId = 0;
    if (captureEnabled) stopCapture();
    else void beginCapture(item);
  });
  $("#save-transcript").addEventListener("click", async () => {
    try {
      await captureQueue;
      await persistTranscript();
      notify("Transcript saved.");
    } catch (error) {
      notify(error.message);
    }
  });
  $("#speech-finished").addEventListener("click", async () => {
    const item = currentItem();
    if (!item || item.type !== "speech") return;
    captureSuppressedItemId = item.id;
    await stopCapture();
    try {
      await persistTranscript();
      const result = await post(`/api/live-events/${eventId}/items/${item.id}/finish`, {
        language: eventState.language,
      });
      await refresh();
      $("#summary-editor").value = result.summary;
      notify("Summary ready for operator review.");
      if ($("#auto-speak-summary").checked) {
        const audio = await post(`/api/live-events/${eventId}/items/${item.id}/summary/speak`);
        await playUrl(audio.url);
      }
    } catch (error) {
      notify(error.message);
    }
  });
  $("#save-summary").addEventListener("click", async () => {
    const item = currentItem();
    if (!item) return notify("Choose a speaker item.");
    try {
      await afterMutation(() => patch(`/api/event/items/${item.id}`, {
        event_id: eventId,
        summary: $("#summary-editor").value,
      }));
      notify("Summary edits saved.");
    } catch (error) {
      notify(error.message);
    }
  });
  $("#regenerate-summary").addEventListener("click", async () => {
    const item = currentItem();
    if (!item) return notify("Choose a speaker item.");
    try {
      const result = await post(`/api/live-events/${eventId}/items/${item.id}/summary`, {
        language: eventState.language,
      });
      await refresh();
      $("#summary-editor").value = result.summary;
    } catch (error) {
      notify(error.message);
    }
  });
  $("#discard-summary").addEventListener("click", async () => {
    const item = currentItem();
    if (!item || !window.confirm("Discard this speaker summary?")) return;
    try {
      await afterMutation(() => patch(`/api/event/items/${item.id}`, { event_id: eventId, summary: "" }));
      $("#summary-editor").value = "";
    } catch (error) {
      notify(error.message);
    }
  });
  $("#speak-summary").addEventListener("click", async () => {
    const item = currentItem();
    if (!item) return notify("Choose a speaker item.");
    try {
      await unlockAudio();
      const generation = audioGeneration;
      const audio = await post(`/api/live-events/${eventId}/items/${item.id}/summary/speak`);
      if (generation !== audioGeneration) return;
      await playUrl(audio.url);
    } catch (error) {
      notify(error.message);
    }
  });
  $("#audio-unlock").addEventListener("click", () => {
    void unlockAudio().catch(() => notify("Could not enable browser audio."));
  });
  $("#deliver-announcement").addEventListener("click", async () => {
    try {
      await unlockAudio();
      const generation = audioGeneration;
      const result = await post(`/api/live-events/${eventId}/announcement`, {
        text: $("#announcement-text").value,
        polish: $("#polish-announcement").checked,
      });
      if (generation !== audioGeneration) return;
      if (result.notice) notify(result.notice);
      await playUrl(result.url);
      notify("Operator announcement delivered.");
    } catch (error) {
      notify(error.message);
    }
  });
  $("#auto-speak-summary").addEventListener("change", async (event) => {
    try {
      await request(`/api/event/settings/${encodeURIComponent(sessionId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_speak_summary: event.currentTarget.checked }),
      });
    } catch (error) {
      notify(error.message);
      event.currentTarget.checked = false;
    }
  });

  window.addEventListener("beforeunload", () => {
    stopCapture();
    window.clearTimeout(retryTimer);
    socket?.close();
  });
  void loadEvent().catch((error) => {
    notify(error.message || "Could not load the event control console.");
  });
})();
