'use client';

import React, { useCallback, useRef, useState } from 'react';
import { Upload, X, CheckCircle, AlertCircle } from 'lucide-react';
import { uploadDocument } from '@/lib/api';
import type { UploadResponse } from '@/lib/types';
import { Spinner } from './ui/spinner';

type FileStatus = 'pending' | 'uploading' | 'done' | 'error';

interface FileEntry {
  file: File;
  status: FileStatus;
  progress: number;
  error?: string;
  result?: UploadResponse;
}

interface UploadDropzoneProps {
  onUploaded?: (result: UploadResponse, file: File) => void;
  compact?: boolean;
}

const CONCURRENCY = 3;

async function uploadWithRetry(
  file: File,
  onProgress: (p: number) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  let attempt = 0;
  const maxAttempts = 4;
  while (attempt < maxAttempts) {
    if (signal?.aborted) throw new Error('Aborted');
    try {
      onProgress(10 + attempt * 10);
      const result = await uploadDocument(file);
      onProgress(100);
      return result;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes('429') && attempt < maxAttempts - 1) {
        const delay = Math.pow(2, attempt) * 1000;
        await new Promise((r) => setTimeout(r, delay));
        attempt++;
      } else {
        throw err;
      }
    }
  }
  throw new Error('Max retries exceeded');
}

export function UploadDropzone({ onUploaded, compact = false }: UploadDropzoneProps) {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const processingRef = useRef(false);

  const processQueue = useCallback(async (entries: FileEntry[]) => {
    if (processingRef.current) return;
    processingRef.current = true;

    const queue = [...entries];
    const active: Promise<void>[] = [];

    async function processOne(entry: FileEntry) {
      setFiles((prev) =>
        prev.map((f) => (f.file === entry.file ? { ...f, status: 'uploading' } : f))
      );
      try {
        const result = await uploadWithRetry(entry.file, (progress) => {
          setFiles((prev) =>
            prev.map((f) => (f.file === entry.file ? { ...f, progress } : f))
          );
        });
        setFiles((prev) =>
          prev.map((f) =>
            f.file === entry.file ? { ...f, status: 'done', progress: 100, result } : f
          )
        );
        onUploaded?.(result, entry.file);
      } catch (err) {
        const error = err instanceof Error ? err.message : 'Upload failed';
        setFiles((prev) =>
          prev.map((f) =>
            f.file === entry.file ? { ...f, status: 'error', error } : f
          )
        );
      }
    }

    while (queue.length > 0 || active.length > 0) {
      while (active.length < CONCURRENCY && queue.length > 0) {
        const entry = queue.shift()!;
        const p = processOne(entry);
        active.push(p);
        p.then(() => {
          const idx = active.indexOf(p);
          if (idx !== -1) active.splice(idx, 1);
        });
      }
      if (active.length > 0) {
        await Promise.race(active);
      }
    }

    processingRef.current = false;
  }, [onUploaded]);

  function addFiles(fileList: FileList | File[]) {
    const arr = Array.from(fileList);
    const newEntries: FileEntry[] = arr.map((file) => ({
      file,
      status: 'pending',
      progress: 0,
    }));
    setFiles((prev) => {
      const combined = [...prev, ...newEntries];
      processQueue(newEntries);
      return combined;
    });
  }

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      addFiles(e.dataTransfer.files);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );

  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(true);
  };

  const onDragLeave = () => setDragging(false);

  const onInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) addFiles(e.target.files);
    e.target.value = '';
  };

  function removeFile(entry: FileEntry) {
    setFiles((prev) => prev.filter((f) => f !== entry));
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <div
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onClick={() => inputRef.current?.click()}
        style={{
          border: `2px dashed ${dragging ? 'var(--ochre-500)' : 'var(--paper-300)'}`,
          borderRadius: 'var(--radius-card)',
          backgroundColor: dragging ? 'var(--ochre-50)' : 'var(--paper-100)',
          padding: compact ? '16px' : '32px',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '8px',
          cursor: 'pointer',
          transition: 'border-color 150ms, background-color 150ms',
          minHeight: compact ? '80px' : '160px',
        }}
      >
        <Upload size={compact ? 16 : 24} color="var(--paper-400)" />
        <span
          style={{
            fontSize: compact ? '12px' : '14px',
            color: 'var(--paper-500)',
            fontFamily: 'var(--font-sans)',
            textAlign: 'center',
          }}
        >
          {dragging
            ? 'Drop files here'
            : 'Drag PDF files here or click to browse'}
        </span>
        {!compact && (
          <span style={{ fontSize: '12px', color: 'var(--paper-400)', fontFamily: 'var(--font-sans)' }}>
            Multiple files supported
          </span>
        )}
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,application/pdf"
          style={{ display: 'none' }}
          onChange={onInputChange}
        />
      </div>

      {files.length > 0 && (
        <div
          style={{
            border: '1px solid var(--paper-300)',
            borderRadius: 'var(--radius-card)',
            backgroundColor: '#fff',
            overflow: 'hidden',
          }}
        >
          {files.map((entry, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '8px 12px',
                borderBottom: i < files.length - 1 ? '1px solid var(--paper-200)' : 'none',
              }}
            >
              {/* Status icon */}
              {entry.status === 'uploading' && <Spinner size={14} color="var(--ochre-500)" />}
              {entry.status === 'done' && <CheckCircle size={14} color="var(--cite-supported)" />}
              {entry.status === 'error' && <AlertCircle size={14} color="var(--cite-unsupported)" />}
              {entry.status === 'pending' && (
                <span
                  style={{
                    width: '14px',
                    height: '14px',
                    borderRadius: '50%',
                    border: '1px solid var(--paper-300)',
                    display: 'inline-block',
                    flexShrink: 0,
                  }}
                />
              )}

              {/* Filename */}
              <span
                style={{
                  flex: 1,
                  fontSize: '12px',
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--paper-600)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {entry.file.name}
              </span>

              {/* Progress / result */}
              {entry.status === 'uploading' && (
                <span
                  style={{
                    fontSize: '11px',
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--ochre-500)',
                  }}
                >
                  {entry.progress}%
                </span>
              )}
              {entry.status === 'done' && entry.result && (
                <span
                  style={{
                    fontSize: '11px',
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--cite-supported)',
                  }}
                >
                  {entry.result.was_new ? 'uploaded' : 'duplicate'}
                </span>
              )}
              {entry.status === 'error' && (
                <span
                  style={{
                    fontSize: '11px',
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--cite-unsupported)',
                    maxWidth: '200px',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={entry.error}
                >
                  {entry.error}
                </span>
              )}

              {/* Remove button */}
              <button
                onClick={(e) => { e.stopPropagation(); removeFile(entry); }}
                style={{
                  background: 'none',
                  border: 'none',
                  padding: '2px',
                  cursor: 'pointer',
                  color: 'var(--paper-400)',
                  display: 'flex',
                  alignItems: 'center',
                }}
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
