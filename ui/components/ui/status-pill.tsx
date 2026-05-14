'use client';

import React from 'react';

type ToneBucket = 'pending' | 'running' | 'ready' | 'failed';

function getTone(status: string): ToneBucket {
  if (status === 'ready') return 'ready';
  if (status === 'failed') return 'failed';
  if (status.endsWith('_running')) return 'running';
  // All other states: uploaded, ocr_pending, ocr_done, layout_done,
  // chunking_done, embedding_running* → pending or running
  if (status === 'embedding_running') return 'running';
  return 'pending';
}

const toneColors: Record<ToneBucket, { fg: string; bg: string }> = {
  pending: { fg: 'var(--status-pending-fg)', bg: 'var(--status-pending-bg)' },
  running: { fg: 'var(--status-running-fg)', bg: 'var(--status-running-bg)' },
  ready: { fg: 'var(--status-ready-fg)', bg: 'var(--status-ready-bg)' },
  failed: { fg: 'var(--status-failed-fg)', bg: 'var(--status-failed-bg)' },
};

function formatStatus(status: string): string {
  return status.replace(/_/g, ' ');
}

interface StatusPillProps {
  status: string;
  className?: string;
}

export function StatusPill({ status, className }: StatusPillProps) {
  const tone = getTone(status);
  const { fg, bg } = toneColors[tone];
  const isRunning = tone === 'running';

  return (
    <span
      className={className}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '5px',
        padding: '2px 8px',
        borderRadius: 'var(--radius-pill)',
        backgroundColor: bg,
        color: fg,
        fontFamily: 'var(--font-mono)',
        fontSize: '11px',
        fontWeight: 500,
        whiteSpace: 'nowrap',
        lineHeight: '18px',
      }}
    >
      {isRunning ? (
        <span
          style={{
            width: '7px',
            height: '7px',
            border: `1.5px solid ${fg}`,
            borderTopColor: 'transparent',
            borderRadius: '50%',
            animation: 'lit-spin 0.7s linear infinite',
            display: 'inline-block',
            flexShrink: 0,
          }}
        />
      ) : (
        <span
          style={{
            width: '7px',
            height: '7px',
            borderRadius: '50%',
            backgroundColor: fg,
            display: 'inline-block',
            flexShrink: 0,
          }}
        />
      )}
      {formatStatus(status)}
    </span>
  );
}
