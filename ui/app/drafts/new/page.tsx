'use client';

import React, { useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import useSWR from 'swr';
import { FileText } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/button';
import { StatusPill } from '@/components/ui/status-pill';
import { Spinner } from '@/components/ui/spinner';
import { listTemplates, listDocuments, createDraft } from '@/lib/api';
import type { TemplateInfo, DocumentSummary } from '@/lib/types';

const EXTRA_INSTRUCTIONS_MAX = 2000;

export default function NewDraftPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [selectedTemplate, setSelectedTemplate] = useState<string | null>(null);
  const [selectedDocs, setSelectedDocs] = useState<Set<string>>(new Set());
  const [extraInstructions, setExtraInstructions] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Prefill from "Regenerate with changes" on an existing draft.
  // We only prefill once on mount — the user is free to mutate from there.
  useEffect(() => {
    const tpl = searchParams.get('template_id');
    const docs = searchParams.get('document_ids');
    const extra = searchParams.get('extra_instructions');
    if (tpl) setSelectedTemplate(tpl);
    if (docs) setSelectedDocs(new Set(docs.split(',').filter(Boolean)));
    if (extra) setExtraInstructions(extra.slice(0, EXTRA_INSTRUCTIONS_MAX));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const { data: templates, isLoading: templatesLoading } = useSWR('templates', listTemplates);
  const { data: docsData, isLoading: docsLoading } = useSWR(
    'documents',
    () => listDocuments(),
    {
      // Same cadence as the documents page so the picker doesn't lag behind
      // when a doc finishes processing.
      refreshInterval: (latest) => {
        const anyActive = (latest?.items ?? []).some(
          (d) => d.status !== 'ready' && d.status !== 'failed'
        );
        return anyActive ? 2_000 : 10_000;
      },
    }
  );
  const readyDocs = (docsData?.items ?? []).filter((d) => d.status === 'ready');

  function toggleDoc(id: string) {
    setSelectedDocs((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handleGenerate() {
    if (!selectedTemplate || selectedDocs.size === 0) return;
    setIsGenerating(true);
    setError(null);
    try {
      const trimmed = extraInstructions.trim();
      const result = await createDraft({
        template_id: selectedTemplate,
        document_ids: Array.from(selectedDocs),
        extra_instructions: trimmed.length > 0 ? trimmed : null,
      });
      router.push(`/drafts/${result.draft_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create draft');
      setIsGenerating(false);
    }
  }

  const canGenerate = !!selectedTemplate && selectedDocs.size > 0 && !isGenerating;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader
        eyebrow="Drafts"
        title="Generate a draft"
        actions={
          <Button
            variant="primary"
            size="md"
            onClick={handleGenerate}
            disabled={!canGenerate}
            loading={isGenerating}
          >
            {isGenerating ? 'Generating...' : 'Generate draft'}
          </Button>
        }
      />

      <div style={{ flex: 1, overflow: 'auto', padding: '24px', display: 'flex', flexDirection: 'column', gap: '24px' }}>
        {error && (
          <div
            style={{
              padding: '12px 16px',
              backgroundColor: 'var(--cite-unsupported-bg)',
              border: '1px solid var(--cite-unsupported)',
              borderRadius: 'var(--radius-card)',
              color: 'var(--cite-unsupported)',
              fontSize: '13px',
            }}
          >
            {error}
          </div>
        )}

        {/* Template picker */}
        <div>
          <div className="eyebrow" style={{ marginBottom: '12px' }}>
            1. Select Template
          </div>

          {templatesLoading && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--paper-400)', fontSize: '13px' }}>
              <Spinner size={14} /> Loading templates...
            </div>
          )}

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(3, 1fr)',
              gap: '12px',
            }}
          >
            {(templates ?? []).map((tpl: TemplateInfo) => {
              const isSelected = selectedTemplate === tpl.id;
              return (
                <div
                  key={tpl.id}
                  onClick={() => setSelectedTemplate(tpl.id)}
                  style={{
                    backgroundColor: '#fff',
                    border: `2px solid ${isSelected ? 'var(--ochre-500)' : 'var(--paper-300)'}`,
                    borderRadius: 'var(--radius-card)',
                    padding: '16px',
                    cursor: 'pointer',
                    opacity: 1,
                    transition: 'border-color 120ms',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '8px' }}>
                    <FileText size={16} color={isSelected ? 'var(--ochre-500)' : 'var(--paper-400)'} />
                    {isSelected && (
                      <span
                        style={{
                          width: '14px',
                          height: '14px',
                          borderRadius: '50%',
                          backgroundColor: 'var(--ochre-500)',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          flexShrink: 0,
                        }}
                      >
                        <span style={{ width: '6px', height: '6px', borderRadius: '50%', backgroundColor: '#fff' }} />
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--paper-700)', marginBottom: '4px' }}>
                    {tpl.display_name}
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--paper-400)', marginBottom: '10px', lineHeight: 1.4 }}>
                    {tpl.description}
                  </div>
                  <div style={{ display: 'flex', gap: '10px' }}>
                    <span className="mono-id" style={{ fontSize: '11px' }}>v{tpl.latest_version}</span>
                    <span className="mono-id" style={{ fontSize: '11px' }}>fp:{tpl.fingerprint.slice(0, 8)}</span>
                  </div>
                </div>
              );
            })}

            {!templatesLoading && (templates ?? []).length === 0 && (
              <div style={{ gridColumn: '1/-1', padding: '24px', color: 'var(--paper-400)', fontSize: '13px' }}>
                No templates found. Add YAML files to config/templates/.
              </div>
            )}
          </div>
        </div>

        {/* Document selector */}
        <div>
          <div className="eyebrow" style={{ marginBottom: '12px' }}>
            2. Select Documents
          </div>

          {docsLoading && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--paper-400)', fontSize: '13px' }}>
              <Spinner size={14} /> Loading documents...
            </div>
          )}

          {!docsLoading && readyDocs.length === 0 && (
            <div
              style={{
                padding: '24px',
                backgroundColor: 'var(--paper-100)',
                borderRadius: 'var(--radius-card)',
                border: '1px solid var(--paper-300)',
                color: 'var(--paper-400)',
                fontSize: '13px',
              }}
            >
              No ready documents found. Upload and process documents first.
            </div>
          )}

          {readyDocs.length > 0 && (
            <div
              style={{
                backgroundColor: '#fff',
                border: '1px solid var(--paper-300)',
                borderRadius: 'var(--radius-card)',
                overflow: 'hidden',
              }}
            >
              {readyDocs.map((doc: DocumentSummary, i: number) => {
                const checked = selectedDocs.has(doc.document_id);
                return (
                  <div
                    key={doc.document_id}
                    onClick={() => toggleDoc(doc.document_id)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '12px',
                      padding: '10px 16px',
                      borderBottom: i < readyDocs.length - 1 ? '1px solid var(--paper-200)' : 'none',
                      cursor: 'pointer',
                      backgroundColor: checked ? 'var(--ochre-50)' : 'transparent',
                      transition: 'background-color 100ms',
                    }}
                  >
                    {/* Checkbox */}
                    <span
                      style={{
                        width: '16px',
                        height: '16px',
                        borderRadius: '3px',
                        border: `2px solid ${checked ? 'var(--ochre-500)' : 'var(--paper-300)'}`,
                        backgroundColor: checked ? 'var(--ochre-500)' : '#fff',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        flexShrink: 0,
                        transition: 'all 120ms',
                      }}
                    >
                      {checked && (
                        <svg width="9" height="7" viewBox="0 0 9 7" fill="none">
                          <path d="M1 3l2.5 2.5L8 1" stroke="#fff" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      )}
                    </span>

                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '13px', color: 'var(--paper-700)', fontWeight: 500 }}>
                        {doc.filename}
                      </div>
                      <div style={{ display: 'flex', gap: '10px', marginTop: '2px' }}>
                        <span className="mono-id" style={{ fontSize: '11px' }}>
                          {doc.document_id.slice(0, 12)}
                        </span>
                        {doc.page_count && (
                          <span className="mono-id" style={{ fontSize: '11px' }}>
                            {doc.page_count}p
                          </span>
                        )}
                      </div>
                    </div>

                    <StatusPill status={doc.status} />
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Custom instructions */}
        <div>
          <div className="eyebrow" style={{ marginBottom: '12px' }}>
            3. Custom instructions <span style={{ color: 'var(--paper-400)', textTransform: 'none' }}>(optional)</span>
          </div>
          <textarea
            value={extraInstructions}
            onChange={(e) => setExtraInstructions(e.target.value.slice(0, EXTRA_INSTRUCTIONS_MAX))}
            placeholder="Focus on the indemnification clause and ignore Schedule B…"
            disabled={isGenerating}
            style={{
              width: '100%',
              minHeight: '90px',
              padding: '10px 12px',
              fontFamily: 'var(--font-sans)',
              fontSize: '13px',
              lineHeight: 1.5,
              color: 'var(--paper-700)',
              border: '1px solid var(--paper-300)',
              borderRadius: 'var(--radius-input)',
              resize: 'vertical',
              outline: 'none',
              backgroundColor: '#fff',
            }}
          />
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              marginTop: '4px',
              fontSize: '11px',
              color: 'var(--paper-400)',
              fontFamily: 'var(--font-mono)',
            }}
          >
            <span>
              Folded into the prompt fingerprint — two drafts with different
              instructions are independent.
            </span>
            <span>
              {extraInstructions.length} / {EXTRA_INSTRUCTIONS_MAX}
            </span>
          </div>
        </div>

        {/* Generate CTA (bottom) */}
        {(selectedTemplate || selectedDocs.size > 0) && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '12px 16px',
              backgroundColor: 'var(--ochre-50)',
              borderRadius: 'var(--radius-card)',
              border: '1px solid var(--ochre-100)',
            }}
          >
            <span style={{ fontSize: '13px', color: 'var(--paper-600)' }}>
              {selectedTemplate ? (templates ?? []).find((t: TemplateInfo) => t.id === selectedTemplate)?.display_name ?? 'Template selected' : 'No template selected'}
              {selectedDocs.size > 0 && ` · ${selectedDocs.size} doc${selectedDocs.size > 1 ? 's' : ''} selected`}
            </span>
            <Button
              variant="primary"
              size="md"
              onClick={handleGenerate}
              disabled={!canGenerate}
              loading={isGenerating}
            >
              {isGenerating ? 'Generating...' : 'Generate draft'}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
