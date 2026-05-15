'use client';

import { useEffect, useRef, useState } from 'react';
import { subscribeToDocumentEvents } from '@/lib/sse';
import type { SSEEvent } from '@/lib/types';

const RING_BUFFER_SIZE = 50;
const STORM_WINDOW_MS = 60_000;
const STORM_LIMIT = 3;

export interface DocumentEventEntry extends SSEEvent {
  ts: number;
}

export interface DocumentEventsState {
  events: DocumentEventEntry[];
  status: string | null;
  livenessError: string | null;
}

/**
 * Subscribe to a document's SSE stream and keep a ring buffer of the last
 * `RING_BUFFER_SIZE` events. Detects reconnect storms (>STORM_LIMIT
 * disconnects in STORM_WINDOW_MS) and surfaces a `livenessError` so the
 * UI can toast and stop fighting a permanently broken connection.
 */
export function useDocumentEvents(
  documentId: string | null,
  onStatusChanged?: (status: string) => void,
): DocumentEventsState {
  const [events, setEvents] = useState<DocumentEventEntry[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [livenessError, setLivenessError] = useState<string | null>(null);
  const stormTimestampsRef = useRef<number[]>([]);

  useEffect(() => {
    if (!documentId) return;
    setEvents([]);
    setStatus(null);
    setLivenessError(null);
    stormTimestampsRef.current = [];

    let stopped = false;

    const cleanup = subscribeToDocumentEvents(
      documentId,
      (event) => {
        if (stopped) return;
        const entry: DocumentEventEntry = { ...event, ts: Date.now() };
        setEvents((prev) => {
          const next = [...prev, entry];
          if (next.length > RING_BUFFER_SIZE) {
            next.splice(0, next.length - RING_BUFFER_SIZE);
          }
          return next;
        });
        const nextStatus =
          (event.data?.to as string | undefined) ||
          (event.data?.status as string | undefined);
        if (nextStatus) {
          setStatus(nextStatus);
          onStatusChanged?.(nextStatus);
        }
      },
      () => {
        if (stopped) return;
        const now = Date.now();
        const recent = stormTimestampsRef.current.filter(
          (t) => now - t < STORM_WINDOW_MS,
        );
        recent.push(now);
        stormTimestampsRef.current = recent;
        if (recent.length > STORM_LIMIT) {
          setLivenessError(
            'Live updates unavailable; falling back to polling.',
          );
        }
      },
    );

    return () => {
      stopped = true;
      cleanup();
    };
  }, [documentId, onStatusChanged]);

  return { events, status, livenessError };
}
