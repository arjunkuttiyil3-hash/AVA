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
  let playbackAudioContext = null;
  let captureEnabled = false;
  let captureQueue = Promise.resolve();
  let capturingItemId = 0;
  let captureSuppressedItemId = 0;
  let audioUnlocked = false;
  let activeAudioSource = null;
  let audioPlaybackActive = false;
  let activeUtterance = null;
  let audioGeneration = 0;
  let silenceSuggestionSeconds = 8;
  let activeScriptKey = "";
  let scheduleTimer = null;
  let scheduleSignature = "";
  let finishTimer = null;
  let finishSignature = "";
  let pendingStartTimer = null;
  let startPending = false;
  let autoFinishingItemId = 0;
  let savedScriptSegments = [];
  let openingPlayedEventId = 0;
  let announcementInProgress = false;
  let editingItemId = 0;
  const eventSummaryAudioSeen = new Set();

  const itemStatusLabels = {
    pending: "scheduled",
    arrived: "present",
    absent: "absent",
    on_stage: "in progress",
    done: "completed",
    skipped: "skipped",
    removed: "deleted",
  };

  function displayItemStatus(status) {
    return itemStatusLabels[status] || status.replaceAll("_", " ");
  }

  async function playEventSummary(payload) {
    const summaryEventId = Number(payload.event_id);
    if (!summaryEventId || eventSummaryAudioSeen.has(summaryEventId)) return;
    eventSummaryAudioSeen.add(summaryEventId);
    try {
      const played = await playUrl(payload.url);
      if (!played && !speakFallback(payload.summary, payload.language)) {
        notify("Event summary is ready, but speech playback is unavailable.");
        return;
      }
      notify("Event summary is being spoken.");
    } catch (error) {
      notify(`Could not play the event summary: ${error.message}`);
    }
  }

  function scriptStorageKey(id = eventId) {
    return `ava-script-lines-${id}`;
  }

  function readScriptLines(id = eventId) {
    try {
      const saved = JSON.parse(localStorage.getItem(scriptStorageKey(id)) || "{}");
      return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
    } catch {
      notify("Saved script lines could not be read; new edits will replace the damaged browser copy.");
      return {};
    }
  }

  function saveScriptLine(key, text, manual) {
    const lines = readScriptLines();
    lines[key] = { text, manual };
    try {
      localStorage.setItem(scriptStorageKey(), JSON.stringify(lines));
    } catch (error) {
      notify(`Could not save this script line in the browser: ${error.message}`);
    }
  }

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
    if (!response.ok) {
      const error = new Error(data.detail || `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
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

  function isSpeechOrTalk(item) {
    return ["speech", "talk", "announcement", "keynote", "lecture", "presentation"]
      .includes(String(item?.type || "").trim().toLowerCase());
  }

  function isAutomaticCaptureType(item) {
    return ["speech", "talk", "announcement"]
      .includes(String(item?.type || "").trim().toLowerCase());
  }

  function render(event) {
    eventState = event;
    if (!event) return;
    eventId = event.id;
    localStorage.setItem(eventStorageKey, String(event.id));
    $("#empty-state").hidden = true;
    $("#control-content").hidden = false;
    $("#event-title").textContent = event.name;
    $("#event-status").textContent = ({
      not_started: "Not started",
      running: "Live",
      paused: "Paused",
      ended: "Stopped",
    })[event.status] || event.status.replaceAll("_", " ");
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
    const current = currentItem();
    const upcoming = event.items.find((item) =>
      item.order_index > (current?.order_index ?? -1)
      && !["absent", "removed", "skipped", "done"].includes(item.status));
    $("#current-item-label").textContent = current ? current.title : "No current item";
    $("#next-item-label").textContent = upcoming ? upcoming.title : "No upcoming item";
    $("#start-event").textContent = startPending
      ? "Start anchoring now"
      : event.status === "paused" ? "Resume anchoring"
        : event.status === "running" ? "Resume anchoring audio" : "Start anchoring";
    $("#start-event-help").textContent = event.status === "running"
      ? "Anchoring is live. Press this button to re-enable audio and resume scheduled playback."
      : "Enable audio now; AVA will begin speaking at the scheduled event time on this laptop.";
    $("#start-event").disabled = event.status === "ended";
    $("#pause-event").disabled = event.status !== "running";
    $("#next-item").disabled = event.status === "ended";
    $("#previous-item").disabled = event.status === "ended";
    $("#end-event").disabled = event.status === "ended";
    $("#save-event-fields").disabled = event.status === "ended";
    $("#undo-change").disabled = event.status === "ended";
    $("#add-item-form").querySelectorAll("input, select, button").forEach((control) => {
      control.disabled = event.status === "ended";
    });
    renderItems(event.items);
    const scriptKey = event.status === "ended" ? "closing" : (current ? String(current.id) : "");
    activeScriptKey = scriptKey;
    const savedLine = scriptKey ? readScriptLines(event.id)[scriptKey] : null;
    $("#script-item-title").textContent = event.status === "ended"
      ? "Event closing"
      : current ? `Script · ${current.title}` : "Current item script";
    $("#current-script-line").value = savedLine?.text || "";
    renderLog(event.log);
    renderSummaries(event.summaries);
    const item = currentItem();
    if (!item || item.id !== captureSuppressedItemId || item.status !== "on_stage") {
      captureSuppressedItemId = 0;
    }
    $("#transcript-editor").value = item?.transcript || "";
    $("#summary-editor").value = item?.summary || "";
    $(".speech-card").hidden = !isSpeechOrTalk(item);
    $("#speech-finished").disabled = !isSpeechOrTalk(item) || item.status !== "on_stage";
    if (!announcementInProgress && item?.id !== captureSuppressedItemId && isAutomaticCaptureType(item)
      && item.status === "on_stage" && capturingItemId !== item.id) {
      if (captureEnabled) {
        capturingItemId = item.id;
        void stopCapture().then(() => beginCapture(item));
      } else {
        void beginCapture(item);
      }
    } else if ((!isAutomaticCaptureType(item) || item.status !== "on_stage") && captureEnabled) {
      stopCapture();
    }
    syncScheduledPlayback();
    syncScheduledFinish();
  }

  function renderItems(items) {
    const list = $("#schedule-list");
    list.replaceChildren();
    const current = currentItem();
    const upcoming = items.find((item) =>
      item.order_index > (current?.order_index ?? -1)
      && !["absent", "removed", "skipped", "done"].includes(item.status));
    items.forEach((item, index) => {
      const card = document.createElement("article");
      card.className = `schedule-item ${item.status}${item.id === eventState.current_item_id ? " current" : ""}${item.id === upcoming?.id ? " upcoming" : ""}`;
      const head = document.createElement("div");
      head.className = "schedule-item-heading";
      const title = document.createElement("strong");
      title.textContent = `${index + 1}. ${item.title}`;
      const status = document.createElement("span");
      status.className = "item-status";
      status.textContent = displayItemStatus(item.status);
      head.append(title, status);
      card.append(head);
      const details = document.createElement("div");
      details.className = "schedule-item-meta";
      details.textContent = [
        item.type,
        item.person_name,
        item.designation,
        item.scheduled_time,
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
        control.disabled = eventState.status === "ended";
        control.addEventListener("click", handler);
        actions.append(control);
      };
      if (item.status === "pending") {
        button("Present", () => updateItem(item, { status: "arrived" }));
      }
      if (!["absent", "removed", "done", "skipped"].includes(item.status)) {
        button("Announce absence / skip", () => {
          if (window.confirm(`Mark ${item.person_name || item.title} absent? Their name will be removed from upcoming announcements.`)) {
            void updateItem(item, { status: "absent" });
          }
        }, "danger-button");
      }
      if (["absent", "removed", "skipped"].includes(item.status)) {
        button("Restore / present", () => void restoreItem(item));
      }
      if (!["done", "skipped", "removed"].includes(item.status)) {
        button("Skip", () => void skipItem(item));
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
    let event = null;
    if (sessionId) {
      try {
        event = await request(`/api/live-events/session/${encodeURIComponent(sessionId)}`);
      } catch (error) {
        if (error.status !== 404) throw error;
      }
    }
    if (!event && eventId) {
      try {
        event = await request(`/api/live-events/${eventId}`);
        sessionId = event.session_id || sessionId;
        if (sessionId) localStorage.setItem(sessionStorageKey, sessionId);
      } catch (error) {
        if (error.status !== 404) throw error;
        eventId = 0;
        localStorage.removeItem(eventStorageKey);
      }
    }
    if (!sessionId) {
      const session = await request("/api/sessions", { method: "POST" });
      sessionId = session.session_id;
      localStorage.setItem(sessionStorageKey, sessionId);
    }
    if (!event) {
      try {
        event = await request(`/api/live-events/session/${encodeURIComponent(sessionId)}`);
      } catch (error) {
        if (error.status === 404) {
          $("#empty-state").hidden = false;
          $("#control-content").hidden = true;
          $("#start-event").disabled = true;
          return;
        }
        throw error;
      }
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
      socket.send(JSON.stringify({ type: "subscribe_event", event_id: eventId, client: "control" }));
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
      } else if (payload.type === "event_summary_audio") {
        void playEventSummary(payload);
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
    const announceAbsence = changes.status === "absent";
    if (announceAbsence) {
      stopBrowserAudio();
      announcementInProgress = true;
    }
    try {
      await afterMutation(() => patch(`/api/event/items/${item.id}`, { event_id: eventId, ...changes }));
      if (announceAbsence) await playAbsenceAnnouncement(item);
    } catch (error) {
      notify(error.message);
    } finally {
      if (announceAbsence) {
        announcementInProgress = false;
        syncScheduledPlayback();
      }
    }
  }

  async function playAbsenceAnnouncement(item) {
    try {
      const notice = await post(
        `/api/live-events/${eventId}/items/${item.id}/absence-announcement`,
      );
      activeScriptKey = `absence-${item.id}`;
      saveScriptLine(activeScriptKey, notice.text, false);
      $("#script-item-title").textContent = `Schedule change · ${item.title}`;
      $("#current-script-line").value = notice.text;
      const result = await playUrlAndWait(notice.url);
      if (!result.completed && !result.interrupted) {
        await speakTextAndWait(notice.text, eventState.language);
      }
    } catch (error) {
      notify(`The item was marked absent, but its cancellation notice could not be played: ${error.message}`);
    }
  }

  async function skipItem(item) {
    const moveToEnd = window.confirm(
      `Skip “${item.title}”? Choose OK to move it to the end of the running order; choose Cancel to keep its position.`,
    );
    try {
      await afterMutation(() => post(
        `/api/live-events/${eventId}/items/${item.id}/skip`,
        { move_to_end: moveToEnd },
      ));
    } catch (error) {
      notify(error.message);
    }
  }

  async function restoreItem(item) {
    const current = currentItem();
    let position;
    if (current && item.order_index < current.order_index) {
      const placeAtCurrent = window.confirm(
        `“${item.title}” was scheduled before the current item. Choose OK to insert it at the current position; choose Cancel to restore it at the end.`,
      );
      position = placeAtCurrent
        ? current.order_index
        : eventState.items.length - 1;
    }
    try {
      await afterMutation(() => post(
        `/api/live-events/${eventId}/items/${item.id}/restore`,
        position === undefined ? {} : { position },
      ));
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
    editingItemId = item.id;
    const form = $("#edit-item-form");
    form.elements.namedItem("title").value = item.title;
    form.elements.namedItem("type").value = item.type;
    form.elements.namedItem("scheduled_time").value = item.scheduled_time || "";
    form.elements.namedItem("person_name").value = item.person_name || "";
    form.elements.namedItem("designation").value = item.designation || "";
    form.elements.namedItem("planned_duration").value = item.planned_duration || 0;
    form.elements.namedItem("participant_details").value = item.participant_details || "";
    $("#edit-item-dialog").showModal();
  }

  async function unlockAudio() {
    if (!playbackAudioContext) {
      const AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextConstructor) throw new Error("Web Audio is not supported in this browser.");
      playbackAudioContext = new AudioContextConstructor();
    }
    if (playbackAudioContext.state !== "running") {
      await playbackAudioContext.resume();
    }
    const oscillator = playbackAudioContext.createOscillator();
    const gain = playbackAudioContext.createGain();
    gain.gain.value = 0;
    oscillator.connect(gain);
    gain.connect(playbackAudioContext.destination);
    oscillator.start();
    oscillator.stop(playbackAudioContext.currentTime + 0.01);
    audioUnlocked = true;
    $("#audio-unlock").textContent = "Audio enabled";
  }

  async function playUrl(url) {
    const result = await playUrlAndWait(url);
    return result.completed;
  }

  function stopBrowserAudio() {
    audioGeneration += 1;
    window.speechSynthesis?.cancel();
    activeUtterance = null;
    if (activeAudioSource) {
      try {
        activeAudioSource.stop();
      } catch {
        activeAudioSource = null;
      }
    }
    audioPlaybackActive = false;
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

  function speakTextAndWait(text, language, generation = audioGeneration) {
    if (!("speechSynthesis" in window) || !text || generation !== audioGeneration) {
      return Promise.resolve(false);
    }
    return new Promise((resolve) => {
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = language === "ml" ? "ml-IN" : "en-IN";
      activeUtterance = utterance;
      utterance.addEventListener("end", () => {
        if (activeUtterance === utterance) activeUtterance = null;
        resolve(generation === audioGeneration);
      }, { once: true });
      utterance.addEventListener("error", () => {
        if (activeUtterance === utterance) activeUtterance = null;
        notify("Browser speech could not play this announcement.");
        resolve(false);
      }, { once: true });
      window.speechSynthesis.speak(utterance);
    });
  }

  async function loadSavedScript() {
    const result = await request(`/api/anchoring/scripts/${eventId}`);
    savedScriptSegments = result.segments;
    return savedScriptSegments;
  }

  async function loadSavedScriptForPlayback() {
    try {
      await loadSavedScript();
    } catch (error) {
      savedScriptSegments = [];
      notify(`Saved script could not be loaded; using live announcements instead: ${error.message}`);
    }
  }

  function scriptSegment(id) {
    return savedScriptSegments.find((segment) => segment.id === id && segment.text.trim());
  }

  async function speakScriptSegment(segment, generation) {
    if (!segment) return false;
    if (generation !== audioGeneration) return Promise.resolve(false);
    try {
      const audio = await request(
        `/api/live-events/${eventId}/scripts/${encodeURIComponent(segment.id)}/audio`,
      );
      if (generation !== audioGeneration) return false;
      const playback = await playUrlAndWait(audio.url);
      if (playback.completed) return true;
      if (playback.interrupted || generation !== audioGeneration) return false;
    } catch (error) {
      if (generation !== audioGeneration) return false;
      if (error.status === 409) {
        notify(error.message);
        return false;
      }
      notify(`Could not prepare saved script audio: ${error.message}. Trying browser speech.`);
    }
    return speakTextAndWait(segment.text, eventState?.language, generation);
  }

  async function speakScriptSegments(segments, generation = audioGeneration) {
    for (const segment of segments) {
      if (generation !== audioGeneration) return false;
      if (!await speakScriptSegment(segment, generation)) return false;
    }
    return segments.length > 0;
  }

  async function playUrlAndWait(url) {
    if (!playbackAudioContext || !audioUnlocked) {
      notify("Press the large Start button or Enable audio before scheduled playback.");
      return { completed: false, interrupted: false };
    }
    if (activeAudioSource || activeUtterance) stopBrowserAudio();
    const generation = audioGeneration;
    let source = null;
    let completed = false;
    try {
      if (playbackAudioContext.state !== "running") {
        await playbackAudioContext.resume();
      }
      const response = await fetch(url);
      if (!response.ok) throw new Error(`Audio request failed (${response.status}).`);
      const buffer = await playbackAudioContext.decodeAudioData(await response.arrayBuffer());
      if (generation !== audioGeneration) {
        return { completed: false, interrupted: true };
      }
      source = playbackAudioContext.createBufferSource();
      source.buffer = buffer;
      source.connect(playbackAudioContext.destination);
      activeAudioSource = source;
      audioPlaybackActive = true;
      await new Promise((resolve, reject) => {
        source.addEventListener("ended", () => {
          completed = true;
          resolve();
        }, { once: true });
        source.addEventListener("error", () => reject(new Error("Audio playback failed.")), { once: true });
        source.start();
      });
      const interrupted = generation !== audioGeneration;
      return { completed: completed && !interrupted, interrupted };
    } catch (error) {
      notify(error.message);
      return { completed: false, interrupted: generation !== audioGeneration };
    } finally {
      if (activeAudioSource === source) activeAudioSource = null;
      audioPlaybackActive = false;
    }
  }

  function playSummaryAfterSpeech(item, summary) {
    if (!summary?.trim()) return Promise.resolve();
    return (async () => {
      const generation = audioGeneration;
      try {
        const audio = await post(`/api/live-events/${eventId}/items/${item.id}/summary/speak`);
        if (generation !== audioGeneration) return;
        const playback = await playUrlAndWait(audio.url);
          if (!playback.completed && !playback.interrupted) {
            await speakTextAndWait(summary, eventState?.language, generation);
          }
      } catch (error) {
        notify(`The speech summary was saved but could not be played: ${error.message}`);
      }
    })();
  }

  function localTimestamp(timeText) {
    const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(timeText || "");
    if (!match) return null;
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const eventDate = eventState?.date && /^\d{4}-\d{2}-\d{2}$/.test(eventState.date)
      ? new Date(`${eventState.date}T00:00:00`)
      : null;
    const target = eventDate && eventDate >= today ? eventDate : today;
    target.setHours(Number(match[1]), Number(match[2]), 0, 0);
    return target.getTime();
  }

  function scheduledTimestamp(item) {
    return localTimestamp(item?.scheduled_time);
  }

  function nextEligibleItem(current = currentItem()) {
    return eventState?.items.find((item) =>
      (!current || item.order_index > current.order_index)
      && !["absent", "removed", "skipped", "done"].includes(item.status)) || null;
  }

  function syncScheduledPlayback() {
    const current = currentItem();
    const next = nextEligibleItem(current);
    if (eventState?.status !== "running" || !next) {
      window.clearTimeout(scheduleTimer);
      scheduleTimer = null;
      scheduleSignature = "";
      return;
    }
    const eventStart = localTimestamp(eventState.event_start_time);
    const itemStart = scheduledTimestamp(next);
    const timestamp = eventStart === null
      ? itemStart
      : itemStart === null ? eventStart : Math.max(eventStart, itemStart);
    if (timestamp === null) return;
    const signature = `${eventState.id}:${current?.id || "none"}:${next.id}:${timestamp}`;
    if (scheduleSignature === signature && scheduleTimer !== null) return;
    window.clearTimeout(scheduleTimer);
    scheduleSignature = signature;

    const runWhenReady = () => {
      scheduleTimer = null;
      if (eventState?.status !== "running"
        || (currentItem()?.id || null) !== (current?.id || null)
        || nextEligibleItem(currentItem())?.id !== next.id) {
        syncScheduledPlayback();
        return;
      }
      const remaining = timestamp - Date.now();
      if (remaining > 0) {
        scheduleTimer = window.setTimeout(runWhenReady, Math.min(remaining, 2_147_000_000));
        return;
      }
      const endingItem = currentItem();
      const endingAt = endingItem && scheduledEndTimestamp(
        endingItem,
        nextEligibleItem(endingItem),
      );
      if (endingItem?.status === "on_stage" && isAutomaticCaptureType(endingItem)
        && endingAt !== null && endingAt <= Date.now()) {
        if (!autoFinishingItemId) void finishSpeechAutomatically(endingItem);
        scheduleTimer = window.setTimeout(runWhenReady, 1000);
        return;
      }
      const audioPlaying = audioPlaybackActive
        || Boolean(activeUtterance && window.speechSynthesis?.speaking);
      if (announcementInProgress || audioPlaying) {
        scheduleTimer = window.setTimeout(runWhenReady, 1000);
        return;
      }
      scheduleSignature = "";
      void callNow(next.id);
    };
    scheduleTimer = window.setTimeout(
      runWhenReady,
      Math.max(0, Math.min(timestamp - Date.now(), 2_147_000_000)),
    );
  }

  function scheduledEndTimestamp(item, next) {
    const start = scheduledTimestamp(item);
    const duration = Number(item.planned_duration) || 0;
    if (start !== null && duration > 0) return start + duration * 60_000;
    return next?.scheduled_time ? scheduledTimestamp(next) : null;
  }

  function syncScheduledFinish() {
    const current = currentItem();
    const next = nextEligibleItem(current);
    if (eventState?.status !== "running" || !current
      || current.status !== "on_stage" || !isAutomaticCaptureType(current)) {
      window.clearTimeout(finishTimer);
      finishTimer = null;
      finishSignature = "";
      return;
    }
    const timestamp = scheduledEndTimestamp(current, next);
    if (timestamp === null || autoFinishingItemId === current.id) return;
    const signature = `${eventState.id}:${current.id}:${timestamp}`;
    if (finishSignature === signature && finishTimer !== null) return;
    window.clearTimeout(finishTimer);
    finishSignature = signature;
    const finishWhenDue = () => {
      finishTimer = null;
      if (eventState?.status !== "running" || currentItem()?.id !== current.id) {
        syncScheduledFinish();
        return;
      }
      const remaining = timestamp - Date.now();
      if (remaining > 0) {
        finishTimer = window.setTimeout(finishWhenDue, Math.min(remaining, 2_147_000_000));
        return;
      }
      if (announcementInProgress || audioPlaybackActive) {
        finishTimer = window.setTimeout(finishWhenDue, 1000);
        return;
      }
      finishSignature = "";
      void finishSpeechAutomatically(current);
    };
    finishTimer = window.setTimeout(
      finishWhenDue,
      Math.max(0, Math.min(timestamp - Date.now(), 2_147_000_000)),
    );
  }

  async function finishSpeechAutomatically(item) {
    if (autoFinishingItemId || currentItem()?.id !== item.id) return;
    autoFinishingItemId = item.id;
    captureSuppressedItemId = item.id;
    announcementInProgress = true;
    try {
      await stopCapture();
      if (currentItem()?.id !== item.id) return;
      await persistTranscript();
      const result = await post(`/api/live-events/${eventId}/items/${item.id}/finish`, {
        language: eventState.language,
      });
      await refresh();
      notify(`Summary ready for ${item.title}.`);
      await playSummaryAfterSpeech(item, result.summary);
    } catch (error) {
      notify(`Could not automatically finish ${item.title}: ${error.message}`);
    } finally {
      autoFinishingItemId = 0;
      announcementInProgress = false;
      syncScheduledFinish();
      syncScheduledPlayback();
    }
  }

  async function callNow(itemId = null, { includeOpening = false } = {}) {
    if (!eventId) {
      notify("Set up an event before calling a schedule item.");
      return;
    }
    if (eventState?.status === "ended") {
      notify("This event has ended; its schedule can no longer be called.");
      return;
    }
    stopBrowserAudio();
    const generation = audioGeneration;
    announcementInProgress = true;
    try {
      try {
        await unlockAudio();
      } catch {
        notify("Audio unlock failed; trying browser speech after the announcement is prepared.");
      }
      await loadSavedScriptForPlayback();
      const current = currentItem();
      const target = itemId === null
        ? nextEligibleItem(current)
        : eventState.items.find((item) => item.id === itemId);
      if (!target) {
        notify("There is no eligible schedule item to announce.");
        return;
      }
      const preparedSegments = [];
      let missingLiveTransition = false;
      if (includeOpening) {
        const opening = scriptSegment("opening");
        if (opening) preparedSegments.push(opening);
      }
      if (current && current.id !== target.id) {
        const transition = scriptSegment(`transition-${current.id}-${target.id}`);
        if (transition) preparedSegments.push(transition);
        else missingLiveTransition = true;
      }
      const itemSegment = scriptSegment(
        `${target.type === "break" ? "break" : "intro"}-${target.id}`,
      );
      if (itemSegment) preparedSegments.push(itemSegment);

      let result;
      if (preparedSegments.length && !missingLiveTransition) {
        const lineKey = String(target.id);
        const savedLines = readScriptLines();
        if (savedLines[lineKey]?.manual && itemSegment) {
          preparedSegments[preparedSegments.length - 1] = {
            ...itemSegment,
            text: savedLines[lineKey].text,
          };
        }
        if (current?.id !== target.id) {
          result = await afterMutation(() => post(
            `/api/live-events/${eventId}/jump/${target.id}`,
          ));
        } else {
          result = { event: eventState };
        }
        const displayLine = itemSegment?.text || preparedSegments[preparedSegments.length - 1].text;
        if (!savedLines[lineKey]?.manual) saveScriptLine(lineKey, displayLine, false);
        if (activeScriptKey === lineKey) $("#current-script-line").value = displayLine;
        if (generation !== audioGeneration) return;
        const played = await speakScriptSegments(preparedSegments, generation);
        if (!played && generation === audioGeneration) {
          notify("The saved script is ready, but browser speech could not play it.");
        }
        return;
      }

      result = await afterMutation(() => post(
        `/api/live-events/${eventId}/call-now`,
        {
          ...(itemId === null ? {} : { item_id: itemId }),
          include_summary: $("#include-transition").checked,
        },
      ));
      if (generation !== audioGeneration) return;
      const lineKey = String(result.event.current_item_id || "");
      const savedLines = readScriptLines();
      if (lineKey && !savedLines[lineKey]?.manual) {
        saveScriptLine(lineKey, result.text || "", false);
        if (activeScriptKey === lineKey) $("#current-script-line").value = result.text || "";
      }
      if (result.notice) notify(result.notice);
      const played = result.audio_url ? await playUrl(result.audio_url) : false;
      if (!played && !speakFallback(result.text, result.event.language)) {
        notify(result.text || "The announcement is ready, but no speech output is available.");
      }
    } catch (error) {
      notify(error.message);
    } finally {
      announcementInProgress = false;
      const current = currentItem();
      if (current && isAutomaticCaptureType(current) && current.status === "on_stage"
        && current.id !== captureSuppressedItemId && capturingItemId !== current.id) {
        void beginCapture(current);
      }
      syncScheduledPlayback();
    }
  }

  async function startEventFlow() {
    if (!eventId || !eventState) {
      notify("No event is loaded. Set up an event before starting anchoring.");
      return;
    }
    if (eventState.status === "ended") {
      notify("This event has ended and cannot be restarted.");
      return;
    }
    const startImmediately = startPending;
    if (startImmediately) {
      window.clearTimeout(pendingStartTimer);
      pendingStartTimer = null;
      startPending = false;
      render(eventState);
    }
    try {
      try {
        await unlockAudio();
      } catch (error) {
        notify(`Audio could not be unlocked; anchoring will still start: ${error.message}`);
      }
      if (eventState.status === "running") {
        await loadSavedScriptForPlayback();
        const opening = scriptSegment("opening");
        if (!currentItem() && opening && openingPlayedEventId !== eventId) {
          announcementInProgress = true;
          try {
            if (await speakScriptSegments([opening])) openingPlayedEventId = eventId;
          } finally {
            announcementInProgress = false;
          }
        }
        syncScheduledPlayback();
        notify("Anchoring audio resumed. Scheduled program announcements are active.");
        return;
      }
      if (startImmediately) {
        await startEventAtScheduledTime();
        return;
      }
      const timestamp = localTimestamp(eventState.event_start_time) ?? Date.now();
      const delay = timestamp - Date.now();
      if (delay > 0) {
        startPending = true;
        render(eventState);
        notify(`Anchoring will start at ${eventState.event_start_time} laptop time.`);
        const startWhenReady = () => {
          const remaining = timestamp - Date.now();
          if (remaining > 0) {
            pendingStartTimer = window.setTimeout(
              startWhenReady,
              Math.min(remaining, 2_147_000_000),
            );
            return;
          }
          pendingStartTimer = null;
          void startEventAtScheduledTime();
        };
        pendingStartTimer = window.setTimeout(
          startWhenReady,
          Math.min(delay, 2_147_000_000),
        );
      } else {
        await startEventAtScheduledTime();
      }
    } catch (error) {
      notify(error.message);
    }
  }

  async function startEventAtScheduledTime() {
    startPending = false;
    announcementInProgress = true;
    if (eventState) render(eventState);
    try {
      if (!eventState) throw new Error("No event is loaded.");
      const existingCurrent = eventState.status === "paused" ? currentItem() : null;
      const started = await post(
        `/api/live-events/${eventId}/start`,
        { defer_first_item: !existingCurrent },
      );
      render(started);
      await loadSavedScriptForPlayback();
      if (existingCurrent) {
        await callNow(existingCurrent.id);
        return;
      }
      const opening = scriptSegment("opening");
      if (opening) {
        stopBrowserAudio();
        const played = await speakScriptSegments([opening]);
        if (played) openingPlayedEventId = eventId;
        if (!played) notify("Anchoring started, but the opening audio could not be played.");
      }
      syncScheduledPlayback();
      if (!started.items.some((item) => !["absent", "removed", "skipped"].includes(item.status))) {
        notify("The event started, but there are no eligible program items to announce.");
      } else {
        notify("Anchoring started. Programs will be introduced at their scheduled laptop times.");
      }
    } catch (error) {
      notify(error.message);
    } finally {
      announcementInProgress = false;
      syncScheduledPlayback();
    }
  }

  function startRecorderSegment(item) {
    if (!captureEnabled || !mediaStream || capturingItemId !== item.id) return;
    const preferredType = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]
      .find((type) => MediaRecorder.isTypeSupported(type));
    recorder = preferredType
      ? new MediaRecorder(mediaStream, { mimeType: preferredType })
      : new MediaRecorder(mediaStream);
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
    const extension = blob.type.includes("mp4") ? "mp4" : "webm";
    body.append("audio", blob, `speech-segment.${extension}`);
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
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      captureEnabled = false;
      capturingItemId = 0;
      $("#capture-state").textContent = "Audio recording is not supported by this browser";
      notify("Speech recording requires a browser with microphone and MediaRecorder support.");
      return;
    }
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
    } catch (error) {
      captureEnabled = false;
      capturingItemId = 0;
      mediaStream?.getTracks().forEach((track) => track.stop());
      mediaStream = null;
      audioContext = null;
      analyser = null;
      $("#capture-state").textContent = "Microphone unavailable — paste notes or transcript below";
      notify(`Could not start microphone capture: ${error.message}`);
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
  $("#start-event").addEventListener("click", () => void startEventFlow());
  $("#pause-event").addEventListener("click", () => runAction(`/api/live-events/${eventId}/pause`));
  $("#end-event").addEventListener("click", () => {
    if (!window.confirm("Stop and close this event?")) return;
    stopBrowserAudio();
    void (async () => {
      try {
        const ended = await afterMutation(() => post(`/api/live-events/${eventId}/end`));
        saveScriptLine("closing", ended.closing_script || "", false);
        activeScriptKey = "closing";
        $("#script-item-title").textContent = "Event closing";
        $("#current-script-line").value = ended.closing_script || "";
        notify("Event stopped. The closing script is ready to copy.");
      } catch (error) {
        notify(error.message);
      }
    })();
  });
  $("#previous-item").addEventListener("click", () => runAction(`/api/live-events/${eventId}/previous`));
  $("#next-item").addEventListener("click", () => void callNow());
  $("#current-script-line").addEventListener("input", (event) => {
    if (activeScriptKey) saveScriptLine(activeScriptKey, event.currentTarget.value, true);
  });
  $("#copy-script-line").addEventListener("click", async () => {
    const text = $("#current-script-line").value.trim();
    if (!text) {
      notify("There is no script line to copy yet.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      notify("Script line copied.");
    } catch (error) {
      notify(`Could not copy the script line: ${error.message}`);
    }
  });
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
      scheduled_time: String(data.get("scheduled_time") || ""),
    };
    if (rawPosition) item.position = Number(rawPosition) - 1;
    try {
      await afterMutation(() => post("/api/event/items", item));
      form.reset();
    } catch (error) {
      notify(error.message);
    }
  });
  $("#edit-item-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const item = eventState?.items.find((entry) => entry.id === editingItemId);
    if (!item) {
      $("#edit-item-dialog").close();
      notify("That program item is no longer in the live schedule.");
      return;
    }
    try {
      await afterMutation(() => patch(`/api/event/items/${item.id}`, {
        event_id: eventId,
        title: String(data.get("title") || "").trim(),
        type: String(data.get("type") || "other"),
        scheduled_time: String(data.get("scheduled_time") || ""),
        person_name: String(data.get("person_name") || "").trim(),
        designation: String(data.get("designation") || "").trim(),
        planned_duration: Number(data.get("planned_duration") || 0),
        participant_details: String(data.get("participant_details") || "").trim(),
      }));
      $("#edit-item-dialog").close();
      notify("Program item updated.");
    } catch (error) {
      notify(error.message);
    }
  });
  ["#cancel-item-edit", "#cancel-item-edit-secondary"].forEach((selector) => {
    $(selector).addEventListener("click", () => $("#edit-item-dialog").close());
  });
  $("#capture-toggle").addEventListener("click", () => {
    const item = currentItem();
    if (!isSpeechOrTalk(item) || item.status !== "on_stage") {
      notify("Start a speech, talk, or announcement item before capturing its transcript.");
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
    if (!isSpeechOrTalk(item)) return;
    captureSuppressedItemId = item.id;
    await stopCapture();
    try {
      await persistTranscript();
      const result = await post(`/api/live-events/${eventId}/items/${item.id}/finish`, {
        language: eventState.language,
      });
      await refresh();
      $("#summary-editor").value = result.summary;
      notify("Speaker summary ready for review. The event recap is spoken at its scheduled end time.");
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
