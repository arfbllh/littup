'use client';

import React, { useRef, useEffect, useState, useCallback } from 'react';
import { getPageImageUrl } from '@/lib/api';

interface PageWithBboxesProps {
  documentId: string;
  page: number;
  highlightBbox?: [number, number, number, number] | null;
  style?: React.CSSProperties;
  /** Document ingestion status — controls the placeholder copy when the image 404s. */
  docStatus?: string;
}

// Stages before page PNGs are written to disk by the OCR worker.
const PRE_OCR_STATES = new Set(['uploaded', 'ocr_pending', 'ocr_running']);

function placeholderMessage(page: number, docStatus: string | undefined): string {
  if (docStatus === 'failed') return `Ingestion failed — page ${page} was never rendered.`;
  if (docStatus && PRE_OCR_STATES.has(docStatus)) {
    return `Page ${page} renders after OCR completes.`;
  }
  if (docStatus && docStatus !== 'ready') {
    return `Page ${page} not ready yet — still processing.`;
  }
  return `Page ${page} not available`;
}

export function PageWithBboxes({ documentId, page, highlightBbox, style, docStatus }: PageWithBboxesProps) {
  const imgRef = useRef<HTMLImageElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [imgSize, setImgSize] = useState<{ w: number; h: number } | null>(null);
  const [imgError, setImgError] = useState(false);

  const imageUrl = getPageImageUrl(documentId, page);

  const drawHighlight = useCallback(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img || !imgSize) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Match canvas to rendered size
    const rect = img.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (!highlightBbox) return;

    const [x0, y0, x1, y1] = highlightBbox;
    // Bboxes are normalized [0,1] when produced by pdfplumber / PaddleOCR (the
    // common case). Docling, on the rare native-doc path, emits PDF-point
    // coords. Detect by max magnitude and scale accordingly.
    const normalized = Math.max(x0, y0, x1, y1) <= 1.0001;
    const sx = normalized ? rect.width : rect.width / imgSize.w;
    const sy = normalized ? rect.height : rect.height / imgSize.h;

    const rx = x0 * sx;
    const ry = y0 * sy;
    const rw = (x1 - x0) * sx;
    const rh = (y1 - y0) * sy;

    ctx.strokeStyle = 'var(--ochre-500, #b8741a)';
    ctx.lineWidth = 2;
    ctx.fillStyle = 'rgba(184, 116, 26, 0.12)';

    ctx.fillRect(rx, ry, rw, rh);
    ctx.strokeRect(rx, ry, rw, rh);
  }, [highlightBbox, imgSize]);

  useEffect(() => {
    drawHighlight();
  }, [drawHighlight]);

  // Reset the error flag when the source URL or doc status changes — without this,
  // an early 404 (e.g. during ocr_pending) sticks even after the image becomes
  // available, so the user sees the placeholder forever.
  useEffect(() => {
    setImgError(false);
  }, [imageUrl, docStatus]);

  function handleImageLoad() {
    const img = imgRef.current;
    if (!img) return;
    setImgSize({ w: img.naturalWidth, h: img.naturalHeight });
    setImgError(false);
  }

  return (
    <div
      style={{
        position: 'relative',
        display: 'inline-block',
        backgroundColor: 'var(--paper-200)',
        borderRadius: 'var(--radius-card)',
        overflow: 'hidden',
        ...style,
      }}
    >
      {imgError ? (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            minHeight: '200px',
            minWidth: '160px',
            color: 'var(--paper-400)',
            fontSize: '12px',
            fontFamily: 'var(--font-sans)',
            padding: '16px',
            textAlign: 'center',
          }}
        >
          {placeholderMessage(page, docStatus)}
        </div>
      ) : (
        <>
          <img
            ref={imgRef}
            src={imageUrl}
            alt={`Document page ${page}`}
            onLoad={handleImageLoad}
            onError={() => setImgError(true)}
            style={{ display: 'block', maxWidth: '100%', height: 'auto' }}
          />
          <canvas
            ref={canvasRef}
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              width: '100%',
              height: '100%',
              pointerEvents: 'none',
            }}
          />
        </>
      )}
    </div>
  );
}
