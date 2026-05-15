export interface DocumentSummary {
  document_id: string;
  filename: string;
  status: string;
  page_count: number | null;
  size_bytes: number | null;
  created_at: string;
  has_blocks?: boolean;
}

export interface DocumentStatus extends DocumentSummary {
  sha256: string;
  mime_type: string | null;
  last_event_seq: number;
  error_code: string | null;
  error_message: string | null;
  updated_at: string;
}

export interface Block {
  id: string;
  block_type: string;
  text: string;
  page_start: number;
  page_end: number;
  reading_order: number;
  bbox: [number, number, number, number];
  metadata?: Record<string, unknown>;
}

export interface TemplateInfo {
  id: string;
  latest_version: number;
  display_name: string;
  description: string;
  fingerprint: string;
}

export type CitationStatus =
  | 'supported'
  | 'partial'
  | 'unsupported'
  | 'contradicted'
  | 'unchecked'
  | 'stale';

export interface CitationView {
  chunk_id: string;
  claim_span_start: number | null;
  claim_span_end: number | null;
  validation_status: CitationStatus;
  validation_reason: string | null;
}

export interface SectionView {
  name: string;
  text: string | null;
  citations: CitationView[];
  groundedness: number | null;
  target_length_min?: number | null;
  target_length_max?: number | null;
}

export interface DraftResponse {
  draft_id: string;
  template_id: string;
  template_version: number;
  prompt_fingerprint: string;
  status: string;
  fields: Record<string, unknown> | null;
  sections: SectionView[];
  groundedness_score: number | null;
  generated_at: string | null;
  cost_usd: number | null;
  edit_count: number;
  error: { error_code?: string | null; error_message?: string | null } | null;
  document_ids?: string[];
  extra_instructions?: string | null;
}

export interface DocumentList {
  items: DocumentSummary[];
  next_offset: number | null;
}

export interface UploadResponse {
  document_id: string;
  status: string;
  was_new: boolean;
  sha256: string;
}

export interface BlocksResponse {
  blocks: Block[];
}

export interface DraftSummary {
  draft_id: string;
  template_id: string;
  template_version: number;
  status: string;
  document_count: number;
  model_used: string | null;
  cost_usd: number | null;
  groundedness_score: number | null;
  edit_count: number;
  error_code: string | null;
  generated_at: string | null;
  created_at: string;
  has_extra_instructions?: boolean;
}

export interface DraftList {
  items: DraftSummary[];
}

export interface DraftCreateRequest {
  template_id: string;
  document_ids: string[];
  extra_instructions?: string | null;
}

export interface DraftCreateResponse {
  draft_id: string;
  status: string;
}

export interface EditRequest {
  final_output: Record<string, unknown>;
}

export interface LlmTierStat {
  tier: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  cache_hits: number;
  p50_ms: number;
  p95_ms: number;
}

export interface LlmStatsTotals {
  calls: number;
  cache_hits: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  cache_hit_rate: number;
}

export interface LlmStatsBudget {
  hourly_usd: number;
  current_spend_usd: number;
  remaining_usd: number;
}

export interface LlmStats {
  window: string;
  totals: LlmStatsTotals;
  by_tier: LlmTierStat[];
  budget: LlmStatsBudget;
}

export interface ExtractionResultRow {
  template_id: string;
  skipped_reason: 'locked' | 'no_template' | 'no_edits' | null;
  edits_processed: number;
  new_rules: string[];
  new_version: number | null;
  prompt_fingerprint: string;
}

export interface RuleExtractorRunResponse {
  results: ExtractionResultRow[];
  partial: boolean;
}

export interface AdminTemplateRow {
  template_id: string;
  latest_version: number;
  prompt_fingerprint: string;
  appended_rules_count: number;
  last_run_at: string | null;
}

export interface AdminTemplatesResponse {
  templates: AdminTemplateRow[];
}

export interface TemplateVersionRow {
  version: number;
  prompt_fingerprint: string;
  appended_rules: string[];
  rules_added_vs_previous: string[];
  resolved_system_prompt: string;
  created_at: string;
}

export interface TemplateVersionsResponse {
  template_id: string;
  versions: TemplateVersionRow[];
}

export interface SSEEvent {
  event_type: string;
  seq: number;
  data: Record<string, unknown>;
}

export interface EditMetricFieldRow {
  name: string;
  edited_count: number;
  edit_rate: number | null;
}

export interface EditMetricSectionRow {
  name: string;
  edited_count: number;
  edit_rate: number | null;
}

export interface EditMetricsResponse {
  template_id: string;
  window_days: number;
  drafts_count: number;
  fields: EditMetricFieldRow[];
  sections: EditMetricSectionRow[];
}
