'use client';

import React from 'react';
import type { CitationStatus } from '@/lib/types';

interface CitationChipProps {
  chunkId: string;
  page?: number | null;
  status: CitationStatus;
  selected?: boolean;
  onClick?: () => void;
  tooltip?: string;
}

const statusColors: Record<CitationStatus, { fg: string; bg: string }> = {
  supported: { fg: 'var(--cite-supported)', bg: 'var(--cite-supported-bg)' },
  partial: { fg: 'var(--cite-partial)', bg: 'var(--cite-partial-bg)' },
  unsupported: { fg: 'var(--cite-unsupported)', bg: 'var(--cite-unsupported-bg)' },
  unchecked: { fg: 'var(--cite-unchecked)', bg: 'var(--cite-unchecked-bg)' },
};

function shortId(chunkId: string): string {
  // Show last 6 chars of the chunk ID for brevity
  return chunkId.slice(-6);
}

export function CitationChip({ chunkId, page, status, selected, onClick, tooltip }: CitationChipProps) {
  const [hovered, setHovered] = React.useState(false);
  const { fg, bg } = statusColors[status];

  const style: React.CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '3px',
    padding: '1px 6px',
    borderRadius: 'var(--radius-pill)',
    backgroundColor: bg,
    color: fg,
    fontFamily: 'var(--font-mono)',
    fontSize: '11px',
    fontWeight: 500,
    cursor: onClick ? 'pointer' : 'default',
    border: selected ? `1.5px solid var(--ochre-500)` : `1px solid ${fg}33`,
    outline: selected ? '2px solid var(--ochre-100)' : 'none',
    outlineOffset: '1px',
    transition: 'opacity 100ms',
    opacity: hovered ? 0.8 : 1,
    verticalAlign: 'middle',
    position: 'relative',
    userSelect: 'none',
  };

  return (
    <span
      style={style}
      onClick={onClick}
      title={tooltip ?? `Chunk ${chunkId} — ${status}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {shortId(chunkId)}
      {page != null && <span style={{ opacity: 0.7 }}>:{page}</span>}
    </span>
  );
}
