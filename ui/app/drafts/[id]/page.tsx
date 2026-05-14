'use client';

import React, { useState, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import useSWR from 'swr';
import { ArrowLeft, RotateCcw, MessageSquareQuote } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { StatusPill } from '@/components/ui/status-pill';
import { CitationChip } from '@/components/ui/citation-chip';
import { Spinner } from '@/components/ui/spinner';
import { CitedText } from '@/components/CitedText';
import { PageWithBboxes } from '@/components/PageWithBboxes';
import { getDraft, saveEdit, regenerateSection } from '@/lib/api';
import type { DraftResponse, CitationView, SectionView } from '@/lib/types';

function GroundednessBar({ score }: { score: number | null }) {
  if (score == null) return null;
  const pct = Math.round(score * 100);
  const segments = 10;
  const filled = Math.round(score * segments);

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <span className="eyebrow">Groundedness</span>
      <div style={{ display: 'flex', gap: '2px' }}>
        {Array.from({ length: segments }).map((_, i) => (
          <span
            key={i}
            style={{
              width: '14px',
              height: '8px',
              borderRadius: '2px',
              backgroundColor:
                i < filled
                  ? pct >= 80
                    ? 'var(--cite-supported)'
                    : pct >= 50
                    ? 'var(--cite-partial)'
                    : 'var(--cite-unsupported)'
                  : 'var(--paper-200)',
            }}
          />
        ))}
      </div>
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--paper-500)' }}>
        {pct}%
      </span>
    </div>
  );
}

interface CitationPanelProps {
  citation: CitationView;
  documentIds: string[];
  onClose: () => void;
}

function CitationPanel({ citation, documentIds, onClose }: CitationPanelProps) {
  const docId = documentIds[0] ?? '';

  const statusColor = {
    supported: 'var(--cite-supported)',
    partial: 'var(--cite-partial)',
    unsupported: 'var(--cite-unsupported)',
    unchecked: 'var(--cite-unchecked)',
  }[citation.validation_status];

  const statusBg = {
    supported: 'var(--cite-supported-bg)',
    partial: 'var(--cite-partial-bg)',
    unsupported: 'var(--cite-unsupported-bg)',
    unchecked: 'var(--cite-unchecked-bg)',
  }[citation.validation_status];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Header */}
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--paper-300)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span className="eyebrow">Source</span>
          <CitationChip
            chunkId={citation.chunk_id}
            status={citation.validation_status}
            selected
          />
        </div>
        <button
          onClick={onClose}
          style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--paper-400)' }}
        >
          ✕
        </button>
      </div>

      <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
        {/* Mini page preview */}
        {docId && (
          <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--paper-200)' }}>
            <PageWithBboxes
              documentId={docId}
              page={1}
              style={{ width: '100%', maxHeight: '200px', overflow: 'hidden' }}
            />
          </div>
        )}

        {/* Chunk ID + metadata */}
        <div
          style={{
            padding: '10px 16px',
            borderBottom: '1px solid var(--paper-200)',
          }}
        >
          <div className="mono-id" style={{ fontSize: '11px', wordBreak: 'break-all' }}>
            chunk:{citation.chunk_id}
          </div>
        </div>

        {/* Validation note */}
        {citation.validation_reason && (
          <div
            style={{
              margin: '12px 16px',
              padding: '10px 12px',
              backgroundColor: statusBg,
              borderRadius: 'var(--radius-card)',
              border: `1px solid ${statusColor}33`,
            }}
          >
            <div style={{ fontSize: '11px', color: statusColor, fontWeight: 600, marginBottom: '4px' }}>
              {citation.validation_status.toUpperCase()}
            </div>
            <p style={{ margin: 0, fontSize: '12px', color: 'var(--paper-600)', lineHeight: 1.5 }}>
              {citation.validation_reason}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

interface SectionEditorProps {
  section: SectionView;
  editMode: boolean;
  editValue: string;
  onEdit: (v: string) => void;
  selectedChunkId: string | null;
  onCitationClick: (c: CitationView) => void;
  onRegenerate: (name: string) => void;
  regenerating: boolean;
}

function SectionEditor({
  section,
  editMode,
  editValue,
  onEdit,
  selectedChunkId,
  onCitationClick,
  onRegenerate,
  regenerating,
}: SectionEditorProps) {
  return (
    <div style={{ marginBottom: '24px' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: '8px',
        }}
      >
        <h3
          style={{
            margin: 0,
            fontSize: '13px',
            fontWeight: 700,
            color: 'var(--paper-700)',
            fontFamily: 'var(--font-sans)',
            textTransform: 'capitalize',
          }}
        >
          {section.name.replace(/_/g, ' ')}
        </h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {section.groundedness != null && (
            <span
              style={{
                fontSize: '11px',
                fontFamily: 'var(--font-mono)',
                color: 'var(--paper-400)',
              }}
            >
              {Math.round(section.groundedness * 100)}% grounded
            </span>
          )}
          <button
            onClick={() => onRegenerate(section.name)}
            disabled={regenerating}
            style={{
              background: 'none',
              border: '1px dashed var(--paper-300)',
              borderRadius: '4px',
              padding: '2px 8px',
              cursor: regenerating ? 'wait' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              fontSize: '11px',
              color: 'var(--paper-400)',
              fontFamily: 'var(--font-sans)',
            }}
          >
            {regenerating ? <Spinner size={10} /> : <RotateCcw size={10} />}
            Regenerate
          </button>
        </div>
      </div>

      {editMode ? (
        <textarea
          value={editValue}
          onChange={(e) => onEdit(e.target.value)}
          style={{
            width: '100%',
            minHeight: '160px',
            padding: '10px 12px',
            fontFamily: 'var(--font-serif)',
            fontSize: '15px',
            lineHeight: 1.65,
            color: 'var(--paper-700)',
            border: '1px solid var(--paper-300)',
            borderRadius: 'var(--radius-input)',
            resize: 'vertical',
            outline: 'none',
            backgroundColor: '#fff',
          }}
          onFocus={(e) => {
            e.target.style.borderColor = 'var(--ochre-500)';
            e.target.style.boxShadow = '0 0 0 2px var(--ochre-100)';
          }}
          onBlur={(e) => {
            e.target.style.borderColor = 'var(--paper-300)';
            e.target.style.boxShadow = 'none';
          }}
        />
      ) : (
        <div className="prose-doc">
          {section.text ? (
            <CitedText
              text={section.text}
              citations={section.citations}
              selectedChunkId={selectedChunkId}
              onCitationClick={onCitationClick}
            />
          ) : (
            <span style={{ color: 'var(--paper-400)', fontStyle: 'italic', fontSize: '14px' }}>
              No content generated for this section.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export default function DraftDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const [editMode, setEditMode] = useState(false);
  const [fieldEdits, setFieldEdits] = useState<Record<string, string>>({});
  const [sectionEdits, setSectionEdits] = useState<Record<string, string>>({});
  const [selectedCitation, setSelectedCitation] = useState<CitationView | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [regeneratingSection, setRegeneratingSection] = useState<string | null>(null);

  const { data: draft, mutate, isLoading } = useSWR<DraftResponse>(
    id ? `draft:${id}` : null,
    () => getDraft(id),
    { refreshInterval: draft?.status === 'generating' ? 3000 : 0 }
  );

  function enterEditMode() {
    if (!draft) return;
    const fe: Record<string, string> = {};
    if (draft.fields) {
      for (const [k, v] of Object.entries(draft.fields)) {
        fe[k] = typeof v === 'string' ? v : JSON.stringify(v);
      }
    }
    const se: Record<string, string> = {};
    for (const sec of draft.sections) {
      se[sec.name] = sec.text ?? '';
    }
    setFieldEdits(fe);
    setSectionEdits(se);
    setEditMode(true);
  }

  async function saveEdits() {
    if (!draft) return;
    setIsSaving(true);
    try {
      // Build final_output: all current fields + sections (edited values override originals)
      const fields: Record<string, unknown> = { ...(draft.fields ?? {}) };
      for (const [k, v] of Object.entries(fieldEdits)) {
        fields[k] = v;
      }
      const sections: Record<string, string> = {};
      for (const sec of draft.sections) {
        sections[sec.name] = sectionEdits[sec.name] ?? sec.text ?? '';
      }
      await saveEdit(draft.draft_id, { fields, sections });
      setEditMode(false);
      mutate();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setIsSaving(false);
    }
  }

  const handleCitationClick = useCallback((citation: CitationView) => {
    setSelectedCitation((prev) =>
      prev?.chunk_id === citation.chunk_id ? null : citation
    );
  }, []);

  async function handleRegenerate(sectionName: string) {
    if (!draft) return;
    setRegeneratingSection(sectionName);
    try {
      await regenerateSection(draft.draft_id, sectionName);
      mutate();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Regeneration failed');
    } finally {
      setRegeneratingSection(null);
    }
  }

  const documentIds = draft
    ? (draft.fields as Record<string, unknown> | null)?.document_ids as string[] | undefined ?? []
    : [];

  if (isLoading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', gap: '8px' }}>
        <Spinner />
        <span style={{ color: 'var(--paper-400)', fontSize: '13px' }}>Loading draft...</span>
      </div>
    );
  }

  if (!draft) {
    return (
      <div style={{ padding: '48px', textAlign: 'center', color: 'var(--paper-400)', fontSize: '13px' }}>
        Draft not found.
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Header */}
      <div
        style={{
          padding: '12px 24px',
          borderBottom: '1px solid var(--paper-300)',
          backgroundColor: '#fff',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexShrink: 0,
          flexWrap: 'wrap',
          gap: '12px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <Button variant="ghost" size="sm" icon={ArrowLeft} onClick={() => router.push('/drafts/new')}>
            Drafts
          </Button>
          <span style={{ color: 'var(--paper-300)' }}>|</span>
          <div>
            <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--paper-700)' }}>
              Draft
            </div>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '2px' }}>
              <span className="mono-id" style={{ fontSize: '11px' }}>{draft.draft_id.slice(0, 12)}</span>
              <span className="mono-id" style={{ fontSize: '11px' }}>tpl:{draft.template_id}</span>
              <span className="mono-id" style={{ fontSize: '11px' }}>fp:{draft.prompt_fingerprint.slice(0, 8)}</span>
              <StatusPill status={draft.status} />
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <GroundednessBar score={draft.groundedness_score} />
          {draft.cost_usd != null && (
            <span className="mono-id" style={{ fontSize: '11px' }}>
              ${draft.cost_usd.toFixed(4)}
            </span>
          )}
          {draft.edit_count > 0 && (
            <span
              style={{
                fontSize: '11px',
                fontFamily: 'var(--font-mono)',
                color: 'var(--ochre-600)',
                padding: '1px 6px',
                backgroundColor: 'var(--ochre-50)',
                borderRadius: 'var(--radius-pill)',
                border: '1px solid var(--ochre-100)',
              }}
            >
              {draft.edit_count} edits applied
            </span>
          )}
        </div>

        <div style={{ display: 'flex', gap: '8px' }}>
          {!editMode ? (
            <Button variant="secondary" size="sm" onClick={enterEditMode}>
              Edit
            </Button>
          ) : (
            <>
              <Button variant="ghost" size="sm" onClick={() => setEditMode(false)}>
                Cancel
              </Button>
              <Button variant="primary" size="sm" onClick={saveEdits} loading={isSaving}>
                Save edits
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Two-pane body */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {/* Left pane: draft content */}
        <div style={{ flex: 1, overflow: 'auto', padding: '24px', minWidth: 0 }}>
          {draft.status === 'generating' && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '12px 16px',
                backgroundColor: 'var(--ochre-50)',
                borderRadius: 'var(--radius-card)',
                border: '1px solid var(--ochre-100)',
                marginBottom: '20px',
              }}
            >
              <Spinner size={14} color="var(--ochre-500)" />
              <span style={{ fontSize: '13px', color: 'var(--ochre-600)' }}>
                Generating draft... this may take a minute.
              </span>
            </div>
          )}

          {draft.error && (
            <div
              style={{
                padding: '12px 16px',
                backgroundColor: 'var(--cite-unsupported-bg)',
                borderRadius: 'var(--radius-card)',
                border: '1px solid var(--cite-unsupported)',
                marginBottom: '20px',
                color: 'var(--cite-unsupported)',
                fontSize: '13px',
              }}
            >
              Error: {JSON.stringify(draft.error)}
            </div>
          )}

          {/* Fields */}
          {draft.fields && Object.keys(draft.fields).length > 0 && (
            <div
              style={{
                backgroundColor: 'var(--paper-50)',
                border: '1px solid var(--paper-300)',
                borderRadius: 'var(--radius-card)',
                padding: '16px',
                marginBottom: '24px',
              }}
            >
              <div className="eyebrow" style={{ marginBottom: '12px' }}>
                Extracted Fields
              </div>
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: '200px 1fr',
                  gap: '8px 16px',
                }}
              >
                {Object.entries(draft.fields).map(([key, value]) => (
                  <React.Fragment key={key}>
                    <div className="mono-id" style={{ fontSize: '12px', paddingTop: '4px' }}>
                      {key}
                    </div>
                    {editMode ? (
                      <input
                        value={fieldEdits[key] ?? ''}
                        onChange={(e) =>
                          setFieldEdits((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                        style={{
                          width: '100%',
                          padding: '4px 8px',
                          fontSize: '13px',
                          fontFamily: 'var(--font-sans)',
                          color: 'var(--paper-700)',
                          border: '1px solid var(--paper-300)',
                          borderRadius: 'var(--radius-input)',
                          backgroundColor: '#fff',
                          outline: 'none',
                        }}
                        onFocus={(e) => {
                          e.target.style.borderColor = 'var(--ochre-500)';
                          e.target.style.boxShadow = '0 0 0 2px var(--ochre-100)';
                        }}
                        onBlur={(e) => {
                          e.target.style.borderColor = 'var(--paper-300)';
                          e.target.style.boxShadow = 'none';
                        }}
                      />
                    ) : (
                      <div className="prose-doc" style={{ fontSize: '13px' }}>
                        {typeof value === 'string' ? value : JSON.stringify(value)}
                      </div>
                    )}
                  </React.Fragment>
                ))}
              </div>
            </div>
          )}

          {/* Sections */}
          {draft.sections.map((section: SectionView) => (
            <SectionEditor
              key={section.name}
              section={section}
              editMode={editMode}
              editValue={sectionEdits[section.name] ?? section.text ?? ''}
              onEdit={(v) => setSectionEdits((prev) => ({ ...prev, [section.name]: v }))}
              selectedChunkId={selectedCitation?.chunk_id ?? null}
              onCitationClick={handleCitationClick}
              onRegenerate={handleRegenerate}
              regenerating={regeneratingSection === section.name}
            />
          ))}

          {draft.edit_count > 0 && (
            <div
              style={{
                marginTop: '16px',
                padding: '12px 16px',
                border: '1px dashed var(--paper-300)',
                borderRadius: 'var(--radius-card)',
                fontSize: '12px',
                color: 'var(--paper-400)',
                fontFamily: 'var(--font-sans)',
              }}
            >
              {draft.edit_count} operator edit{draft.edit_count > 1 ? 's' : ''} have been applied to this draft via the few-shot store.
            </div>
          )}
        </div>

        {/* Right pane: citation viewer */}
        <div
          style={{
            width: '480px',
            flexShrink: 0,
            borderLeft: '1px solid var(--paper-300)',
            backgroundColor: 'var(--paper-100)',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {selectedCitation ? (
            <CitationPanel
              citation={selectedCitation}
              documentIds={documentIds}
              onClose={() => setSelectedCitation(null)}
            />
          ) : (
            <div
              style={{
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '12px',
                padding: '32px',
                textAlign: 'center',
              }}
            >
              <MessageSquareQuote size={28} color="var(--paper-300)" />
              <span style={{ fontSize: '13px', color: 'var(--paper-400)', fontFamily: 'var(--font-sans)', lineHeight: 1.5 }}>
                Click any citation chip to see the source span
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
