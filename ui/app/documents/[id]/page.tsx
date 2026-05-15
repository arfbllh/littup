'use client';

import React, { useState, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import useSWR from 'swr';
import { ArrowLeft, Check, ChevronDown, ChevronLeft, ChevronRight, Copy, Download, Pencil, RotateCw, Trash2 } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { StatusPill } from '@/components/ui/status-pill';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { PageWithBboxes } from '@/components/PageWithBboxes';
import { getDocument, getBlocks, retryDocument, deleteDocument, patchBlockText } from '@/lib/api';
import { useDocumentEvents } from '@/lib/hooks/useDocumentEvents';
import type { Block } from '@/lib/types';

const STATE_MACHINE: string[] = [
  'uploaded',
  'ocr_pending',
  'ocr_running',
  'ocr_done',
  'layout_running',
  'layout_done',
  'chunking_running',
  'chunking_done',
  'embedding_running',
  'ready',
];

function TimelineStep({
  label,
  active,
  done,
}: {
  label: string;
  active: boolean;
  done: boolean;
}) {
  const color = done
    ? 'var(--cite-supported)'
    : active
    ? 'var(--ochre-500)'
    : 'var(--paper-300)';

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        fontSize: '11px',
        fontFamily: 'var(--font-mono)',
      }}
    >
      {active ? (
        <span
          style={{
            width: '10px',
            height: '10px',
            border: `1.5px solid ${color}`,
            borderTopColor: 'transparent',
            borderRadius: '50%',
            animation: 'lit-spin 0.7s linear infinite',
            display: 'inline-block',
            flexShrink: 0,
          }}
          aria-label="in progress"
        />
      ) : (
        <span
          style={{
            width: '8px',
            height: '8px',
            borderRadius: '50%',
            flexShrink: 0,
            backgroundColor: color,
          }}
        />
      )}
      <span style={{ color, fontWeight: active ? 600 : 400 }}>
        {label.replace(/_/g, ' ')}
      </span>
    </div>
  );
}

export default function DocumentDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const [currentPage, setCurrentPage] = useState(1);
  const [hoveredBlock, setHoveredBlock] = useState<Block | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [showEventLog, setShowEventLog] = useState(false);
  const [showReadingView, setShowReadingView] = useState(true);
  const [editingBlockId, setEditingBlockId] = useState<string | null>(null);
  const [editingBlockText, setEditingBlockText] = useState('');
  const [savingBlock, setSavingBlock] = useState(false);
  const [copiedBlockId, setCopiedBlockId] = useState<string | null>(null);

  function copyBlockText(blockId: string, blockText: string) {
    try {
      void navigator.clipboard.writeText(blockText);
      setCopiedBlockId(blockId);
      window.setTimeout(() => {
        setCopiedBlockId((current) => (current === blockId ? null : current));
      }, 1500);
    } catch {
      // pre-clipboard browsers: ignore silently
    }
  }

  const { data: doc, mutate: mutateDoc } = useSWR(
    id ? `doc:${id}` : null,
    () => getDocument(id),
    {
      // SWR runs as a slow safety-net behind SSE (Inv #10). When SSE is healthy
      // the storm-guard in useDocumentEvents shuts up; if SSE truly drops, the
      // hook starts its own 5 s polling. This 10 s tick is just a final
      // belt-and-braces for slow proxies that buffer the stream.
      refreshInterval: (latest) =>
        latest && (latest.status === 'ready' || latest.status === 'failed') ? 0 : 10_000,
    }
  );

  const { data: blocksData, mutate: mutateBlocks } = useSWR(
    doc?.status === 'ready' ? `blocks:${id}` : null,
    () => getBlocks(id)
  );

  async function saveBlockEdit() {
    if (!id || !editingBlockId) return;
    setSavingBlock(true);
    try {
      const res = await patchBlockText(id, editingBlockId, editingBlockText);
      if (!res.no_change) {
        await mutateBlocks();
      }
      setEditingBlockId(null);
      setEditingBlockText('');
    } catch (err) {
      // eslint-disable-next-line no-alert
      alert(`Save failed: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setSavingBlock(false);
    }
  }

  // Mutate the SWR cache the moment we see a `status_changed` event so the
  // timeline animates without waiting for the next GET (Inv #10 friendly:
  // SWR still polls, but the UI is no longer behind it).
  const onStatusChanged = useCallback(
    (next: string) => {
      mutateDoc(
        (prev) =>
          prev ? { ...prev, status: next, updated_at: new Date().toISOString() } : prev,
        { revalidate: false },
      );
    },
    [mutateDoc],
  );

  const { events, status: liveStatus, livenessError } = useDocumentEvents(
    id ?? null,
    onStatusChanged,
  );

  const status = liveStatus ?? doc?.status ?? 'uploaded';
  const pageCount = doc?.page_count ?? 1;
  const blocks = blocksData?.blocks ?? [];
  const currentPageBlocks = blocks.filter(
    (b) => currentPage >= b.page_start && currentPage <= b.page_end,
  );
  // WS-A.4: a `ready` doc with zero blocks slid past the empty-OCR gate before
  // we shipped the guard. Surface a retry path so the operator isn't stuck.
  const stuckEmpty =
    status === 'ready' && doc?.status === 'ready' && blocks.length === 0;

  function requestDelete() {
    if (!doc || deleting) return;
    setConfirmingDelete(true);
  }

  async function confirmDelete() {
    if (!doc) return;
    setDeleting(true);
    try {
      await deleteDocument(id);
      router.push('/documents');
    } catch (err) {
      // eslint-disable-next-line no-alert
      alert(`Delete failed: ${err instanceof Error ? err.message : 'unknown error'}`);
      setDeleting(false);
      setConfirmingDelete(false);
    }
  }

  async function handleRetry(opts: { force?: boolean } = {}) {
    if (!doc || retrying) return;
    setRetrying(true);
    try {
      const updated = await retryDocument(id, opts);
      await mutateDoc(
        (prev) =>
          prev ? { ...prev, status: updated.status } : prev,
        { revalidate: true },
      );
    } catch (err) {
      // eslint-disable-next-line no-alert
      alert(`Retry failed: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setRetrying(false);
    }
  }

  function handleExport() {
    if (!doc) return;
    const json = JSON.stringify(doc, null, 2);
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${doc.filename}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // `*_done` states (ocr_done, layout_done, chunking_done) really mean
  // "previous stage complete, waiting for the next worker to pick up." Treat
  // them as completed and advance the active marker to the next stage so the
  // spinning indicator points at the work that's actually queued — not at a
  // step labelled "done".
  const rawIdx = STATE_MACHINE.indexOf(status);
  const isIntermediateDone =
    status !== 'ready' && status !== 'failed' && status.endsWith('_done');
  const advancedIdx =
    isIntermediateDone && rawIdx >= 0 && rawIdx < STATE_MACHINE.length - 1
      ? rawIdx + 1
      : rawIdx;
  // `ready` is a terminal state — every step is done, nothing should spin.
  // Pushing the active index past the end makes the `currentStatusIdx === idx`
  // check fail for every row, so no row renders as active.
  const currentStatusIdx = status === 'ready' ? STATE_MACHINE.length : advancedIdx;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader
        eyebrow="Documents"
        title={doc?.filename ?? 'Loading...'}
        actions={
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            {doc && <StatusPill status={status} />}
            {(status === 'failed' || stuckEmpty) && (
              <Button
                variant="primary"
                size="sm"
                icon={RotateCw}
                onClick={() => handleRetry({ force: stuckEmpty })}
                disabled={retrying}
              >
                {retrying ? 'Retrying…' : 'Retry'}
              </Button>
            )}
            <Button variant="ghost" size="sm" icon={Download} onClick={handleExport}>
              Export JSON
            </Button>
            <Button
              variant="danger"
              size="sm"
              icon={Trash2}
              onClick={requestDelete}
              disabled={deleting}
            >
              {deleting ? 'Deleting…' : 'Delete'}
            </Button>
            <Button variant="ghost" size="sm" icon={ArrowLeft} onClick={() => router.push('/documents')}>
              Back
            </Button>
          </div>
        }
      />

      {/* Metadata strip */}
      {doc && (
        <div
          style={{
            padding: '8px 24px',
            backgroundColor: 'var(--paper-100)',
            borderBottom: '1px solid var(--paper-300)',
            display: 'flex',
            gap: '16px',
            flexWrap: 'wrap',
            alignItems: 'center',
          }}
        >
          <span className="mono-id">id:{doc.document_id.slice(0, 12)}</span>
          <span className="mono-id">sha256:{doc.sha256.slice(0, 12)}</span>
          {doc.mime_type && <span className="mono-id">{doc.mime_type}</span>}
          {doc.page_count && (
            <span className="mono-id">{doc.page_count} pages</span>
          )}
        </div>
      )}

      {livenessError && (
        <div
          role="status"
          style={{
            margin: '8px 24px 0',
            padding: '6px 12px',
            backgroundColor: 'var(--paper-100)',
            border: '1px solid var(--paper-300)',
            borderRadius: 'var(--radius-card)',
            fontSize: '12px',
            color: 'var(--paper-500)',
            fontFamily: 'var(--font-mono)',
          }}
        >
          {livenessError}
        </div>
      )}

      {stuckEmpty && (
        <div
          role="alert"
          style={{
            margin: '12px 24px 0',
            padding: '10px 14px',
            border: '1px solid var(--cite-partial, #d4a017)',
            backgroundColor: 'var(--ochre-50, #fdf5e1)',
            borderRadius: 'var(--radius-card)',
            fontSize: '13px',
            lineHeight: 1.5,
            color: 'var(--paper-700)',
          }}
        >
          <strong>This document finished processing but no text blocks were extracted.</strong>{' '}
          That usually means a scanned PDF with no usable text layer. Hit{' '}
          <em>Retry</em> to re-run OCR — if it fails the same way, the source likely has
          no recoverable text and the result is expected.
        </div>
      )}

      {/* Main split layout */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {/* Left: page viewer */}
        <div
          style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            borderRight: '1px solid var(--paper-300)',
            overflow: 'hidden',
          }}
        >
          {/* Page controls */}
          <div
            style={{
              padding: '8px 16px',
              borderBottom: '1px solid var(--paper-200)',
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              flexShrink: 0,
              backgroundColor: '#fff',
            }}
          >
            <Button
              variant="ghost"
              size="sm"
              icon={ChevronLeft}
              disabled={currentPage <= 1}
              onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
            >
              Prev
            </Button>
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: '12px',
                color: 'var(--paper-500)',
                flex: 1,
                textAlign: 'center',
              }}
            >
              Page {currentPage} / {pageCount}
            </span>
            <Button
              variant="ghost"
              size="sm"
              icon={ChevronRight}
              iconPosition="right"
              disabled={currentPage >= pageCount}
              onClick={() => setCurrentPage((p) => Math.min(pageCount, p + 1))}
            >
              Next
            </Button>
          </div>

          {/* Page image */}
          <div
            style={{
              flex: 1,
              overflow: 'auto',
              padding: '16px',
              display: 'flex',
              justifyContent: 'center',
              backgroundColor: 'var(--paper-100)',
            }}
          >
            {id && doc && (
              <PageWithBboxes
                documentId={id}
                page={currentPage}
                highlightBbox={
                  hoveredBlock &&
                  currentPage >= hoveredBlock.page_start &&
                  currentPage <= hoveredBlock.page_end
                    ? hoveredBlock.bbox
                    : null
                }
                docStatus={status}
                style={{ maxWidth: '100%', boxShadow: '0 2px 12px rgba(0,0,0,0.08)' }}
              />
            )}
            {!doc && (
              <div style={{ color: 'var(--paper-400)', fontSize: '13px', padding: '48px' }}>
                Loading document...
              </div>
            )}
          </div>
        </div>

        {/* Right: blocks + timeline */}
        <div
          style={{
            width: '400px',
            flexShrink: 0,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {/* Ingestion timeline */}
          <div
            style={{
              padding: '12px 16px',
              borderBottom: '1px solid var(--paper-300)',
              backgroundColor: '#fff',
              flexShrink: 0,
            }}
          >
            <div className="eyebrow" style={{ marginBottom: '10px' }}>
              Ingestion Timeline
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {STATE_MACHINE.map((state, idx) => (
                <TimelineStep
                  key={state}
                  label={state}
                  active={currentStatusIdx === idx}
                  done={
                    status === 'failed'
                      ? false
                      : currentStatusIdx > idx
                  }
                />
              ))}
              {status === 'failed' && (
                <div style={{ fontSize: '11px', color: 'var(--cite-unsupported)', fontFamily: 'var(--font-mono)', marginTop: '4px' }}>
                  {doc?.error_message ?? 'Processing failed'}
                </div>
              )}
            </div>

            {/* WS-B: collapsible event log over the SSE ring buffer. */}
            <div style={{ marginTop: '10px' }}>
              <button
                type="button"
                onClick={() => setShowEventLog((v) => !v)}
                aria-expanded={showEventLog}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  background: 'none',
                  border: 'none',
                  padding: 0,
                  cursor: 'pointer',
                  color: 'var(--paper-500)',
                  fontFamily: 'var(--font-mono)',
                  fontSize: '11px',
                }}
              >
                <ChevronDown
                  size={11}
                  style={{
                    transition: 'transform 120ms',
                    transform: showEventLog ? 'rotate(0deg)' : 'rotate(-90deg)',
                  }}
                />
                Event log ({events.length})
              </button>
              {showEventLog && (
                <div
                  style={{
                    marginTop: '6px',
                    maxHeight: '160px',
                    overflowY: 'auto',
                    border: '1px solid var(--paper-200)',
                    borderRadius: '4px',
                    backgroundColor: 'var(--paper-100)',
                  }}
                >
                  {events.length === 0 && (
                    <div
                      style={{
                        padding: '8px 10px',
                        fontSize: '11px',
                        color: 'var(--paper-400)',
                        fontFamily: 'var(--font-mono)',
                      }}
                    >
                      (no events received yet)
                    </div>
                  )}
                  {events.map((e, i) => {
                    const ts = new Date(e.ts).toLocaleTimeString();
                    const summary =
                      (e.data?.to as string | undefined) ??
                      (e.data?.error_code as string | undefined) ??
                      Object.keys(e.data ?? {}).slice(0, 3).join(',');
                    return (
                      <div
                        key={`${e.seq}-${i}`}
                        style={{
                          padding: '4px 10px',
                          fontFamily: 'var(--font-mono)',
                          fontSize: '11px',
                          color: 'var(--paper-600)',
                          borderBottom: i === events.length - 1 ? 'none' : '1px solid var(--paper-200)',
                          display: 'flex',
                          gap: '8px',
                        }}
                      >
                        <span style={{ color: 'var(--paper-400)' }}>{ts}</span>
                        <span style={{ fontWeight: 600 }}>{e.event_type}</span>
                        {summary && <span style={{ color: 'var(--paper-500)' }}>{summary}</span>}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          {/* Blocks list — caps at half the sidebar so the reading view below
              stays attached just under it (rather than dropping to the bottom
              when collapsed). Internal scroll handles long block lists. */}
          <div
            style={{
              flexGrow: 0,
              flexShrink: 1,
              flexBasis: 'auto',
              maxHeight: '50%',
              overflow: 'auto',
              minHeight: 0,
            }}
          >
            <div
              style={{
                padding: '10px 16px',
                borderBottom: '1px solid var(--paper-200)',
                backgroundColor: 'var(--paper-100)',
              }}
            >
              <span className="eyebrow">
                Extracted Blocks (page {currentPage})
              </span>
              <span
                className="mono-id"
                style={{ marginLeft: '8px', fontSize: '11px' }}
              >
                {currentPageBlocks.length} blocks
              </span>
            </div>

            {blocks.length === 0 && doc?.status !== 'ready' && (
              <div style={{ padding: '24px 16px', color: 'var(--paper-400)', fontSize: '12px' }}>
                Blocks available after document is ready.
              </div>
            )}

            {blocks.length === 0 && doc?.status === 'ready' && (
              <div style={{ padding: '24px 16px', color: 'var(--paper-400)', fontSize: '12px' }}>
                No blocks found for this document.
              </div>
            )}

            {currentPageBlocks.map((block) => (
              <div
                key={block.id}
                id={`block-${block.id}`}
                onMouseEnter={() => setHoveredBlock(block)}
                onMouseLeave={() => setHoveredBlock(null)}
                style={{
                  padding: '10px 16px',
                  borderBottom: '1px solid var(--paper-200)',
                  cursor: 'default',
                  backgroundColor: hoveredBlock?.id === block.id ? 'var(--ochre-50)' : 'transparent',
                  transition: 'background-color 100ms',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                  <span
                    style={{
                      fontSize: '10px',
                      fontFamily: 'var(--font-mono)',
                      padding: '1px 5px',
                      borderRadius: '3px',
                      backgroundColor: 'var(--paper-200)',
                      color: 'var(--paper-500)',
                    }}
                  >
                    {block.block_type}
                  </span>
                  <span className="mono-id" style={{ fontSize: '10px' }}>
                    [{block.bbox.map((v) => v.toFixed(2)).join(',')}]
                  </span>
                  <div style={{ flex: 1 }} />
                  {doc?.status === 'ready' && editingBlockId !== block.id && (
                    <button
                      type="button"
                      aria-label="Edit block text"
                      title="Edit OCR transcript for this block"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingBlockId(block.id);
                        setEditingBlockText(block.text);
                      }}
                      style={{
                        background: 'none',
                        border: 'none',
                        cursor: 'pointer',
                        color: 'var(--paper-400)',
                        padding: 0,
                        display: 'inline-flex',
                      }}
                    >
                      <Pencil size={11} />
                    </button>
                  )}
                  <button
                    type="button"
                    aria-label="Copy block text"
                    title={copiedBlockId === block.id ? 'Copied!' : 'Copy block text'}
                    onClick={(e) => {
                      e.stopPropagation();
                      copyBlockText(block.id, block.text);
                    }}
                    style={{
                      background: 'none',
                      border: 'none',
                      cursor: 'pointer',
                      color:
                        copiedBlockId === block.id
                          ? 'var(--ochre-500)'
                          : 'var(--paper-400)',
                      padding: 0,
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '3px',
                      fontFamily: 'var(--font-mono)',
                      fontSize: '10px',
                    }}
                  >
                    {copiedBlockId === block.id ? (
                      <>
                        <Check size={11} />
                        Copied
                      </>
                    ) : (
                      <Copy size={11} />
                    )}
                  </button>
                </div>
                {editingBlockId === block.id ? (
                  <div>
                    <textarea
                      value={editingBlockText}
                      onChange={(e) => setEditingBlockText(e.target.value)}
                      disabled={savingBlock}
                      style={{
                        width: '100%',
                        minHeight: '90px',
                        padding: '6px 8px',
                        fontFamily: 'var(--font-mono)',
                        fontSize: '12px',
                        lineHeight: 1.45,
                        color: 'var(--paper-700)',
                        border: '1px solid var(--ochre-500)',
                        borderRadius: '4px',
                        resize: 'vertical',
                        outline: 'none',
                        backgroundColor: '#fff',
                      }}
                    />
                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '6px', marginTop: '4px' }}>
                      <button
                        type="button"
                        onClick={() => {
                          setEditingBlockId(null);
                          setEditingBlockText('');
                        }}
                        disabled={savingBlock}
                        style={{
                          background: 'none',
                          border: '1px solid var(--paper-300)',
                          padding: '3px 8px',
                          fontSize: '11px',
                          borderRadius: '3px',
                          cursor: savingBlock ? 'wait' : 'pointer',
                          color: 'var(--paper-500)',
                        }}
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={saveBlockEdit}
                        disabled={savingBlock || !editingBlockText.trim()}
                        style={{
                          background: 'var(--ochre-500)',
                          border: '1px solid var(--ochre-500)',
                          padding: '3px 8px',
                          fontSize: '11px',
                          borderRadius: '3px',
                          cursor: savingBlock ? 'wait' : 'pointer',
                          color: '#fff',
                        }}
                      >
                        {savingBlock ? 'Saving…' : 'Save'}
                      </button>
                    </div>
                    <div style={{ marginTop: '4px', fontSize: '11px', color: 'var(--paper-400)', fontFamily: 'var(--font-mono)' }}>
                      Edits only update the OCR transcript; the PDF page image stays as scanned.
                    </div>
                  </div>
                ) : (
                  <p
                    className="prose-source"
                    style={{ margin: 0, fontSize: '12px' }}
                  >
                    {block.text.length > 200
                      ? block.text.slice(0, 200) + '...'
                      : block.text}
                  </p>
                )}
              </div>
            ))}
          </div>

          {/* Layout — reading view: blocks rendered as continuous prose so the
              operator can read the page text without bbox/block-id noise.
              Per-block Edit + Copy controls mirror the Extracted Blocks panel
              above and share the same patchBlockText + clipboard handlers. */}
          <div
            style={{
              borderTop: '1px solid var(--paper-300)',
              backgroundColor: '#fff',
              display: 'flex',
              flexDirection: 'column',
              flexShrink: 0,
              flexGrow: showReadingView ? 1 : 0,
              flexBasis: showReadingView ? 0 : 'auto',
              minHeight: showReadingView ? 0 : undefined,
            }}
          >
            <button
              type="button"
              onClick={() => setShowReadingView((v) => !v)}
              aria-expanded={showReadingView}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                width: '100%',
                padding: '10px 16px',
                background: 'var(--paper-100)',
                border: 'none',
                borderBottom: showReadingView ? '1px solid var(--paper-200)' : 'none',
                cursor: 'pointer',
                color: 'var(--paper-600)',
                textAlign: 'left',
                flexShrink: 0,
              }}
            >
              <ChevronDown
                size={12}
                style={{
                  transition: 'transform 120ms',
                  transform: showReadingView ? 'rotate(0deg)' : 'rotate(-90deg)',
                }}
              />
              <span className="eyebrow" style={{ flex: 1 }}>
                Layout — Reading View (page {currentPage})
              </span>
              <span
                className="mono-id"
                style={{ fontSize: '11px', color: 'var(--paper-500)' }}
              >
                {currentPageBlocks.length} block{currentPageBlocks.length === 1 ? '' : 's'}
              </span>
            </button>
            {showReadingView && (
              <div style={{ flex: 1, overflowY: 'auto' }}>
                {currentPageBlocks.length === 0 && (
                  <div style={{ padding: '24px 16px', color: 'var(--paper-400)', fontSize: '12px' }}>
                    {doc?.status === 'ready'
                      ? 'No blocks on this page.'
                      : 'Reading view available once layout completes.'}
                  </div>
                )}

                {currentPageBlocks.map((block) => (
                  <div
                    key={`reading-${block.id}`}
                    id={`reading-block-${block.id}`}
                    onMouseEnter={() => setHoveredBlock(block)}
                    onMouseLeave={() => setHoveredBlock(null)}
                    style={{
                      padding: '10px 16px',
                      borderBottom: '1px solid var(--paper-200)',
                      cursor: 'default',
                      backgroundColor: hoveredBlock?.id === block.id ? 'var(--ochre-50)' : 'transparent',
                      transition: 'background-color 100ms',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                      <span
                        style={{
                          fontSize: '10px',
                          fontFamily: 'var(--font-mono)',
                          padding: '1px 5px',
                          borderRadius: '3px',
                          backgroundColor: 'var(--paper-200)',
                          color: 'var(--paper-500)',
                        }}
                      >
                        {block.block_type}
                      </span>
                      <span className="mono-id" style={{ fontSize: '10px' }}>
                        [{block.bbox.map((v) => v.toFixed(2)).join(',')}]
                      </span>
                      <div style={{ flex: 1 }} />
                      {doc?.status === 'ready' && editingBlockId !== block.id && (
                        <button
                          type="button"
                          aria-label="Edit block text"
                          title="Edit OCR transcript for this block"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingBlockId(block.id);
                            setEditingBlockText(block.text);
                          }}
                          style={{
                            background: 'none',
                            border: 'none',
                            cursor: 'pointer',
                            color: 'var(--paper-400)',
                            padding: 0,
                            display: 'inline-flex',
                          }}
                        >
                          <Pencil size={11} />
                        </button>
                      )}
                      <button
                        type="button"
                        aria-label="Copy block text"
                        title={copiedBlockId === block.id ? 'Copied!' : 'Copy block text'}
                        onClick={(e) => {
                          e.stopPropagation();
                          copyBlockText(block.id, block.text);
                        }}
                        style={{
                          background: 'none',
                          border: 'none',
                          cursor: 'pointer',
                          color:
                            copiedBlockId === block.id
                              ? 'var(--ochre-500)'
                              : 'var(--paper-400)',
                          padding: 0,
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '3px',
                          fontFamily: 'var(--font-mono)',
                          fontSize: '10px',
                        }}
                      >
                        {copiedBlockId === block.id ? (
                          <>
                            <Check size={11} />
                            Copied
                          </>
                        ) : (
                          <Copy size={11} />
                        )}
                      </button>
                    </div>
                    {editingBlockId === block.id ? (
                      <div>
                        <textarea
                          value={editingBlockText}
                          onChange={(e) => setEditingBlockText(e.target.value)}
                          disabled={savingBlock}
                          style={{
                            width: '100%',
                            minHeight: '90px',
                            padding: '6px 8px',
                            fontFamily: 'var(--font-mono)',
                            fontSize: '12px',
                            lineHeight: 1.45,
                            color: 'var(--paper-700)',
                            border: '1px solid var(--ochre-500)',
                            borderRadius: '4px',
                            resize: 'vertical',
                            outline: 'none',
                            backgroundColor: '#fff',
                          }}
                        />
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '6px', marginTop: '4px' }}>
                          <button
                            type="button"
                            onClick={() => {
                              setEditingBlockId(null);
                              setEditingBlockText('');
                            }}
                            disabled={savingBlock}
                            style={{
                              background: 'none',
                              border: '1px solid var(--paper-300)',
                              padding: '3px 8px',
                              fontSize: '11px',
                              borderRadius: '3px',
                              cursor: savingBlock ? 'wait' : 'pointer',
                              color: 'var(--paper-500)',
                            }}
                          >
                            Cancel
                          </button>
                          <button
                            type="button"
                            onClick={saveBlockEdit}
                            disabled={savingBlock || !editingBlockText.trim()}
                            style={{
                              background: 'var(--ochre-500)',
                              border: '1px solid var(--ochre-500)',
                              padding: '3px 8px',
                              fontSize: '11px',
                              borderRadius: '3px',
                              cursor: savingBlock ? 'wait' : 'pointer',
                              color: '#fff',
                            }}
                          >
                            {savingBlock ? 'Saving…' : 'Save'}
                          </button>
                        </div>
                        <div style={{ marginTop: '4px', fontSize: '11px', color: 'var(--paper-400)', fontFamily: 'var(--font-mono)' }}>
                          Edits only update the OCR transcript; the PDF page image stays as scanned.
                        </div>
                      </div>
                    ) : (
                      <p
                        className="prose-source"
                        style={{ margin: 0, fontSize: '12px' }}
                      >
                        {block.text.length > 200
                          ? block.text.slice(0, 200) + '...'
                          : block.text}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmingDelete}
        title="Delete document?"
        message={
          doc && (
            <>
              <strong style={{ color: 'var(--paper-700)' }}>{doc.filename}</strong> and every
              artifact derived from it (pages, blocks, chunks, embeddings, drafts) will be removed.
              This cannot be undone.
            </>
          )
        }
        confirmLabel="Delete"
        variant="danger"
        icon={Trash2}
        busy={deleting}
        onConfirm={confirmDelete}
        onCancel={() => {
          if (deleting) return;
          setConfirmingDelete(false);
        }}
      />
    </div>
  );
}
