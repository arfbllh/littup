'use client';

import React from 'react';
import { CitationChip } from './ui/citation-chip';
import type { CitationView } from '@/lib/types';

interface CitedTextProps {
  text: string;
  citations: CitationView[];
  selectedChunkId?: string | null;
  onCitationClick?: (citation: CitationView) => void;
}

interface TextSegment {
  type: 'text';
  content: string;
}

interface ChipSegment {
  type: 'chip';
  chunkId: string;
  citation: CitationView | null;
}

type Segment = TextSegment | ChipSegment;

function parseText(text: string, citations: CitationView[]): Segment[] {
  const citationMap = new Map<string, CitationView>();
  for (const c of citations) {
    citationMap.set(c.chunk_id, c);
  }

  const segments: Segment[] = [];
  const regex = /\[chunk:([^\]]+)\]/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ type: 'text', content: text.slice(lastIndex, match.index) });
    }
    const chunkId = match[1];
    segments.push({
      type: 'chip',
      chunkId,
      citation: citationMap.get(chunkId) ?? null,
    });
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    segments.push({ type: 'text', content: text.slice(lastIndex) });
  }

  return segments;
}

export function CitedText({ text, citations, selectedChunkId, onCitationClick }: CitedTextProps) {
  const segments = parseText(text, citations);

  return (
    <span className="prose-doc">
      {segments.map((seg, i) => {
        if (seg.type === 'text') {
          return <span key={i}>{seg.content}</span>;
        }
        const citation = seg.citation;
        if (!citation) {
          return (
            <CitationChip
              key={i}
              chunkId={seg.chunkId}
              status="unchecked"
              selected={selectedChunkId === seg.chunkId}
              tooltip={`Chunk ${seg.chunkId} — no validation data`}
            />
          );
        }
        return (
          <CitationChip
            key={i}
            chunkId={citation.chunk_id}
            status={citation.validation_status}
            selected={selectedChunkId === citation.chunk_id}
            onClick={onCitationClick ? () => onCitationClick(citation) : undefined}
            tooltip={citation.validation_reason ?? `Chunk ${citation.chunk_id} — ${citation.validation_status}`}
          />
        );
      })}
    </span>
  );
}
