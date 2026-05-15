'use client';

import React, { useState, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import useSWR from 'swr';
import { ArrowLeft, RotateCcw, MessageSquareQuote, GitBranchPlus } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { StatusPill } from '@/components/ui/status-pill';
import { CitationChip } from '@/components/ui/citation-chip';
import { Spinner } from '@/components/ui/spinner';
import { CitedText } from '@/components/CitedText';
import { PageWithBboxes } from '@/components/PageWithBboxes';
import { getDraft, saveEdit, regenerateSection, getEditMetrics, getTemplateVersions, revalidateDraft } from '@/lib/api';
import type { DraftResponse, CitationView, SectionView, TemplateVersionsResponse, EditMetricsResponse } from '@/lib/types';

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
    contradicted: 'var(--cite-unsupported)',
    unchecked: 'var(--cite-unchecked)',
    stale: 'var(--cite-partial)',
  }[citation.validation_status];

  const statusBg = {
    supported: 'var(--cite-supported-bg)',
    partial: 'var(--cite-partial-bg)',
    unsupported: 'var(--cite-unsupported-bg)',
    contradicted: 'var(--cite-unsupported-bg)',
    unchecked: 'var(--cite-unchecked-bg)',
    stale: 'var(--cite-partial-bg)',
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
  canRegenerate: boolean;
  editedCount?: number;
  windowDays?: number;
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
  canRegenerate,
  editedCount,
  windowDays,
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
          {editedCount != null && editedCount > 0 && (
            <span
              title={`Edited ${editedCount}× across drafts in the last ${windowDays ?? 30} days.`}
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
              edited {editedCount}× / {windowDays ?? 30}d
            </span>
          )}
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
            disabled={regenerating || !canRegenerate}
            title={
              !canRegenerate
                ? 'Regenerate is available once the draft is ready or edited.'
                : 'Regenerate this section'
            }
            style={{
              background: 'none',
              border: '1px dashed var(--paper-300)',
              borderRadius: '4px',
              padding: '2px 8px',
              cursor:
                regenerating ? 'wait' : !canRegenerate ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              fontSize: '11px',
              color: !canRegenerate ? 'var(--paper-300)' : 'var(--paper-400)',
              fontFamily: 'var(--font-sans)',
              opacity: !canRegenerate ? 0.6 : 1,
            }}
          >
            {regenerating ? <Spinner size={10} /> : <RotateCcw size={10} />}
            Regenerate
          </button>
        </div>
      </div>

      <div style={{ position: 'relative' }}>
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
          <div
            className="prose-doc"
            style={{
              filter: regenerating ? 'blur(2px)' : undefined,
              transition: 'filter 120ms',
              pointerEvents: regenerating ? 'none' : undefined,
            }}
          >
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
        {regenerating && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '8px',
              backgroundColor: 'rgba(255,255,255,0.55)',
              borderRadius: 'var(--radius-card)',
            }}
          >
            <Spinner size={14} color="var(--ochre-500)" />
            <span style={{ fontSize: '12px', color: 'var(--ochre-600)' }}>
              Regenerating section…
            </span>
          </div>
        )}
      </div>
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
  const [showVersionsModal, setShowVersionsModal] = useState(false);
  const [revalidating, setRevalidating] = useState(false);

  const { data: draft, mutate, isLoading } = useSWR<DraftResponse>(
    id ? `draft:${id}` : null,
    () => getDraft(id),
    // Callback form — referencing the just-being-declared `draft` directly here
    // is a TDZ ReferenceError. SWR passes the latest data into this callback.
    { refreshInterval: (latest) => (latest?.status === 'generating' ? 3000 : 0) }
  );

  // D1: edit-metrics for the section badges. Pulled once on mount per draft;
  // window_days defaults server-side, so we don't need to thread it.
  const { data: editMetrics } = useSWR<EditMetricsResponse>(
    draft?.template_id ? `edit-metrics:${draft.template_id}` : null,
    () => getEditMetrics(draft!.template_id),
  );
  const sectionEditedMap = React.useMemo(() => {
    const map: Record<string, number> = {};
    for (const row of editMetrics?.sections ?? []) {
      map[row.name] = row.edited_count;
    }
    return map;
  }, [editMetrics]);

  // D3: lazy-load template versions only when the modal opens.
  const { data: templateVersions } = useSWR<TemplateVersionsResponse>(
    showVersionsModal && draft?.template_id
      ? `template-versions:${draft.template_id}`
      : null,
    () => getTemplateVersions(draft!.template_id),
  );

  function enterEditMode() {
    if (!draft) return;
    const fe: Record<string, string> = {};
    if (draft.fields) {
      for (const [k, v] of Object.entries(draft.fields)) {
        const raw = v != null && typeof v === 'object' && 'value' in (v as object)
          ? (v as Record<string, unknown>).value
          : v;
        fe[k] = typeof raw === 'string' ? raw : JSON.stringify(raw);
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
      // Build final_output: unwrap {value,...} objects to plain values, then apply edits
      const fields: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(draft.fields ?? {})) {
        fields[k] = v != null && typeof v === 'object' && 'value' in (v as object)
          ? (v as Record<string, unknown>).value
          : v;
      }
      for (const [k, v] of Object.entries(fieldEdits)) {
        fields[k] = v;
      }
      const sections = draft.sections.map((sec) => ({
        name: sec.name,
        text: sectionEdits[sec.name] ?? sec.text ?? '',
      }));
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
    if (draft.status !== 'ready' && draft.status !== 'edited') {
      alert(`Cannot regenerate while draft is ${draft.status}.`);
      return;
    }
    // Edit-aware UX: if the operator has unsaved edits in this section and the
    // textarea diverges from the persisted text, confirm before regenerating —
    // we have no place to write that draft text once the section is replaced.
    if (editMode) {
      const persisted = draft.sections.find((s) => s.name === sectionName)?.text ?? '';
      const pending = sectionEdits[sectionName] ?? persisted;
      if (pending !== persisted) {
        const ok = window.confirm(
          'Regenerating will discard your in-progress edit for this section. Continue?',
        );
        if (!ok) return;
      }
    }
    setRegeneratingSection(sectionName);
    try {
      const updated = await regenerateSection(draft.draft_id, sectionName);
      // In-place: splice the new SectionView into the cached draft so the rest
      // of the UI (selected citation, etc.) doesn't flash.
      mutate(
        (prev) =>
          prev
            ? {
                ...prev,
                sections: prev.sections.map((s) =>
                  s.name === sectionName ? updated : s,
                ),
              }
            : prev,
        { revalidate: true },
      );
      // Clear any stale pending edit for this section so the textarea doesn't
      // re-paint over the freshly generated text.
      setSectionEdits((prev) => {
        const { [sectionName]: _drop, ...rest } = prev;
        return rest;
      });
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Regeneration failed');
      mutate();
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
              <button
                type="button"
                onClick={() => setShowVersionsModal(true)}
                className="mono-id"
                title="Show template version history"
                style={{
                  fontSize: '11px',
                  background: 'none',
                  border: 'none',
                  padding: 0,
                  cursor: 'pointer',
                  textDecoration: 'underline dotted',
                  color: 'var(--paper-500)',
                }}
              >
                tpl:{draft.template_id} v{draft.template_version}
              </button>
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
          {(() => {
            const staleCount = draft.sections.reduce(
              (acc, s) => acc + s.citations.filter((c) => c.validation_status === 'stale').length,
              0,
            );
            if (staleCount === 0) return null;
            return (
              <Button
                variant="secondary"
                size="sm"
                disabled={revalidating}
                onClick={async () => {
                  setRevalidating(true);
                  try {
                    await revalidateDraft(draft.draft_id);
                    mutate();
                  } catch (err) {
                    alert(err instanceof Error ? err.message : 'Revalidate failed');
                  } finally {
                    setRevalidating(false);
                  }
                }}
              >
                {revalidating ? 'Re-validating…' : `Re-validate (${staleCount})`}
              </Button>
            );
          })()}
          {!editMode && (draft.status === 'ready' || draft.status === 'edited' || draft.status === 'failed') && (
            <Button
              variant="ghost"
              size="sm"
              icon={GitBranchPlus}
              title="Open a new draft pre-filled with this draft's inputs (creates a new draft; original is preserved)."
              onClick={() => {
                const params = new URLSearchParams();
                params.set('template_id', draft.template_id);
                if (draft.document_ids && draft.document_ids.length > 0) {
                  params.set('document_ids', draft.document_ids.join(','));
                }
                if (draft.extra_instructions) {
                  params.set('extra_instructions', draft.extra_instructions);
                }
                router.push(`/drafts/new?${params.toString()}`);
              }}
            >
              Regenerate with changes
            </Button>
          )}
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

          {draft.extra_instructions && (
            <details
              style={{
                marginBottom: '20px',
                padding: '10px 14px',
                backgroundColor: 'var(--ochre-50)',
                border: '1px solid var(--ochre-100)',
                borderRadius: 'var(--radius-card)',
              }}
            >
              <summary
                style={{
                  fontSize: '12px',
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--ochre-600)',
                  cursor: 'pointer',
                  fontWeight: 600,
                }}
              >
                Custom instructions ({draft.extra_instructions.length} chars)
              </summary>
              <p
                style={{
                  margin: '6px 0 0',
                  fontSize: '13px',
                  color: 'var(--paper-700)',
                  whiteSpace: 'pre-wrap',
                  lineHeight: 1.5,
                }}
              >
                {draft.extra_instructions}
              </p>
            </details>
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
                        {(() => {
                          const v = value != null && typeof value === 'object' && 'value' in (value as object)
                            ? (value as Record<string, unknown>).value
                            : value;
                          return typeof v === 'string' ? v : JSON.stringify(v);
                        })()}
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
              canRegenerate={draft.status === 'ready' || draft.status === 'edited'}
              editedCount={sectionEditedMap[section.name]}
              windowDays={editMetrics?.window_days}
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

        {showVersionsModal && (
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Template versions"
            onClick={() => setShowVersionsModal(false)}
            style={{
              position: 'fixed',
              inset: 0,
              zIndex: 50,
              backgroundColor: 'rgba(20, 20, 20, 0.32)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: '24px',
            }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                width: 'min(720px, 92vw)',
                maxHeight: '80vh',
                overflow: 'auto',
                backgroundColor: '#fff',
                borderRadius: 'var(--radius-card)',
                border: '1px solid var(--paper-300)',
                padding: '20px',
                boxShadow: '0 8px 32px rgba(0,0,0,0.18)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
                <div>
                  <div className="eyebrow">Template versions</div>
                  <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--paper-700)' }}>
                    {draft.template_id}
                  </div>
                </div>
                <button
                  onClick={() => setShowVersionsModal(false)}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--paper-400)', fontSize: '18px' }}
                  aria-label="Close versions dialog"
                >
                  ✕
                </button>
              </div>
              {!templateVersions && (
                <div style={{ padding: '24px', textAlign: 'center', color: 'var(--paper-400)' }}>
                  Loading…
                </div>
              )}
              {templateVersions && templateVersions.versions.length === 0 && (
                <div style={{ padding: '24px', textAlign: 'center', color: 'var(--paper-400)' }}>
                  No version history yet.
                </div>
              )}
              {templateVersions?.versions.map((v) => (
                <div
                  key={`${v.version}-${v.prompt_fingerprint}`}
                  style={{
                    padding: '10px 0',
                    borderTop: '1px solid var(--paper-200)',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px', flexWrap: 'wrap' }}>
                    <strong style={{ fontSize: '13px' }}>v{v.version}</strong>
                    <span className="mono-id" style={{ fontSize: '11px' }}>
                      fp:{v.prompt_fingerprint.slice(0, 12)}
                    </span>
                    {v.version === draft.template_version && (
                      <span
                        style={{
                          fontSize: '10px',
                          fontFamily: 'var(--font-mono)',
                          color: 'var(--ochre-600)',
                          padding: '0 6px',
                          backgroundColor: 'var(--ochre-50)',
                          borderRadius: 'var(--radius-pill)',
                          border: '1px solid var(--ochre-100)',
                        }}
                      >
                        this draft
                      </span>
                    )}
                    <span style={{ fontSize: '11px', color: 'var(--paper-400)' }}>
                      {new Date(v.created_at).toLocaleString()}
                    </span>
                  </div>
                  {v.rules_added_vs_previous.length > 0 && (
                    <ul style={{ marginTop: '6px', paddingLeft: '18px', color: 'var(--paper-600)', fontSize: '12px' }}>
                      {v.rules_added_vs_previous.map((rule, i) => (
                        <li key={i}>{rule}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

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
