(() => {
  "use strict";

  const api = window.AVAAnchoringStudioAPI;
  if (!api) throw new Error("Anchoring Studio API client did not load.");

  const $ = (selector, root = document) => root.querySelector(selector);
  const form = $("#studio-setup-form");
  const itemList = $("#studio-items-list");
  const scriptList = $("#studio-script-list");
  const state = {
    sessionId: localStorage.getItem("ava-session-id") || "",
    event: null,
    script: [],
    requestPending: false,
    autoCloseBusy: false,
    retryTargets: null,
    saveTimer: 0,
    draftGeneration: 0,
    speechRecorder: null,
    speechStream: null,
    speechChunks: [],
    discardSpeechSample: false,
  };
  const typeOptions = [
    ["participant", "Participant"],
    ["performance", "Performance"],
    ["speech", "Speech"],
    ["talk", "Talk"],
    ["break", "Break"],
    ["announcement", "Announcement"],
    ["other", "Other"],
  ];
  function setError(element, message = "") {
    element.textContent = message;
    element.hidden = !message;
  }

  function setSpeechStatus(message) {
    $("#speech-test-status").textContent = message;
  }

  function releaseSpeechStream() {
    state.speechStream?.getTracks().forEach((track) => track.stop());
    state.speechStream = null;
  }

  async function transcribeSpeechSample(blob) {
    const generation = state.draftGeneration;
    if (!blob.size) {
      setError($("#speech-test-error"), "The recording was empty. Please record again.");
      setSpeechStatus("No audio captured");
      return;
    }
    setSpeechStatus("Transcribing speech…");
    $("#speech-record-start").disabled = true;
    try {
      const result = await api.transcribeSpeech(
        blob,
        form.elements.namedItem("language").value || "en",
        state.event?.id,
      );
      if (!result.text.trim()) {
        throw new Error("No speech was recognized. Try a clearer or longer recording.");
      }
      if (generation !== state.draftGeneration) return;
      $("#speech-test-transcript").value = result.text;
      $("#speech-commentary-result").hidden = true;
      setError($("#speech-test-error"));
      setSpeechStatus(`Transcribed · ${result.language}`);
    } catch (error) {
      if (generation !== state.draftGeneration) return;
      setError($("#speech-test-error"), error.message);
      setSpeechStatus("Transcription failed");
    } finally {
      $("#speech-record-start").disabled = false;
    }
  }

  async function startSpeechTestRecording() {
    setError($("#speech-test-error"));
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      setError($("#speech-test-error"), "Speech recording is not supported by this browser.");
      return;
    }
    try {
      state.speechStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      state.speechChunks = [];
      const preferredType = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]
        .find((type) => MediaRecorder.isTypeSupported(type));
      state.speechRecorder = preferredType
        ? new MediaRecorder(state.speechStream, { mimeType: preferredType })
        : new MediaRecorder(state.speechStream);
      state.speechRecorder.addEventListener("dataavailable", (event) => {
        if (event.data.size) state.speechChunks.push(event.data);
      });
      state.speechRecorder.addEventListener("stop", () => {
        const recording = new Blob(state.speechChunks, {
          type: state.speechRecorder?.mimeType || "audio/webm",
        });
        state.speechRecorder = null;
        state.speechChunks = [];
        releaseSpeechStream();
        $("#speech-record-stop").disabled = true;
        if (state.discardSpeechSample) {
          state.discardSpeechSample = false;
        } else {
          void transcribeSpeechSample(recording);
        }
      }, { once: true });
      state.speechRecorder.start();
      $("#speech-record-start").disabled = true;
      $("#speech-record-stop").disabled = false;
      setSpeechStatus("Recording · speak clearly, then stop");
    } catch (error) {
      releaseSpeechStream();
      setError($("#speech-test-error"), `Could not start microphone recording: ${error.message}`);
      setSpeechStatus("Microphone unavailable");
    }
  }

  function stopSpeechTestRecording() {
    if (state.speechRecorder?.state === "recording") {
      state.speechRecorder.stop();
      setSpeechStatus("Preparing transcription…");
    }
  }

  async function analyzeSpeechSample() {
    const transcript = $("#speech-test-transcript").value.trim();
    if (transcript.length < 3) {
      setError($("#speech-test-error"), "Record or enter a speech transcript before analysis.");
      return;
    }
    setError($("#speech-test-error"));
    if (!state.event && !(await saveEvent())) return;
    const button = $("#speech-analyze");
    const generation = state.draftGeneration;
    button.disabled = true;
    button.textContent = "Analyzing speech…";
    setSpeechStatus("Finding key points and drafting an anchoring comment…");
    try {
      const result = await api.analyzeSpeech(
        state.event.id,
        transcript,
        form.elements.namedItem("language").value || state.event.language || "en",
      );
      if (generation !== state.draftGeneration) return;
      const points = $("#speech-key-points");
      points.replaceChildren();
      result.key_points.forEach((point) => {
        const item = document.createElement("li");
        item.textContent = point;
        points.append(item);
      });
      $("#speech-commentary-script").value = result.commentary_script;
      $("#speech-commentary-result").hidden = false;
      setSpeechStatus("Commentary draft ready");
    } catch (error) {
      if (generation !== state.draftGeneration) return;
      setError($("#speech-test-error"), error.message);
      setSpeechStatus("Analysis failed");
    } finally {
      button.disabled = false;
      button.textContent = "Analyze and draft comment";
    }
  }

  function saveDraft() {
    try {
      localStorage.setItem("ava-anchoring-studio", JSON.stringify({
        values: Object.fromEntries(new FormData(form).entries()),
        items: collectItems(),
        eventId: state.event?.id || 0,
      }));
      $("#studio-save-status").textContent = "Draft saved locally";
      return true;
    } catch (error) {
      $("#studio-save-status").textContent = "Draft could not be saved";
      setError($("#studio-validation-error"), "Browser storage is unavailable. Save or copy your event details before leaving.");
      console.error("Anchoring Studio autosave failed", error);
      return false;
    }
  }

  function collectItems() {
    return [...itemList.querySelectorAll(".studio-item-row")].map((row, index) => ({
      clientKey: row.dataset.key,
      title: row.querySelector('[data-field="title"]').value.trim(),
      type: row.querySelector('[data-field="type"]').value,
      person_name: row.querySelector('[data-field="person_name"]').value.trim(),
      designation: row.querySelector('[data-field="designation"]').value.trim(),
      participant_details: row.querySelector('[data-field="participant_details"]').value.trim(),
      planned_duration: Number(row.querySelector('[data-field="planned_duration"]').value) || 0,
      order_index: index,
      notes: row.querySelector('[data-field="notes"]').value.trim(),
      scheduled_time: row.querySelector('[data-field="scheduled_time"]').value,
    }));
  }

  function sortItemsByScheduledTime() {
    const items = collectItems().map((item, index) => ({ ...item, originalIndex: index }));
    items.sort((left, right) => {
      if (!left.scheduled_time && !right.scheduled_time) {
        return left.originalIndex - right.originalIndex;
      }
      if (!left.scheduled_time) return 1;
      if (!right.scheduled_time) return -1;
      return left.scheduled_time.localeCompare(right.scheduled_time)
        || left.originalIndex - right.originalIndex;
    });
    items.forEach((item) => delete item.originalIndex);
    renderItems(items);
  }

  function control(tag, field, label, value = "", attributes = {}) {
    const wrapper = document.createElement("label");
    wrapper.textContent = label;
    const element = document.createElement(tag);
    element.dataset.field = field;
    if (tag === "select") {
      typeOptions.forEach(([key, text]) => {
        const option = document.createElement("option");
        option.value = key;
        option.textContent = text;
        element.append(option);
      });
      element.value = value || "participant";
    } else {
      element.value = value ?? "";
      if (tag === "textarea") element.rows = 2;
    }
    Object.entries(attributes).forEach(([key, attrValue]) => {
      if (key in element) element[key] = attrValue;
      else element.setAttribute(key, attrValue);
    });
    wrapper.append(element);
    return wrapper;
  }

  function renderItems(items = collectItems()) {
    itemList.replaceChildren();
    items.forEach((item, index) => {
      const row = document.createElement("article");
      row.className = "studio-item-row";
      row.dataset.key = item.clientKey || crypto.randomUUID();
      const heading = document.createElement("div");
      heading.className = "studio-item-heading";
      const title = document.createElement("strong");
      title.textContent = `${index + 1}. ${item.title || item.person_name || typeOptions.find(([key]) => key === item.type)?.[1] || "Program item"}`;
      const actions = document.createElement("div");
      actions.className = "studio-row-actions";
      [["up", "Move up"], ["down", "Move down"], ["remove", "Remove"]].forEach(([action, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "studio-icon-action";
        button.dataset.action = action;
        button.textContent = { up: "↑", down: "↓", remove: "×" }[action];
        button.title = label;
        button.setAttribute("aria-label", label);
        button.disabled = (action === "up" && index === 0)
          || (action === "down" && index === items.length - 1);
        actions.append(button);
      });
      heading.append(title, actions);
      const fields = document.createElement("div");
      fields.className = "studio-item-fields";
      fields.append(
        control("select", "type", "Item type", item.type),
        control("input", "title", "Program / topic", item.title, { maxLength: 200, placeholder: "Opening prayer, keynote…" }),
        control("input", "person_name", "Participant name", item.person_name, { maxLength: 200, placeholder: "Name (optional for breaks)" }),
        control("input", "designation", "Role / designation", item.designation, { maxLength: 200, placeholder: "Role, department…" }),
        control("input", "planned_duration", "Duration (minutes)", item.planned_duration, { type: "number", min: 0, max: 1440 }),
        control("input", "scheduled_time", "Scheduled time", item.scheduled_time, { type: "time" }),
        control("textarea", "participant_details", "Topic / introduction details", item.participant_details, { maxLength: 1200, placeholder: "Confirmed details for the introduction" }),
        control("textarea", "notes", "Speaker notes / transcript", item.notes, { maxLength: 4000, placeholder: "Optional notes for the closing summary" }),
      );
      row.append(heading, fields);
      itemList.append(row);
    });
  }

  function appendItem(defaults = {}) {
    const items = collectItems();
    items.push({
      clientKey: crypto.randomUUID(),
      type: "participant",
      title: "",
      person_name: "",
      designation: "",
      participant_details: "",
      planned_duration: 0,
      scheduled_time: "",
      notes: "",
      ...defaults,
    });
    renderItems(items);
    saveDraft();
  }

  function formValues() {
    const data = Object.fromEntries(new FormData(form).entries());
    return {
      ...data,
      sponsors: String(data.sponsors || "").split(",").map((value) => value.trim()).filter(Boolean),
      schedule: collectItems().map((item) => ({
        title: item.title || item.person_name || typeOptions.find(([key]) => key === item.type)?.[1],
        type: item.type,
        participant: item.person_name,
        person_name: item.person_name,
        designation: item.designation,
        participant_details: item.participant_details,
        duration_minutes: item.planned_duration,
        planned_duration: item.planned_duration,
        time: item.scheduled_time,
        scheduled_time: item.scheduled_time,
        notes: item.notes,
        performance_style: String(data.style || "Professional host"),
      })),
    };
  }

  function validateForm() {
    setError($("#studio-validation-error"));
    for (const field of form.querySelectorAll("[required]")) {
      if (!field.value.trim()) {
        field.focus();
        field.setAttribute("aria-invalid", "true");
        setError($("#studio-validation-error"), `${field.labels?.[0]?.textContent.trim() || "This field"} is required.`);
        return false;
      }
      field.removeAttribute("aria-invalid");
    }
    const values = formValues();
    if (values.event_end_time <= values.event_start_time) {
      const end = form.elements.namedItem("event_end_time");
      end.focus();
      end.setAttribute("aria-invalid", "true");
      setError($("#studio-validation-error"), "End time must be later than start time.");
      return false;
    }
    const items = collectItems();
    if (!items.length || items.some((item) => !item.title && !item.person_name)) {
      if (!items.length) appendItem();
      setError($("#studio-validation-error"), items.length
        ? "Give every running-order item a program/topic or participant name."
        : "Add at least one participant or special program item.");
      itemList.scrollIntoView({ behavior: "smooth", block: "center" });
      return false;
    }
    if (!items.some((item) => ["participant", "performance", "speech", "talk"].includes(item.type))) {
      setError($("#studio-validation-error"), "Add at least one participant, speaker, or performer to the running order.");
      return false;
    }
    if (items.some((item) => !Number.isInteger(item.planned_duration) || item.planned_duration < 0)) {
      setError($("#studio-validation-error"), "Duration must be a non-negative whole number of minutes.");
      return false;
    }
    return true;
  }

  function eligibleItems(event = state.event) {
    return (event?.items || []).filter((item) => !["absent", "skipped", "removed"].includes(item.status));
  }

  function updateEvent(event) {
    state.event = event;
    localStorage.setItem("ava-event-id", String(event.id));
    saveDraft();
  }

  async function saveEvent() {
    if (state.requestPending) return false;
    sortItemsByScheduledTime();
    if (!validateForm()) return;
    state.requestPending = true;
    $("#studio-new-event").disabled = true;
    $("#save-event-details").disabled = true;
    $("#generate-script").disabled = true;
    const payload = formValues();
    payload.session_id = state.sessionId;
    payload.language = payload.language || "en";
    payload.speakers = [];
    try {
      let result = state.event
        ? await api.patchEvent(state.event.id, {
          name: payload.name,
          type: payload.event_type,
          date: payload.event_date,
          event_start_time: payload.event_start_time,
          event_end_time: payload.event_end_time,
          venue: payload.venue,
          host: payload.host,
          sponsors_json: JSON.stringify(payload.sponsors),
          audience: payload.audience,
          style: payload.style,
          special_instructions: payload.special_instructions,
          chief_guest: payload.chief_guest,
          chief_guest_title: payload.chief_guest_title,
          tone: payload.tone,
          language: payload.language,
        })
        : await api.createEvent(payload);
      if (!state.event) {
        const live = await api.getEvent(result.id);
        const initialScript = await api.getScript(result.id);
        state.script = initialScript.segments;
        updateEvent(live);
      } else {
        const requestedItems = payload.schedule;
        const draftItems = collectItems();
        const originalItems = [...result.items];
        const desiredIds = new Set();
        const desiredOrder = [];
        for (let index = 0; index < requestedItems.length; index += 1) {
          const desired = requestedItems[index];
          const candidateId = Number(draftItems[index]?.clientKey);
          const existingItem = result.items.find((item) => item.id === candidateId);
          const changes = {
            title: desired.title,
            type: desired.type,
            person_name: desired.person_name,
            designation: desired.designation,
            participant_details: desired.participant_details,
            planned_duration: desired.planned_duration,
            scheduled_time: desired.scheduled_time,
            notes: desired.notes,
          };
          if (existingItem) {
            result = await api.patchItem(result.id, candidateId, changes);
            desiredIds.add(candidateId);
            desiredOrder.push(candidateId);
          } else {
            const beforeIds = new Set(result.items.map((item) => item.id));
            result = await api.addItem(result.id, changes);
            const added = result.items.find((item) => !beforeIds.has(item.id));
            if (added) {
              desiredIds.add(added.id);
              desiredOrder.push(added.id);
            }
          }
        }
        for (const item of originalItems) {
          if (!desiredIds.has(item.id) && item.status !== "removed") {
            result = await api.deleteItem(result.id, item.id);
          }
        }
        const allIds = result.items.map((item) => item.id);
        const fullOrder = [...desiredOrder, ...allIds.filter((id) => !desiredIds.has(id))];
        if (fullOrder.length === allIds.length) result = await api.reorder(result.id, fullOrder);
        await loadEventIntoForm(result);
        updateEvent(result);
      }
      $("#studio-save-status").textContent = "Event saved";
      return true;
    } catch (error) {
      setError($("#studio-validation-error"), error.message);
      return false;
    } finally {
      state.requestPending = false;
      $("#studio-new-event").disabled = false;
      $("#save-event-details").disabled = false;
      $("#generate-script").disabled = false;
    }
  }

  function renderScript() {
    scriptList.replaceChildren();
    if (!state.script.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "Generate a script to see editable segments here.";
      scriptList.append(empty);
      return;
    }
    state.script.forEach((segment, index) => {
      const card = document.createElement("article");
      card.className = "script-segment-card";
      const heading = document.createElement("div");
      heading.className = "script-segment-heading";
      const title = document.createElement("strong");
      title.textContent = `${index + 1}. ${segment.type.charAt(0).toUpperCase()}${segment.type.slice(1)}`;
      const meta = document.createElement("span");
      meta.textContent = `${segment.estimatedSeconds}s${segment.edited ? " · Edited" : ""}`;
      if (segment.edited) meta.className = "edited-indicator";
      heading.append(title, meta);
      const editor = document.createElement("textarea");
      editor.value = segment.text;
      editor.rows = Math.max(3, Math.min(8, Math.ceil(segment.text.length / 95)));
      editor.maxLength = 8000;
      editor.setAttribute("aria-label", `${segment.type} segment text`);
      editor.addEventListener("input", () => {
        segment.text = editor.value;
        segment.edited = true;
        meta.textContent = `${segment.estimatedSeconds}s · Edited`;
        meta.className = "edited-indicator";
        scheduleScriptSave();
      });
      const controls = document.createElement("div");
      controls.className = "script-segment-actions";
      const regenerate = document.createElement("button");
      regenerate.type = "button";
      regenerate.className = "text-action";
      regenerate.textContent = "Regenerate";
      regenerate.addEventListener("click", async () => {
        if (segment.edited && !window.confirm("This segment has hand-edited text. Replace it with a new Gemini version?")) return;
        await regenerateTargets([{
          id: segment.id, type: segment.type, itemId: segment.itemId,
        }], segment.edited ? [segment.id] : []);
      });
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "text-action";
      preview.textContent = "▶ Preview audio";
      preview.addEventListener("click", () => speak(segment.text));
      controls.append(regenerate, preview);
      card.append(heading, editor, controls);
      scriptList.append(card);
    });
  }

  function scheduleScriptSave() {
    window.clearTimeout(state.saveTimer);
    state.saveTimer = window.setTimeout(async () => {
      if (!state.event) return;
      try {
        const saved = await api.saveScript(state.event.id, state.script);
        $("#undo-script").disabled = saved.version < 2;
      } catch (error) {
        setError($("#script-error"), `Could not save the edited script: ${error.message}`);
      }
    }, 450);
  }

  async function regenerateTargets(targets, replaceEditedIds = []) {
    if (!state.event || state.requestPending) return;
    const replace = new Set(replaceEditedIds);
    const editedTargets = targets.filter((target) => (
      state.script.some((segment) => segment.id === target.id && segment.edited)
      && !replace.has(target.id)
    ));
    if (editedTargets.length) {
      const confirmed = window.confirm(
        `Replace these hand-edited script segments?\n\n${editedTargets.map((target) => target.id).join(", ")}`,
      );
      if (confirmed) editedTargets.forEach((target) => replace.add(target.id));
      else targets = targets.filter((target) => !editedTargets.some((edited) => edited.id === target.id));
    }
    if (!targets.length) return;
    state.requestPending = true;
    $("#studio-new-event").disabled = true;
    $("#script-loading").hidden = false;
    state.retryTargets = targets;
    setError($("#script-error"));
    try {
      const result = await api.generate(state.event.id, targets, [...replace]);
      if (result.protected_ids?.length) {
        const allow = window.confirm("Some affected segments were edited by hand. Replace these edited lines?\n\n"
          + result.protected_ids.join(", "));
        if (allow) {
          const retry = await api.generate(state.event.id, targets, result.protected_ids);
          state.script = retry.segments;
        } else {
          state.script = result.segments;
        }
      } else {
        state.script = result.segments;
      }
      state.retryTargets = null;
      $("#retry-script").hidden = true;
      renderScript();
    } catch (error) {
      state.retryTargets = targets;
      setError($("#script-error"), error.message);
      $("#retry-script").hidden = false;
    } finally {
      $("#script-loading").hidden = true;
      state.requestPending = false;
      $("#studio-new-event").disabled = false;
    }
  }

  function fullScriptTargets() {
    const active = eligibleItems();
    return [
      { id: "opening", type: "opening", itemId: null },
      ...active.map((item) => ({
        id: `${item.type === "break" ? "break" : "intro"}-${item.id}`,
        type: item.type === "break" ? "break" : "intro",
        itemId: item.id,
      })),
      ...active.slice(1).map((item, index) => ({
        id: `transition-${active[index].id}-${item.id}`,
        type: "transition",
        itemId: item.id,
      })),
      { id: "closing", type: "closing", itemId: null },
    ];
  }

  async function generateFullScript() {
    if (!validateForm()) return;
    if (!await saveEvent()) return;
    $("#generate-script").disabled = true;
    $("#script-loading").hidden = false;
    try {
      await regenerateTargets(fullScriptTargets());
    } finally {
      $("#generate-script").disabled = false;
      $("#script-loading").hidden = true;
    }
  }

  function speak(text, onEnd = null) {
    if (!("speechSynthesis" in window)) {
      setError($("#script-error"), "Speech synthesis is not supported in this browser.");
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    const voice = speechSynthesis.getVoices().find((candidate) => candidate.name === $("#studio-voice").value);
    if (voice) utterance.voice = voice;
    utterance.rate = Number($("#studio-speed").value) || 1;
    utterance.pitch = Number($("#studio-pitch").value) || 1;
    utterance.lang = form.elements.namedItem("language").value === "ml" ? "ml-IN" : "en-IN";
    utterance.onend = () => onEnd?.();
    utterance.onerror = () => {
      if (onEnd) setError($("#script-error"), "Closing audio failed. The event remains open and can be closed from the event controls.");
    };
    window.speechSynthesis.speak(utterance);
  }

  function initializeVoices() {
    const select = $("#studio-voice");
    const populate = () => {
      const chosen = select.value;
      select.replaceChildren(new Option("System default", ""));
      speechSynthesis.getVoices().forEach((voice) => select.add(new Option(`${voice.name} · ${voice.lang}`, voice.name)));
      select.value = chosen;
    };
    if ("speechSynthesis" in window) {
      populate();
      speechSynthesis.addEventListener("voiceschanged", populate);
    }
  }

  async function refreshLiveEvent() {
    if (!state.event) return;
    try {
      updateEvent(await api.getEvent(state.event.id));
    } catch (error) {
      setError($("#script-error"), error.message);
    }
  }

  async function closeEvent(automatic = false) {
    if (!state.event || (!automatic && !window.confirm("Stop the event and play the closing now?"))) return;
    try {
      let result;
      try {
        result = await api.close(state.event.id, form.elements.namedItem("language").value);
      } catch (error) {
        if (!error.message.includes("edited by hand")) throw error;
        if (!window.confirm("The closing segment has hand-edited text. Replace it with the event closing?")) {
          return;
        }
        result = await api.close(state.event.id, form.elements.namedItem("language").value, true);
      }
      speak(result.closing.text, async () => {
        try {
          updateEvent(await api.end(state.event.id));
        } catch (error) {
          setError($("#script-error"), `Closing was played, but event completion could not be saved: ${error.message}`);
          await refreshLiveEvent();
        }
      });
    } catch (error) {
      setError($("#script-error"), error.message);
    }
  }

  async function loadSessionEvent(eventDatabaseId) {
    try {
      const event = await api.getSessionEvent(state.sessionId);
      if (event.id !== Number(eventDatabaseId)) return;
      const alreadyLoaded = state.event?.id === event.id;
      if (!alreadyLoaded) await loadEventIntoForm(event);
      const savedScript = await api.getScript(event.id);
      state.script = savedScript.segments;
      renderScript();
      updateEvent(event);
    } catch (error) {
      if (error.message !== "No live event is linked to this session.") {
        setError($("#script-error"), error.message);
      }
    }
  }

  async function loadEventIntoForm(event) {
    const items = event.items.map((item) => ({
      clientKey: String(item.id),
      title: item.title,
      type: item.type,
      person_name: item.person_name,
      designation: item.designation,
      participant_details: item.participant_details,
      planned_duration: item.planned_duration,
      scheduled_time: item.scheduled_time,
      notes: item.notes,
    }));
    renderItems(items);
    const values = {
      name: event.name,
      event_type: event.type,
      venue: event.venue,
      event_date: event.date,
      event_start_time: event.event_start_time,
      event_end_time: event.event_end_time,
      chief_guest: event.chief_guest,
      chief_guest_title: event.chief_guest_title,
      host: event.host,
      sponsors: JSON.parse(event.sponsors_json || "[]").join(", "),
      audience: event.audience,
      tone: event.tone,
      style: event.style,
      language: event.language,
      special_instructions: event.special_instructions,
    };
    Object.entries(values).forEach(([name, value]) => {
      const field = form.elements.namedItem(name);
      if (field) field.value = value || "";
    });
  }

  function restoreDraft() {
    try {
      const draft = JSON.parse(localStorage.getItem("ava-anchoring-studio") || "null");
      if (draft?.values) {
        Object.entries(draft.values).forEach(([name, value]) => {
          const field = form.elements.namedItem(name);
          if (field) field.value = value;
        });
        if (Array.isArray(draft.items)) renderItems(draft.items);
        if (draft.eventId) localStorage.setItem("ava-event-id", String(draft.eventId));
      } else {
        renderItems([]);
      }
    } catch (error) {
      console.error("Could not restore Anchoring Studio draft", error);
      renderItems([]);
    }
    if (localStorage.getItem("ava-event-id")) {
      void loadSessionEvent(localStorage.getItem("ava-event-id"));
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void generateFullScript();
  });
  $("#add-studio-item").addEventListener("click", () => appendItem());
  itemList.addEventListener("input", (event) => {
    saveDraft();
    const title = event.target?.closest?.(".studio-item-row")?.querySelector('[data-field="title"]')?.value;
    if (title) {
      const row = event.target.closest(".studio-item-row");
      row.querySelector(".studio-item-heading strong").textContent = `${[...itemList.children].indexOf(row) + 1}. ${title}`;
    }
  });
  itemList.addEventListener("change", saveDraft);
  itemList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    const row = button?.closest(".studio-item-row");
    if (!button || !row) return;
    const items = collectItems();
    const index = items.findIndex((item) => item.clientKey === row.dataset.key);
    if (button.dataset.action === "remove") items.splice(index, 1);
    if (button.dataset.action === "up" && index > 0) [items[index - 1], items[index]] = [items[index], items[index - 1]];
    if (button.dataset.action === "down" && index < items.length - 1) [items[index + 1], items[index]] = [items[index], items[index + 1]];
    renderItems(items);
    saveDraft();
  });
  form.addEventListener("input", saveDraft);
  form.addEventListener("change", saveDraft);
  $("#save-event-details").addEventListener("click", () => { void saveEvent(); });
  $("#studio-new-event").addEventListener("click", () => {
    if (state.requestPending) {
      setError($("#studio-validation-error"), "Wait for the current save or script generation to finish before starting a new event.");
      return;
    }
    if (!window.confirm(
      "Start a new event? This clears the current Studio form, running order, and script from this draft. Any event already saved will remain available in Live Event Control.",
    )) return;
    window.clearTimeout(state.saveTimer);
    state.draftGeneration += 1;
    if (state.speechRecorder?.state === "recording") {
      state.discardSpeechSample = true;
      state.speechRecorder.stop();
    }
    releaseSpeechStream();
    state.speechRecorder = null;
    state.speechChunks = [];
    state.event = null;
    state.script = [];
    state.retryTargets = null;
    form.reset();
    form.querySelectorAll("[aria-invalid]").forEach((field) => field.removeAttribute("aria-invalid"));
    renderItems([]);
    renderScript();
    $("#undo-script").disabled = true;
    $("#retry-script").hidden = true;
    $("#script-loading").hidden = true;
    $("#speech-test-transcript").value = "";
    $("#speech-commentary-script").value = "";
    $("#speech-key-points").replaceChildren();
    $("#speech-commentary-result").hidden = true;
    setError($("#studio-validation-error"));
    setError($("#script-error"));
    setError($("#speech-test-error"));
    setSpeechStatus("Microphone is off");
    $("#speech-record-start").disabled = false;
    $("#speech-record-stop").disabled = true;
    localStorage.removeItem("ava-event-id");
    saveDraft();
    $("#studio-save-status").textContent = "New event draft";
    form.elements.namedItem("name").focus();
  });
  $("#speech-record-start").addEventListener("click", () => {
    void startSpeechTestRecording();
  });
  $("#speech-record-stop").addEventListener("click", stopSpeechTestRecording);
  $("#speech-analyze").addEventListener("click", () => {
    void analyzeSpeechSample();
  });
  $("#speech-commentary-speak").addEventListener("click", () => {
    const text = $("#speech-commentary-script").value.trim();
    if (!text) {
      setError($("#speech-test-error"), "Generate a commentary script before playing it.");
      return;
    }
    setError($("#speech-test-error"));
    speak(text);
  });
  $("#retry-script").addEventListener("click", () => {
    const targets = state.retryTargets || fullScriptTargets();
    void regenerateTargets(targets);
  });
  $("#undo-script").addEventListener("click", async () => {
    if (!state.event) return;
    try {
      const restored = await api.undoScript(state.event.id);
      state.script = restored.segments;
      renderScript();
    } catch (error) {
      setError($("#script-error"), error.message);
    }
  });
  window.addEventListener("ava-live-event-updated", (event) => {
    if (state.event && event.detail?.id === state.event.id) updateEvent(event.detail);
  });

  function setSession(sessionId) {
    state.sessionId = sessionId;
    if (sessionId) void loadSessionEvent(localStorage.getItem("ava-event-id"));
  }
  function resetSession() {
    stopSpeechTestRecording();
    releaseSpeechStream();
    state.event = null;
    state.script = [];
    form.reset();
    renderItems([]);
    renderScript();
    localStorage.removeItem("ava-event-id");
    localStorage.removeItem("ava-anchoring-studio");
  }

  window.addEventListener("beforeunload", () => {
    stopSpeechTestRecording();
    releaseSpeechStream();
  });

  window.AVAAnchoringStudio = Object.freeze({ setSession, loadSessionEvent, resetSession });
  initializeVoices();
  restoreDraft();
  window.setInterval(async () => {
    await refreshLiveEvent();
    const event = state.event;
    if (!event || event.status === "ended" || state.autoCloseBusy || !event.event_end_time) return;
    const end = new Date(`${event.date || new Date().toISOString().slice(0, 10)}T${event.event_end_time}:00`);
    if (Number.isFinite(end.getTime()) && Date.now() >= end.getTime()) {
      state.autoCloseBusy = true;
      try {
        await closeEvent(true);
      } finally {
        state.autoCloseBusy = false;
      }
    }
  }, 30_000);
})();
