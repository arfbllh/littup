'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';
import { Search, Upload, ChevronRight } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { StatusPill } from '@/components/ui/status-pill';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { UploadDropzone } from '@/components/UploadDropzone';
import { listDocuments } from '@/lib/api';
import type { DocumentSummary } from '@/lib/types';

function formatBytes(bytes: number | null): string {
  if (bytes == null) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'short',
    timeStyle: 'short',
  });
}

export default function DocumentsPage() {
  const router = useRouter();
  const [filter, setFilter] = useState('');
  const [showUpload, setShowUpload] = useState(false);

  const { data, error, isLoading, mutate } = useSWR(
    'documents',
    () => listDocuments(),
    { refreshInterval: 5_000 }
  );

  const documents: DocumentSummary[] = data?.items ?? [];

  const filtered = filter
    ? documents.filter((d) =>
        d.filename.toLowerCase().includes(filter.toLowerCase())
      )
    : documents;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader
        eyebrow="Workspace"
        title="Documents"
        actions={
          <>
            <span
              style={{
                fontSize: '12px',
                fontFamily: 'var(--font-mono)',
                color: 'var(--paper-400)',
                padding: '2px 8px',
                backgroundColor: 'var(--paper-100)',
                borderRadius: 'var(--radius-pill)',
                border: '1px solid var(--paper-300)',
              }}
            >
              {documents.length} doc{documents.length !== 1 ? 's' : ''}
            </span>
            <Button
              variant="primary"
              size="sm"
              icon={Upload}
              onClick={() => setShowUpload((v) => !v)}
            >
              Upload documents
            </Button>
          </>
        }
      />

      <div style={{ flex: 1, overflow: 'auto', padding: '16px 24px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {showUpload && (
          <div
            style={{
              backgroundColor: '#fff',
              border: '1px solid var(--paper-300)',
              borderRadius: 'var(--radius-card)',
              padding: '16px',
            }}
          >
            <UploadDropzone
              compact
              onUploaded={(result) => {
                mutate();
                router.push(`/documents/${result.document_id}`);
              }}
            />
          </div>
        )}

        {/* Filter */}
        <div style={{ maxWidth: '320px' }}>
          <Input
            icon={Search}
            placeholder="Filter by filename..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        {/* Table */}
        <div
          style={{
            backgroundColor: '#fff',
            border: '1px solid var(--paper-300)',
            borderRadius: 'var(--radius-card)',
            overflow: 'hidden',
          }}
        >
          {isLoading && (
            <div style={{ padding: '32px', textAlign: 'center', color: 'var(--paper-400)', fontSize: '13px' }}>
              Loading documents...
            </div>
          )}

          {error && (
            <div style={{ padding: '32px', textAlign: 'center', color: 'var(--cite-unsupported)', fontSize: '13px' }}>
              Failed to load documents. Check API connection.
            </div>
          )}

          {!isLoading && !error && filtered.length === 0 && (
            <div style={{ padding: '48px', textAlign: 'center', color: 'var(--paper-400)', fontSize: '13px' }}>
              {documents.length === 0
                ? 'No documents yet. Upload a PDF to get started.'
                : 'No documents match your filter.'}
            </div>
          )}

          {!isLoading && filtered.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Status</th>
                  <th>Pages</th>
                  <th>Size</th>
                  <th>Uploaded</th>
                  <th style={{ width: '32px' }}></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((doc) => (
                  <tr
                    key={doc.document_id}
                    style={{ cursor: 'pointer' }}
                    onClick={() => router.push(`/documents/${doc.document_id}`)}
                  >
                    <td>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                        <span style={{ color: 'var(--paper-700)', fontWeight: 500, fontSize: '13px' }}>
                          {doc.filename}
                        </span>
                        <span className="mono-id" style={{ fontSize: '11px' }}>
                          {doc.document_id.slice(0, 8)}
                        </span>
                      </div>
                    </td>
                    <td>
                      <StatusPill status={doc.status} />
                    </td>
                    <td>
                      <span className="mono-id">
                        {doc.page_count ?? '—'}
                      </span>
                    </td>
                    <td>
                      <span className="mono-id">
                        {formatBytes(doc.size_bytes)}
                      </span>
                    </td>
                    <td>
                      <span style={{ fontSize: '12px', color: 'var(--paper-400)' }}>
                        {formatDate(doc.created_at)}
                      </span>
                    </td>
                    <td>
                      <ChevronRight size={14} color="var(--paper-400)" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
