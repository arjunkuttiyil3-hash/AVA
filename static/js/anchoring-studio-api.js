(() => {
  "use strict";

  async function request(path, options = {}) {
    let response;
    const { timeoutMs = 30_000, ...fetchOptions } = options;
    const isFormData = fetchOptions.body instanceof FormData;
    try {
      response = await fetch(path, {
        ...fetchOptions,
        signal: fetchOptions.signal || AbortSignal.timeout(timeoutMs),
        headers: {
          ...(fetchOptions.body && !isFormData ? { "Content-Type": "application/json" } : {}),
          ...fetchOptions.headers,
        },
      });
    } catch (error) {
      if (error.name === "TimeoutError") {
        throw new Error("The request timed out. Check Gemini availability and retry.");
      }
      throw new Error("Network error. Check your connection and retry.");
    }
    let result;
    try {
      result = await response.json();
    } catch {
      throw new Error("AVA returned an unreadable response. Please retry.");
    }
    if (!response.ok) {
      const message = typeof result.detail === "string"
        ? result.detail
        : typeof result.message === "string"
          ? result.message
        : `Request failed (${response.status}).`;
      throw new Error(message);
    }
    return result;
  }

  const json = (method, body) => ({ method, body: JSON.stringify(body) });
  const api = {
    createEvent: (data) => request("/api/events", json("POST", data)),
    getEvent: (id) => request(`/api/live-events/${id}`),
    getSessionEvent: (sessionId) => request(`/api/live-events/session/${encodeURIComponent(sessionId)}`),
    patchEvent: (id, data) => request(`/api/live-events/${id}`, json("PATCH", data)),
    start: (id) => request(`/api/live-events/${id}/start`, json("POST", {})),
    pause: (id) => request(`/api/live-events/${id}/pause`, json("POST", {})),
    end: (id) => request(`/api/live-events/${id}/end`, json("POST", {})),
    next: (id) => request(`/api/live-events/${id}/next`, json("POST", {})),
    previous: (id) => request(`/api/live-events/${id}/previous`, json("POST", {})),
    end: (id) => request(`/api/live-events/${id}/end`, json("POST", {})),
    addItem: (eventId, item, position) => request("/api/event/items", json("POST", {
      event_id: eventId, ...item, position,
    })),
    patchItem: (eventId, itemId, changes) => request(`/api/event/items/${itemId}`, json("PATCH", {
      event_id: eventId, ...changes,
    })),
    deleteItem: (eventId, itemId) => request(`/api/event/items/${itemId}?event_id=${eventId}`, {
      method: "DELETE",
    }),
    skip: (eventId, itemId) => request(`/api/live-events/${eventId}/items/${itemId}/skip`, json("POST", {})),
    restore: (eventId, itemId, position) => request(`/api/live-events/${eventId}/items/${itemId}/restore`, json("POST", {
      position,
    })),
    reorder: (eventId, ids) => request("/api/event/reorder", json("POST", {
      event_id: eventId, item_ids: ids,
    })),
    generate: (eventId, targets = null, replaceEditedIds = []) => request(
      "/api/anchoring/scripts/generate",
      {
        ...json("POST", {
          event_id: eventId,
          targets,
          replace_edited_ids: replaceEditedIds,
        }),
        timeoutMs: 90_000,
      },
    ),
    transcribeSpeech: (audio, language, eventId = 0) => {
      const body = new FormData();
      const extension = audio.type.includes("mp4") ? "mp4"
        : audio.type.includes("ogg") ? "ogg"
          : "webm";
      body.append("audio", audio, `anchoring-speech.${extension}`);
      body.append("language", language);
      if (eventId) body.append("event_id", String(eventId));
      return request("/api/transcribe", { method: "POST", body, timeoutMs: 90_000 });
    },
    analyzeSpeech: (eventId, transcript, language) => request(
      "/api/anchoring/speech-commentary",
      {
        ...json("POST", { event_id: eventId, transcript, language }),
        timeoutMs: 90_000,
      },
    ),
    getScript: (eventId) => request(`/api/anchoring/scripts/${eventId}`),
    saveScript: (eventId, segments) => request(`/api/anchoring/scripts/${eventId}`, json("PUT", { segments })),
    undoScript: (eventId) => request(`/api/anchoring/scripts/${eventId}/undo`, json("POST", {})),
    undoEvent: (eventId) => request(`/api/live-events/${eventId}/undo`, json("POST", {})),
    close: (eventId, language, replaceEdited = false) => request(
      `/api/live-events/${eventId}/close`,
      {
        ...json("POST", { language, replace_edited: replaceEdited }),
        timeoutMs: 90_000,
      },
    ),
  };

  window.AVAAnchoringStudioAPI = Object.freeze(api);
})();
