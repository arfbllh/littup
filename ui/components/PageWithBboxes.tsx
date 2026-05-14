'use client';

import React, { useRef, useEffect, useState, useCallback } from 'react';
import { getPageImageUrl } from '@/lib/api';

interface PageWithBboxesProps {
  documentId: string;
  page: number;
  highlightBbox?: [number, number, number, number] | null;
  style?: React.CSSProperties;
}

export function PageWithBboxes({ documentId, page, highlightBbox, style }: PageWithBboxesProps) {
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
    // Scale from natural image coords to rendered display coords
    const scaleX = rect.width / imgSize.w;
    const scaleY = rect.height / imgSize.h;

    const rx = x0 * scaleX;
    const ry = y0 * scaleY;
    const rw = (x1 - x0) * scaleX;
    const rh = (y1 - y0) * scaleY;

    ctx.strokeStyle = 'var(--ochre-500, #b8741a)';
    ctx.lineWidth = 2;
    ctx.fillStyle = 'rgba(184, 116, 26, 0.12)';

    ctx.fillRect(rx, ry, rw, rh);
    ctx.strokeRect(rx, ry, rw, rh);
  }, [highlightBbox, imgSize]);

  useEffect(() => {
    drawHighlight();
  }, [drawHighlight]);

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
          Page {page} not available
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
