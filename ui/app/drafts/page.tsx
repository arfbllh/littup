'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';
import { ChevronRight, Plus, Trash2 } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { StatusPill } from '@/components/ui/status-pill';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { listDrafts, deleteDraft } from '@/lib/api';
import type { DraftSummary } from '@/lib/types';

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'short',
    timeStyle: 'short',
  });
}

function formatCost(usd: number | null): string {
  if (usd == null) return '—';
  return `$${usd.toFixed(4)}`;
}

function formatGroundedness(g: number | null): string {
  if (g == null) return '—';
  return `${Math.round(g * 100)}%`;
}

export default function DraftsPage() {
  const router = useRouter();
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DraftSummary | null>(null);

  const { data, error, isLoading, mutate } = useSWR(
    'drafts',
    () => listDrafts(),
    {
      refreshInterval: (latest) => {
        const anyActive = (latest?.items ?? []).some(
          (d) => d.status !== 'ready' && d.status !== 'failed' && d.status !== 'edited'
        );
        return anyActive ? 2_000 : 10_000;
      },
    }
  );

  const drafts: DraftSummary[] = data?.items ?? [];

  function requestDelete(draft: DraftSummary, event: React.MouseEvent) {
    event.stopPropagation();
    if (deletingId) return;
    setDeleteTarget(draft);
  }

  async function confirmDelete() {
    const draft = deleteTarget;
    if (!draft) return;
    setDeletingId(draft.draft_id);
    await mutate(
      (curr) =>
        curr && { ...curr, items: curr.items.filter((d) => d.draft_id !== draft.draft_id) },
      { revalidate: false }
    );
    try {
      await deleteDraft(draft.draft_id);
      await mutate();
      setDeleteTarget(null);
    } catch (err) {
      await mutate();
      // eslint-disable-next-line no-alert
      alert(`Delete failed: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader
        eyebrow="Workspace"
        title="Drafts"
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
              {drafts.length} draft{drafts.length !== 1 ? 's' : ''}
            </span>
            <Button
              variant="primary"
              size="sm"
              icon={Plus}
              onClick={() => router.push('/drafts/new')}
            >
              New draft
            </Button>
          </>
        }
      />

      <div style={{ flex: 1, overflow: 'auto', padding: '16px 24px' }}>
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
              Loading drafts...
            </div>
          )}

          {error && (
            <div style={{ padding: '32px', textAlign: 'center', color: 'var(--cite-unsupported)', fontSize: '13px' }}>
              Failed to load drafts. Check API connection.
            </div>
          )}

          {!isLoading && !error && drafts.length === 0 && (
            <div style={{ padding: '48px', textAlign: 'center', color: 'var(--paper-400)', fontSize: '13px' }}>
              No drafts yet. Create one to get started.
            </div>
          )}

          {!isLoading && drafts.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Draft</th>
                  <th>Status</th>
                  <th>Docs</th>
                  <th>Grounded</th>
                  <th>Cost</th>
                  <th>Edits</th>
                  <th>Created</th>
                  <th style={{ width: '32px' }}></th>
                </tr>
              </thead>
              <tbody>
                {drafts.map((d) => (
                  <tr
                    key={d.draft_id}
                    style={{ cursor: 'pointer' }}
                    onClick={() => router.push(`/drafts/${d.draft_id}`)}
                  >
                    <td>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                        <span style={{ color: 'var(--paper-700)', fontWeight: 500, fontSize: '13px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                          {d.template_id} <span style={{ color: 'var(--paper-400)', fontWeight: 400 }}>v{d.template_version}</span>
                          {d.has_extra_instructions && (
                            <span
                              title="Has custom operator instructions"
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
                              custom prompt
                            </span>
                          )}
                        </span>
                        <span className="mono-id" style={{ fontSize: '11px' }}>
                          {d.draft_id.slice(0, 8)}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <StatusPill status={d.status} />
                        {d.status === 'failed' && d.error_code && (
                          <span
                            className="mono-id"
                            style={{ fontSize: '10px', color: 'var(--cite-unsupported)' }}
                          >
                            {d.error_code}
                          </span>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          icon={Trash2}
                          onClick={(e) => requestDelete(d, e)}
                          disabled={deletingId === d.draft_id}
                          aria-label={`Delete draft ${d.draft_id.slice(0, 8)}`}
                        >
                          {deletingId === d.draft_id ? 'Deleting…' : ''}
                        </Button>
                      </div>
                    </td>
                    <td>
                      <span className="mono-id">{d.document_count}</span>
                    </td>
                    <td>
                      <span className="mono-id">{formatGroundedness(d.groundedness_score)}</span>
                    </td>
                    <td>
                      <span className="mono-id">{formatCost(d.cost_usd)}</span>
                    </td>
                    <td>
                      <span className="mono-id">{d.edit_count}</span>
                    </td>
                    <td>
                      <span style={{ fontSize: '12px', color: 'var(--paper-400)' }}>
                        {formatDate(d.generated_at ?? d.created_at)}
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

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete draft?"
        message={
          deleteTarget && (
            <>
              Draft <strong style={{ color: 'var(--paper-700)' }}>{deleteTarget.draft_id.slice(0, 8)}</strong>
              {' '}({deleteTarget.template_id} v{deleteTarget.template_version}) and all its sections,
              citations, and edits will be removed. This cannot be undone.
            </>
          )
        }
        confirmLabel="Delete"
        variant="danger"
        icon={Trash2}
        busy={deletingId !== null}
        onConfirm={confirmDelete}
        onCancel={() => {
          if (deletingId) return;
          setDeleteTarget(null);
        }}
      />
    </div>
  );
}
