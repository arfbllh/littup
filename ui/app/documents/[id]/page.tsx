'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import useSWR from 'swr';
import { ArrowLeft, ChevronLeft, ChevronRight, Download } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { StatusPill } from '@/components/ui/status-pill';
import { Button } from '@/components/ui/button';
import { PageWithBboxes } from '@/components/PageWithBboxes';
import { getDocument, getBlocks } from '@/lib/api';
import { subscribeToDocumentEvents } from '@/lib/sse';
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

function TimelineStep({ label, active, done }: { label: string; active: boolean; done: boolean }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '11px', fontFamily: 'var(--font-mono)' }}>
      <span
        style={{
          width: '8px',
          height: '8px',
          borderRadius: '50%',
          flexShrink: 0,
          backgroundColor: done
            ? 'var(--cite-supported)'
            : active
            ? 'var(--ochre-500)'
            : 'var(--paper-300)',
        }}
      />
      <span
        style={{
          color: done
            ? 'var(--cite-supported)'
            : active
            ? 'var(--ochre-500)'
            : 'var(--paper-300)',
          fontWeight: active ? 600 : 400,
        }}
      >
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
  const [liveStatus, setLiveStatus] = useState<string | null>(null);

  const { data: doc, mutate: mutateDoc } = useSWR(
    id ? `doc:${id}` : null,
    () => getDocument(id)
  );

  const { data: blocksData } = useSWR(
    doc?.status === 'ready' ? `blocks:${id}` : null,
    () => getBlocks(id)
  );

  const handleSSEEvent = useCallback(
    (event: { event_type: string; data: Record<string, unknown> }) => {
      if (event.data.status) {
        setLiveStatus(event.data.status as string);
        mutateDoc();
      }
    },
    [mutateDoc]
  );

  useEffect(() => {
    if (!id) return;
    const cleanup = subscribeToDocumentEvents(id, handleSSEEvent, () => {});
    return cleanup;
  }, [id, handleSSEEvent]);

  const status = liveStatus ?? doc?.status ?? 'uploaded';
  const pageCount = doc?.page_count ?? 1;
  const blocks = blocksData?.blocks ?? [];
  const currentPageBlocks = blocks.filter((b) => b.page === currentPage);

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

  const currentStatusIdx = STATE_MACHINE.indexOf(status);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader
        eyebrow="Documents"
        title={doc?.filename ?? 'Loading...'}
        actions={
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            {doc && <StatusPill status={status} />}
            <Button variant="ghost" size="sm" icon={Download} onClick={handleExport}>
              Export JSON
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
                highlightBbox={hoveredBlock?.page === currentPage ? hoveredBlock.bbox : null}
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
          </div>

          {/* Blocks list */}
          <div
            style={{
              flex: 1,
              overflow: 'auto',
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
                    {block.kind}
                  </span>
                  <span className="mono-id" style={{ fontSize: '10px' }}>
                    [{block.bbox.map((v) => v.toFixed(0)).join(',')}]
                  </span>
                  {block.confidence < 0.8 && (
                    <span
                      style={{
                        fontSize: '10px',
                        fontFamily: 'var(--font-mono)',
                        color: 'var(--cite-partial)',
                      }}
                    >
                      conf:{(block.confidence * 100).toFixed(0)}%
                    </span>
                  )}
                </div>
                <p
                  className="prose-source"
                  style={{ margin: 0, fontSize: '12px' }}
                >
                  {block.text.length > 200
                    ? block.text.slice(0, 200) + '...'
                    : block.text}
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
