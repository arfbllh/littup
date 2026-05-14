'use client';

import React, { useState } from 'react';
import useSWR from 'swr';
import { RefreshCw, ChevronDown, ChevronRight } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { Card, CardHeader, CardBody } from '@/components/ui/card';
import { getLlmStats, runRuleExtractor, getAdminTemplates, getTemplateVersions } from '@/lib/api';
import type { LlmTierStat, AdminTemplateRow, TemplateVersionRow, RuleExtractorRunResponse } from '@/lib/types';

function StatCard({
  label,
  value,
  sub,
  progress,
  progressColor,
}: {
  label: string;
  value: string;
  sub?: string;
  progress?: number;
  progressColor?: string;
}) {
  return (
    <div
      style={{
        backgroundColor: '#fff',
        border: '1px solid var(--paper-300)',
        borderRadius: 'var(--radius-card)',
        padding: '16px',
      }}
    >
      <div className="eyebrow" style={{ marginBottom: '8px' }}>
        {label}
      </div>
      <div
        style={{
          fontSize: '22px',
          fontWeight: 700,
          color: 'var(--paper-800)',
          fontFamily: 'var(--font-mono)',
        }}
      >
        {value}
      </div>
      {sub && (
        <div
          style={{
            fontSize: '11px',
            color: 'var(--paper-400)',
            fontFamily: 'var(--font-mono)',
            marginTop: '4px',
          }}
        >
          {sub}
        </div>
      )}
      {progress != null && (
        <div
          style={{
            marginTop: '10px',
            height: '4px',
            backgroundColor: 'var(--paper-200)',
            borderRadius: '2px',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              height: '100%',
              width: `${Math.min(progress, 100)}%`,
              backgroundColor: progressColor ?? 'var(--ochre-500)',
              borderRadius: '2px',
              transition: 'width 400ms',
            }}
          />
        </div>
      )}
    </div>
  );
}

function TemplateRow({ template }: { template: AdminTemplateRow }) {
  const [open, setOpen] = useState(false);
  const { data: versionsData } = useSWR(
    open ? `tpl-versions-${template.template_id}` : null,
    () => getTemplateVersions(template.template_id),
  );
  const latestVersion = versionsData?.versions[0] as TemplateVersionRow | undefined;

  return (
    <div
      style={{
        backgroundColor: '#fff',
        border: '1px solid var(--paper-300)',
        borderRadius: 'var(--radius-card)',
        overflow: 'hidden',
      }}
    >
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 16px',
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {open ? (
            <ChevronDown size={14} color="var(--paper-400)" />
          ) : (
            <ChevronRight size={14} color="var(--paper-400)" />
          )}
          <span
            style={{
              fontSize: '13px',
              fontWeight: 600,
              color: 'var(--paper-700)',
              fontFamily: 'var(--font-mono)',
            }}
          >
            {template.template_id}
          </span>
          <span className="mono-id" style={{ fontSize: '11px' }}>
            v{template.latest_version}
          </span>
          <span className="mono-id" style={{ fontSize: '11px' }}>
            fp:{template.prompt_fingerprint.slice(0, 8)}
          </span>
        </div>
        <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
          {template.appended_rules_count > 0 && (
            <span
              style={{
                fontSize: '11px',
                fontFamily: 'var(--font-mono)',
                padding: '1px 6px',
                backgroundColor: 'var(--ochre-50)',
                color: 'var(--ochre-600)',
                borderRadius: 'var(--radius-pill)',
                border: '1px solid var(--ochre-100)',
              }}
            >
              {template.appended_rules_count} rules
            </span>
          )}
          {template.last_run_at && (
            <span className="mono-id" style={{ fontSize: '10px' }}>
              last run {new Date(template.last_run_at).toLocaleTimeString()}
            </span>
          )}
        </div>
      </button>

      {open && (
        <div style={{ borderTop: '1px solid var(--paper-200)', padding: '12px 16px' }}>
          {!latestVersion ? (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                fontSize: '12px',
                color: 'var(--paper-400)',
              }}
            >
              <Spinner size={12} /> Loading rules...
            </div>
          ) : latestVersion.appended_rules.length === 0 ? (
            <div style={{ fontSize: '12px', color: 'var(--paper-400)', fontStyle: 'italic' }}>
              No rules extracted yet.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {latestVersion.appended_rules.map((rule, idx) => (
                <div key={idx} style={{ display: 'flex', gap: '12px' }}>
                  <span
                    style={{
                      width: '22px',
                      height: '22px',
                      borderRadius: '50%',
                      backgroundColor: 'var(--ochre-100)',
                      color: 'var(--ochre-600)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: '11px',
                      fontWeight: 700,
                      fontFamily: 'var(--font-mono)',
                      flexShrink: 0,
                    }}
                  >
                    {idx + 1}
                  </span>
                  <div style={{ flex: 1 }}>
                    <p className="prose-doc" style={{ margin: 0, fontSize: '13px' }}>
                      {rule}
                    </p>
                    {latestVersion.rules_added_vs_previous.includes(rule) && (
                      <span
                        style={{
                          display: 'inline-block',
                          marginTop: '3px',
                          fontSize: '10px',
                          color: 'var(--cite-supported)',
                          backgroundColor: 'var(--cite-supported-bg)',
                          borderRadius: 'var(--radius-pill)',
                          padding: '1px 6px',
                          fontFamily: 'var(--font-mono)',
                        }}
                      >
                        new in v{template.latest_version}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function AdminPage() {
  const { data: stats, isLoading: statsLoading } = useSWR('llm-stats', getLlmStats, {
    refreshInterval: 15_000,
  });
  const {
    data: templatesData,
    isLoading: tplLoading,
    mutate: mutateTpl,
  } = useSWR('admin-templates', getAdminTemplates);

  const [extracting, setExtracting] = useState(false);
  const [extractResult, setExtractResult] = useState<string | null>(null);

  async function handleRunExtractor() {
    setExtracting(true);
    setExtractResult(null);
    try {
      const result: RuleExtractorRunResponse = await runRuleExtractor();
      const totalRules = result.results.reduce((s, r) => s + r.new_rules.length, 0);
      const totalEdits = result.results.reduce((s, r) => s + r.edits_processed, 0);
      setExtractResult(
        `${totalRules} new rule${totalRules !== 1 ? 's' : ''} appended · ${totalEdits} edits processed${result.partial ? ' (partial)' : ''}.`,
      );
      await mutateTpl();
    } catch (err) {
      setExtractResult(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`);
    } finally {
      setExtracting(false);
    }
  }

  const spendPct = stats
    ? Math.min((stats.budget.current_spend_usd / (stats.budget.hourly_usd || 1)) * 100, 100)
    : 0;
  const spendColor =
    spendPct > 80
      ? 'var(--cite-unsupported)'
      : spendPct > 50
      ? 'var(--cite-partial)'
      : 'var(--cite-supported)';

  const totalP95 = stats?.by_tier?.length
    ? Math.max(...stats.by_tier.map((t) => t.p95_ms))
    : null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader eyebrow="System" title="Admin" />

      <div
        style={{
          flex: 1,
          overflow: 'auto',
          padding: '24px',
          display: 'flex',
          flexDirection: 'column',
          gap: '24px',
        }}
      >
        {/* Stats grid */}
        <div>
          <div className="eyebrow" style={{ marginBottom: '12px' }}>
            Last hour
          </div>
          {statsLoading && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                color: 'var(--paper-400)',
                fontSize: '13px',
              }}
            >
              <Spinner size={14} /> Loading stats...
            </div>
          )}
          {stats && (
            <div
              style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '12px' }}
            >
              <StatCard
                label="Spend / hour"
                value={`$${stats.budget.current_spend_usd.toFixed(2)}`}
                sub={`budget $${stats.budget.hourly_usd.toFixed(2)}/h`}
                progress={spendPct}
                progressColor={spendColor}
              />
              <StatCard
                label="LLM calls"
                value={stats.totals.calls.toLocaleString()}
                sub={`${Math.round(stats.totals.cache_hit_rate * 100)}% cache hits`}
              />
              <StatCard
                label="p95 latency"
                value={totalP95 != null ? `${totalP95.toFixed(0)}ms` : '—'}
              />
              <StatCard label="Cost (tokens)" value={`$${stats.totals.cost_usd.toFixed(4)}`} />
            </div>
          )}
          {!statsLoading && !stats && (
            <div
              style={{
                padding: '16px',
                color: 'var(--cite-unsupported)',
                fontSize: '13px',
                backgroundColor: 'var(--cite-unsupported-bg)',
                borderRadius: 'var(--radius-card)',
                border: '1px solid var(--cite-unsupported)',
              }}
            >
              Stats unavailable — check API connection.
            </div>
          )}
        </div>

        {/* Calls by tier */}
        {stats && stats.by_tier.length > 0 && (
          <Card>
            <CardHeader eyebrow="LLM Router" title="Calls by tier" />
            <table>
              <thead>
                <tr>
                  <th>Tier</th>
                  <th style={{ textAlign: 'right' }}>Calls</th>
                  <th style={{ textAlign: 'right' }}>Cost</th>
                  <th style={{ textAlign: 'right' }}>p50</th>
                  <th style={{ textAlign: 'right' }}>p95</th>
                  <th style={{ width: '160px' }}>Share</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_tier.map((tier: LlmTierStat) => {
                  const share = stats.totals.calls
                    ? tier.calls / stats.totals.calls
                    : 0;
                  return (
                    <tr key={tier.tier}>
                      <td>
                        <span
                          style={{
                            fontSize: '12px',
                            color: 'var(--paper-700)',
                            fontFamily: 'var(--font-mono)',
                          }}
                        >
                          {tier.tier}
                        </span>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <span className="mono-id">{tier.calls.toLocaleString()}</span>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <span className="mono-id">${tier.cost_usd.toFixed(4)}</span>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <span className="mono-id">{tier.p50_ms.toFixed(0)}ms</span>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <span className="mono-id">{tier.p95_ms.toFixed(0)}ms</span>
                      </td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <div
                            style={{
                              flex: 1,
                              height: '4px',
                              backgroundColor: 'var(--paper-200)',
                              borderRadius: '2px',
                              overflow: 'hidden',
                            }}
                          >
                            <div
                              style={{
                                height: '100%',
                                width: `${(share * 100).toFixed(0)}%`,
                                backgroundColor: 'var(--ochre-400)',
                                borderRadius: '2px',
                              }}
                            />
                          </div>
                          <span
                            className="mono-id"
                            style={{ fontSize: '11px', width: '32px', textAlign: 'right' }}
                          >
                            {(share * 100).toFixed(0)}%
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        )}

        {/* Rule extractor */}
        <Card>
          <CardHeader eyebrow="Stage 2" title="Rule extractor" />
          <CardBody>
            <p
              style={{
                margin: '0 0 12px',
                fontSize: '13px',
                color: 'var(--paper-500)',
                lineHeight: 1.5,
              }}
            >
              Clusters recent operator edits and appends durable rules to template system prompts.
              Runs automatically every 6 hours via APScheduler. The trigger is idempotent — edits
              already consumed are not re-applied.
            </p>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
              <Button
                variant="secondary"
                size="sm"
                icon={RefreshCw}
                onClick={handleRunExtractor}
                loading={extracting}
                disabled={extracting}
              >
                Re-extract rules now
              </Button>
              {extractResult && (
                <span
                  style={{
                    fontSize: '12px',
                    fontFamily: 'var(--font-sans)',
                    color: extractResult.startsWith('Error')
                      ? 'var(--cite-unsupported)'
                      : 'var(--cite-supported)',
                    padding: '4px 10px',
                    backgroundColor: extractResult.startsWith('Error')
                      ? 'var(--cite-unsupported-bg)'
                      : 'var(--cite-supported-bg)',
                    borderRadius: 'var(--radius-pill)',
                    border: `1px solid ${extractResult.startsWith('Error') ? 'var(--cite-unsupported)' : 'var(--cite-supported)'}`,
                  }}
                >
                  {extractResult}
                </span>
              )}
            </div>
          </CardBody>
        </Card>

        {/* Templates panel */}
        <div>
          <div className="eyebrow" style={{ marginBottom: '12px' }}>
            Templates
          </div>
          {tplLoading && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                color: 'var(--paper-400)',
                fontSize: '13px',
              }}
            >
              <Spinner size={14} /> Loading templates...
            </div>
          )}
          {!tplLoading &&
            (!templatesData?.templates || templatesData.templates.length === 0) && (
              <div style={{ padding: '16px', color: 'var(--paper-400)', fontSize: '13px' }}>
                No templates found.
              </div>
            )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {(templatesData?.templates ?? []).map((tpl: AdminTemplateRow) => (
              <TemplateRow key={tpl.template_id} template={tpl} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
