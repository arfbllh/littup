import { fetchEventSource } from '@microsoft/fetch-event-source';
import type { SSEEvent } from './types';

const RECONNECT_TIMEOUT_MS = 30_000;
const POLL_INTERVAL_MS = 5_000;

export function subscribeToDocumentEvents(
  documentId: string,
  onEvent: (event: SSEEvent) => void,
  onClose: () => void,
): () => void {
  const storageKey = `sse:${documentId}`;
  let abortController = new AbortController();
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let disconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let lastSuccessTime = Date.now();
  let isClosed = false;

  function getLastEventId(): string | null {
    try {
      return sessionStorage.getItem(storageKey);
    } catch {
      return null;
    }
  }

  function setLastEventId(id: string): void {
    try {
      sessionStorage.setItem(storageKey, id);
    } catch {
      // ignore storage errors
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      if (isClosed) return;
      try {
        const res = await fetch(`/api/documents/${documentId}`);
        if (res.ok) {
          const data = await res.json();
          onEvent({ event_type: 'poll_update', seq: -1, data });
        }
      } catch {
        // ignore poll errors
      }
    }, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function scheduleDisconnectFallback() {
    if (disconnectTimer) clearTimeout(disconnectTimer);
    disconnectTimer = setTimeout(() => {
      if (isClosed) return;
      const elapsed = Date.now() - lastSuccessTime;
      if (elapsed >= RECONNECT_TIMEOUT_MS) {
        startPolling();
      }
    }, RECONNECT_TIMEOUT_MS);
  }

  const lastEventId = getLastEventId();
  const headers: Record<string, string> = {};
  if (lastEventId) {
    headers['Last-Event-ID'] = lastEventId;
  }

  fetchEventSource(`/api/documents/${documentId}/events`, {
    headers,
    signal: abortController.signal,
    onopen: async (response) => {
      if (response.ok) {
        stopPolling();
        lastSuccessTime = Date.now();
      }
    },
    onmessage: (msg) => {
      lastSuccessTime = Date.now();
      if (disconnectTimer) {
        clearTimeout(disconnectTimer);
        disconnectTimer = null;
      }
      stopPolling();

      if (msg.id) {
        setLastEventId(msg.id);
      }

      let data: Record<string, unknown> = {};
      try {
        data = JSON.parse(msg.data || '{}') as Record<string, unknown>;
      } catch {
        data = { raw: msg.data };
      }

      onEvent({
        event_type: msg.event || 'message',
        seq: msg.id ? parseInt(msg.id, 10) : -1,
        data,
      });
    },
    onerror: () => {
      scheduleDisconnectFallback();
    },
    onclose: () => {
      if (!isClosed) {
        scheduleDisconnectFallback();
        onClose();
      }
    },
  }).catch(() => {
    if (!isClosed) {
      scheduleDisconnectFallback();
    }
  });

  return () => {
    isClosed = true;
    abortController.abort();
    stopPolling();
    if (disconnectTimer) clearTimeout(disconnectTimer);
  };
}
