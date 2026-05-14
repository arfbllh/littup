'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { ArrowLeft } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { UploadDropzone } from '@/components/UploadDropzone';
import { Button } from '@/components/ui/button';
import type { UploadResponse } from '@/lib/types';

interface UploadResult {
  result: UploadResponse;
  filename: string;
}

export default function UploadPage() {
  const [uploads, setUploads] = useState<UploadResult[]>([]);

  function handleUploaded(result: UploadResponse, file: File) {
    setUploads((prev) => [...prev, { result, filename: file.name }]);
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader
        eyebrow="Documents"
        title="Upload Documents"
        actions={
          <Button variant="ghost" size="sm" icon={ArrowLeft} onClick={() => history.back()}>
            Back to documents
          </Button>
        }
      />

      <div style={{ flex: 1, overflow: 'auto', padding: '24px', maxWidth: '720px' }}>
        <UploadDropzone onUploaded={handleUploaded} />

        {uploads.length > 0 && (
          <div
            style={{
              marginTop: '24px',
              backgroundColor: '#fff',
              border: '1px solid var(--paper-300)',
              borderRadius: 'var(--radius-card)',
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                padding: '12px 16px',
                borderBottom: '1px solid var(--paper-300)',
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--paper-500)',
                textTransform: 'uppercase',
                letterSpacing: '0.06em',
              }}
            >
              Uploaded Files
            </div>
            {uploads.map((u, i) => (
              <div
                key={i}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '10px 16px',
                  borderBottom: i < uploads.length - 1 ? '1px solid var(--paper-200)' : 'none',
                }}
              >
                <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  <span style={{ fontSize: '13px', color: 'var(--paper-700)' }}>{u.filename}</span>
                  <span className="mono-id" style={{ fontSize: '11px' }}>
                    {u.result.was_new ? 'New document' : 'Duplicate (already indexed)'} · sha256:{u.result.sha256.slice(0, 12)}
                  </span>
                </div>
                <Link
                  href={`/documents/${u.result.document_id}`}
                  style={{
                    fontSize: '12px',
                    color: 'var(--ochre-500)',
                    textDecoration: 'none',
                    fontFamily: 'var(--font-sans)',
                    fontWeight: 500,
                  }}
                >
                  View document
                </Link>
              </div>
            ))}
          </div>
        )}

        <div style={{ marginTop: '16px' }}>
          <Link
            href="/documents"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
              fontSize: '13px',
              color: 'var(--paper-500)',
              textDecoration: 'none',
              fontFamily: 'var(--font-sans)',
            }}
          >
            <ArrowLeft size={13} />
            Back to documents list
          </Link>
        </div>
      </div>
    </div>
  );
}
