'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, RotateCw, Trash2, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import {
  acceptReextractSession,
  getReextractSession,
  rejectReextractSession,
  startReextractSession,
} from '@/lib/api';
import type {
  OcrProvider,
  ReextractPageView,
  ReextractSessionView,
} from '@/lib/types';

interface ReextractDialogProps {
  open: boolean;
  documentId: string;
  pageCount: number;
  isPdf: boolean;
  onClose: () => void;
  /** Called after a successful accept so the parent can refresh the doc. */
  onAccepted: () => void;
}

type Stage = 'select' | 'running' | 'review' | 'applying' | 'discarding';

export function ReextractDialog({
  open,
  documentId,
  pageCount,
  isPdf,
  onClose,
  onAccepted,
}: ReextractDialogProps) {
  const [stage, setStage] = useState<Stage>('select');
  const [provider, setProvider] = useState<OcrProvider>('paddleocr');
  const [pagesInput, setPagesInput] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [session, setSession] = useState<ReextractSessionView | null>(null);
  const [keepFlags, setKeepFlags] = useState<Record<number, boolean>>({});
  const pollRef = useRef<number | null>(null);

  // On open: try to resume a backgrounded session from localStorage; otherwise
  // reset to a fresh 'select' stage. Closing the dialog does NOT clear state —
  // the worker keeps running and the operator can reopen to keep reviewing.
  useEffect(() => {
    if (!open) {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    setError(null);
    const storedId = readStoredSession(documentId);
    if (!storedId) {
      setStage('select');
      setProvider('paddleocr');
      setPagesInput('');
      setSession(null);
      setKeepFlags({});
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const s = await getReextractSession(documentId, storedId);
        if (cancelled) return;
        if (s.status === 'pending' || s.status === 'running') {
          setSession(s);
          setStage('running');
        } else if (s.status === 'ready') {
          const flags: Record<number, boolean> = {};
          for (const p of s.pages) if (p.status === 'ok') flags[p.page_number] = true;
          setKeepFlags(flags);
          setSession(s);
          setStage('review');
        } else {
          // accepted / rejected / failed → done; clear and start fresh.
          clearStoredSession(documentId);
          setStage('select');
          setSession(null);
          setKeepFlags({});
        }
      } catch {
        // Session no longer exists on the server.
        clearStoredSession(documentId);
        if (!cancelled) {
          setStage('select');
          setSession(null);
          setKeepFlags({});
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, documentId]);

  // Poll while OCR is running in the worker. Stops when the dialog is closed.
  useEffect(() => {
    if (!open || stage !== 'running' || !session) return;
    let cancelled = false;
    pollRef.current = window.setInterval(async () => {
      try {
        const fresh = await getReextractSession(documentId, session.session_id);
        if (cancelled) return;
        setSession(fresh);
        if (fresh.status === 'ready') {
          // Pre-select all `ok` pages by default.
          const flags: Record<number, boolean> = {};
          for (const p of fresh.pages) {
            if (p.status === 'ok') flags[p.page_number] = true;
          }
          setKeepFlags(flags);
          setStage('review');
        } else if (fresh.status === 'failed') {
          setError(fresh.error_message ?? 'Re-extract failed');
          setStage('review');
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Polling failed');
      }
    }, 1500);
    return () => {
      cancelled = true;
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [open, stage, session, documentId]);

  if (!open) return null;

  const parsedPages = parsePageRange(pagesInput, pageCount);
  const canStart =
    stage === 'select' &&
    parsedPages.ok &&
    parsedPages.pages.length > 0 &&
    !!provider;

  async function handleStart() {
    if (!canStart) return;
    setError(null);
    try {
      const created = await startReextractSession(
        documentId,
        parsedPages.pages,
        provider,
      );
      writeStoredSession(documentId, created.session_id);
      setSession(created);
      setStage('running');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start session');
    }
  }

  async function handleApply() {
    if (!session) return;
    const accepted = Object.entries(keepFlags)
      .filter(([, v]) => v)
      .map(([k]) => Number(k));
    if (accepted.length === 0) {
      setError('Pick at least one page to keep, or click Discard All.');
      return;
    }
    setStage('applying');
    setError(null);
    try {
      await acceptReextractSession(documentId, session.session_id, accepted);
      clearStoredSession(documentId);
      onAccepted();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Apply failed');
      setStage('review');
    }
  }

  async function handleDiscard() {
    if (!session) {
      onClose();
      return;
    }
    setStage('discarding');
    setError(null);
    try {
      await rejectReextractSession(documentId, session.session_id);
      clearStoredSession(documentId);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Discard failed');
      setStage('review');
    }
  }

  const busy =
    stage === 'running' || stage === 'applying' || stage === 'discarding';
  // Closing during 'running' is fine — the worker keeps going and the dialog
  // resumes from localStorage on next open. Only block close during the brief
  // accept/reject API calls.
  const closeBlocked = stage === 'applying' || stage === 'discarding';

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="reextract-dialog-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !closeBlocked) onClose();
      }}
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(35, 27, 19, 0.45)',
        backdropFilter: 'blur(2px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
        padding: '16px',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          backgroundColor: '#fff',
          borderRadius: 'var(--radius-card)',
          border: '1px solid var(--paper-300)',
          boxShadow: '0 12px 40px rgba(35, 27, 19, 0.18)',
          width: '100%',
          maxWidth: stage === 'select' ? '460px' : '900px',
          maxHeight: '90vh',
          padding: '20px 22px',
          display: 'flex',
          flexDirection: 'column',
          gap: '14px',
          overflow: 'hidden',
        }}
      >
        <header style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <RotateCw size={18} color="var(--ochre-500)" />
          <h2
            id="reextract-dialog-title"
            style={{
              margin: 0,
              fontFamily: 'var(--font-serif, var(--font-sans))',
              fontSize: '16px',
              fontWeight: 600,
              color: 'var(--paper-700)',
            }}
          >
            Re-extract pages
          </h2>
          <div style={{ flex: 1 }} />
          <button
            type="button"
            onClick={() => !closeBlocked && onClose()}
            disabled={closeBlocked}
            aria-label={stage === 'running' ? 'Close (keeps running in background)' : 'Close'}
            title={stage === 'running' ? 'Close — re-extract keeps running in the background' : 'Close'}
            style={{
              background: 'none',
              border: 'none',
              cursor: closeBlocked ? 'wait' : 'pointer',
              color: 'var(--paper-400)',
              padding: 4,
              display: 'inline-flex',
            }}
          >
            <X size={16} />
          </button>
        </header>

        {stage === 'select' && (
          <SelectStage
            pageCount={pageCount}
            isPdf={isPdf}
            provider={provider}
            setProvider={setProvider}
            pagesInput={pagesInput}
            setPagesInput={setPagesInput}
            parsed={parsedPages}
          />
        )}

        {stage === 'running' && session && (
          <RunningStage session={session} />
        )}

        {(stage === 'review' || stage === 'applying' || stage === 'discarding') &&
          session && (
            <ReviewStage
              session={session}
              keepFlags={keepFlags}
              setKeepFlags={setKeepFlags}
              busy={busy}
            />
          )}

        {error && (
          <div
            role="alert"
            style={{
              padding: '8px 10px',
              border: '1px solid var(--cite-unsupported)',
              backgroundColor: 'rgba(213, 76, 56, 0.08)',
              borderRadius: 4,
              fontSize: '12px',
              color: 'var(--paper-700)',
              fontFamily: 'var(--font-mono)',
            }}
          >
            {error}
          </div>
        )}

        <footer style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          {stage === 'select' && (
            <>
              <Button variant="ghost" size="sm" onClick={onClose}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                icon={RotateCw}
                onClick={handleStart}
                disabled={!canStart}
              >
                Start re-extract
              </Button>
            </>
          )}
          {stage === 'running' && (
            <Button variant="ghost" size="sm" onClick={onClose}>
              Run in background
            </Button>
          )}
          {(stage === 'review' || stage === 'applying' || stage === 'discarding') && (
            <>
              <Button
                variant="ghost"
                size="sm"
                icon={Trash2}
                onClick={handleDiscard}
                disabled={busy}
              >
                {stage === 'discarding' ? 'Discarding…' : 'Discard all'}
              </Button>
              <Button
                variant="primary"
                size="sm"
                icon={Check}
                onClick={handleApply}
                disabled={busy}
              >
                {stage === 'applying' ? 'Applying…' : 'Apply selected'}
              </Button>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}

// ─── Stages ──────────────────────────────────────────────────────────────────

function SelectStage({
  pageCount,
  isPdf,
  provider,
  setProvider,
  pagesInput,
  setPagesInput,
  parsed,
}: {
  pageCount: number;
  isPdf: boolean;
  provider: OcrProvider;
  setProvider: (p: OcrProvider) => void;
  pagesInput: string;
  setPagesInput: (v: string) => void;
  parsed: ParsedPages;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <p style={{ margin: 0, fontSize: 13, color: 'var(--paper-600)', lineHeight: 1.55 }}>
        Pick which pages to re-OCR with a different engine, then preview the new
        text before keeping it. Layout, chunking, and embedding will run once at
        the end — no matter how many pages you pick.
      </p>

      <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <span style={{ fontSize: 11, color: 'var(--paper-500)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>
          Pages (1–{pageCount})
        </span>
        <input
          type="text"
          value={pagesInput}
          onChange={(e) => setPagesInput(e.target.value)}
          placeholder="e.g. 2,4,5 or 1-3,7"
          aria-label="Pages to re-extract"
          style={{
            padding: '8px 10px',
            border: '1px solid var(--paper-300)',
            borderRadius: 4,
            fontSize: 13,
            fontFamily: 'var(--font-mono)',
            outline: 'none',
          }}
        />
        <span
          style={{
            fontSize: 11,
            fontFamily: 'var(--font-mono)',
            color: parsed.ok ? 'var(--paper-500)' : 'var(--cite-unsupported)',
          }}
        >
          {parsed.ok
            ? parsed.pages.length === 0
              ? '(no pages selected)'
              : `${parsed.pages.length} page${parsed.pages.length === 1 ? '' : 's'}: ${parsed.pages.join(', ')}`
            : parsed.error}
        </span>
      </label>

      <fieldset
        style={{
          border: '1px solid var(--paper-300)',
          borderRadius: 4,
          padding: '8px 12px',
          margin: 0,
        }}
      >
        <legend style={{ fontSize: 11, color: 'var(--paper-500)', textTransform: 'uppercase', letterSpacing: '0.08em', padding: '0 4px' }}>
          Engine
        </legend>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, padding: '4px 0', cursor: isPdf ? 'pointer' : 'not-allowed', opacity: isPdf ? 1 : 0.5 }}>
          <input
            type="radio"
            name="provider"
            value="pdfplumber"
            checked={provider === 'pdfplumber'}
            disabled={!isPdf}
            onChange={() => setProvider('pdfplumber')}
          />
          <span>
            <strong>pdfplumber</strong> — read embedded text layer (PDF only, fast, no OCR)
          </span>
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, padding: '4px 0', cursor: 'pointer' }}>
          <input
            type="radio"
            name="provider"
            value="paddleocr"
            checked={provider === 'paddleocr'}
            onChange={() => setProvider('paddleocr')}
          />
          <span>
            <strong>paddleocr</strong> — render page and OCR pixels (handles scans / bad text layers)
          </span>
        </label>
      </fieldset>
    </div>
  );
}

function RunningStage({ session }: { session: ReextractSessionView }) {
  const total = session.page_numbers.length;
  const done = session.pages.length;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: '24px 8px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <Spinner size={16} />
        <span style={{ fontSize: 13, color: 'var(--paper-600)' }}>
          Running {session.provider} on {total} page{total === 1 ? '' : 's'}…
          {done > 0 ? ` (${done}/${total} complete)` : ''}
        </span>
      </div>
      <div
        style={{
          height: 4,
          backgroundColor: 'var(--paper-200)',
          borderRadius: 2,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${total > 0 ? (done / total) * 100 : 0}%`,
            height: '100%',
            backgroundColor: 'var(--ochre-500)',
            transition: 'width 200ms',
          }}
        />
      </div>
      <p style={{ margin: 0, fontSize: 11, color: 'var(--paper-400)', fontFamily: 'var(--font-mono)' }}>
        Spans are written to staging — your document is untouched until you click <strong>Apply selected</strong>.
        You can close this dialog and the worker will keep running; reopen any time to review.
      </p>
    </div>
  );
}

function ReviewStage({
  session,
  keepFlags,
  setKeepFlags,
  busy,
}: {
  session: ReextractSessionView;
  keepFlags: Record<number, boolean>;
  setKeepFlags: (v: Record<number, boolean>) => void;
  busy: boolean;
}) {
  const allOn = useMemo(
    () => session.pages.filter((p) => p.status === 'ok').every((p) => keepFlags[p.page_number]),
    [session.pages, keepFlags],
  );
  function toggleAll() {
    const next: Record<number, boolean> = {};
    for (const p of session.pages) {
      if (p.status === 'ok') next[p.page_number] = !allOn;
    }
    setKeepFlags(next);
  }
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        flex: 1,
        minHeight: 0,
        overflow: 'hidden',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 12, color: 'var(--paper-500)', flex: 1 }}>
          Tick the pages whose new extraction looks better. Untick to keep the
          original. Failed pages are read-only.
        </span>
        <button
          type="button"
          onClick={toggleAll}
          disabled={busy}
          style={{
            background: 'none',
            border: 'none',
            cursor: busy ? 'wait' : 'pointer',
            color: 'var(--ochre-500)',
            fontSize: 12,
            fontFamily: 'var(--font-mono)',
          }}
        >
          {allOn ? 'untick all' : 'tick all'}
        </button>
      </div>
      <div
        style={{
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          border: '1px solid var(--paper-200)',
          borderRadius: 4,
        }}
      >
        {session.pages.map((p) => (
          <PageDiff
            key={p.page_number}
            page={p}
            checked={!!keepFlags[p.page_number] && p.status === 'ok'}
            disabled={busy || p.status !== 'ok'}
            onToggle={() =>
              setKeepFlags({ ...keepFlags, [p.page_number]: !keepFlags[p.page_number] })
            }
          />
        ))}
        {session.pages.length === 0 && (
          <div style={{ padding: 20, color: 'var(--paper-400)', fontSize: 12, textAlign: 'center' }}>
            (no pages — session may have failed)
          </div>
        )}
      </div>
    </div>
  );
}

function PageDiff({
  page,
  checked,
  disabled,
  onToggle,
}: {
  page: ReextractPageView;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  const failed = page.status === 'failed';
  return (
    <div
      style={{
        borderBottom: '1px solid var(--paper-200)',
        padding: '12px 14px',
        backgroundColor: failed ? 'rgba(213, 76, 56, 0.04)' : '#fff',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={onToggle}
          aria-label={`Keep new extraction for page ${page.page_number}`}
        />
        <strong style={{ fontSize: 13, color: 'var(--paper-700)' }}>
          Page {page.page_number}
        </strong>
        <span
          style={{
            fontSize: 10,
            fontFamily: 'var(--font-mono)',
            padding: '1px 6px',
            borderRadius: 3,
            backgroundColor: failed ? 'var(--cite-unsupported)' : 'var(--paper-200)',
            color: failed ? '#fff' : 'var(--paper-500)',
          }}
        >
          {failed ? 'failed' : page.provider}
        </span>
        {failed && page.error_message && (
          <span style={{ fontSize: 11, color: 'var(--cite-unsupported)' }}>
            {page.error_message}
          </span>
        )}
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: 10,
          fontSize: 12,
          fontFamily: 'var(--font-mono)',
          lineHeight: 1.5,
        }}
      >
        <DiffPane label="before" text={page.old_text} />
        <DiffPane label={failed ? '(no new text)' : `after — ${page.provider}`} text={page.new_text} highlight={!failed} />
      </div>
    </div>
  );
}

function DiffPane({
  label,
  text,
  highlight = false,
}: {
  label: string;
  text: string;
  highlight?: boolean;
}) {
  return (
    <div
      style={{
        border: '1px solid var(--paper-200)',
        borderRadius: 4,
        backgroundColor: highlight ? 'rgba(193, 144, 41, 0.06)' : 'var(--paper-100)',
        padding: 8,
        maxHeight: 180,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      <div
        style={{
          fontSize: 10,
          color: 'var(--paper-400)',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          marginBottom: 4,
        }}
      >
        {label}
      </div>
      <div>{text || <em style={{ color: 'var(--paper-400)' }}>(empty)</em>}</div>
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const SESSION_STORAGE_PREFIX = 'reextract:session:';

function readStoredSession(documentId: string): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(SESSION_STORAGE_PREFIX + documentId);
  } catch {
    return null;
  }
}

function writeStoredSession(documentId: string, sessionId: string): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(SESSION_STORAGE_PREFIX + documentId, sessionId);
  } catch {
    /* quota / private mode — silently ignore */
  }
  notifySessionChange();
}

function clearStoredSession(documentId: string): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(SESSION_STORAGE_PREFIX + documentId);
  } catch {
    /* ignore */
  }
  notifySessionChange();
}

const SESSION_CHANGE_EVENT = 'reextract:session-change';

function notifySessionChange(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new Event(SESSION_CHANGE_EVENT));
}

/** Background-aware view of the active re-extract session for a document.
 *  Polls the server while a stored session_id exists; mirrors the dialog so
 *  the parent button can show "running" / "ready to review" indicators. */
export function useReextractSessionStatus(documentId: string): {
  active: boolean;
  status: ReextractSessionView['status'] | null;
  done: number;
  total: number;
} {
  const [sessionId, setSessionId] = useState<string | null>(() =>
    readStoredSession(documentId),
  );
  const [view, setView] = useState<ReextractSessionView | null>(null);

  useEffect(() => {
    function refresh() {
      setSessionId(readStoredSession(documentId));
    }
    refresh();
    window.addEventListener(SESSION_CHANGE_EVENT, refresh);
    window.addEventListener('storage', refresh);
    return () => {
      window.removeEventListener(SESSION_CHANGE_EVENT, refresh);
      window.removeEventListener('storage', refresh);
    };
  }, [documentId]);

  useEffect(() => {
    if (!sessionId) {
      setView(null);
      return;
    }
    let cancelled = false;
    let timer: number | null = null;
    async function tick() {
      try {
        const fresh = await getReextractSession(documentId, sessionId!);
        if (cancelled) return;
        setView(fresh);
        if (
          fresh.status === 'accepted' ||
          fresh.status === 'rejected' ||
          fresh.status === 'failed'
        ) {
          clearStoredSession(documentId);
          return;
        }
      } catch {
        if (cancelled) return;
        clearStoredSession(documentId);
        return;
      }
      timer = window.setTimeout(tick, 2000);
    }
    tick();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [sessionId, documentId]);

  if (!sessionId || !view) {
    return { active: false, status: null, done: 0, total: 0 };
  }
  return {
    active: true,
    status: view.status,
    done: view.pages.length,
    total: view.page_numbers.length,
  };
}

interface ParsedPages {
  ok: boolean;
  pages: number[];
  error: string;
}

function parsePageRange(input: string, max: number): ParsedPages {
  const trimmed = input.trim();
  if (!trimmed) return { ok: true, pages: [], error: '' };
  const out = new Set<number>();
  for (const piece of trimmed.split(',').map((s) => s.trim()).filter(Boolean)) {
    const range = piece.match(/^(\d+)\s*-\s*(\d+)$/);
    if (range) {
      const lo = Number(range[1]);
      const hi = Number(range[2]);
      if (lo < 1 || hi > max || lo > hi) {
        return { ok: false, pages: [], error: `bad range: ${piece} (1–${max} only)` };
      }
      for (let i = lo; i <= hi; i++) out.add(i);
      continue;
    }
    const n = Number(piece);
    if (!Number.isInteger(n) || n < 1 || n > max) {
      return { ok: false, pages: [], error: `bad page: ${piece} (1–${max} only)` };
    }
    out.add(n);
  }
  return { ok: true, pages: Array.from(out).sort((a, b) => a - b), error: '' };
}
