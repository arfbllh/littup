import type {
  DocumentList,
  DocumentStatus,
  UploadResponse,
  BlocksResponse,
  TemplateInfo,
  DraftCreateRequest,
  DraftCreateResponse,
  DraftList,
  DraftResponse,
  LlmStats,
  RuleExtractorRunResponse,
  AdminTemplatesResponse,
  TemplateVersionsResponse,
  SectionView,
  EditMetricsResponse,
  ReextractSessionView,
  OcrProvider,
} from './types';

const API_BASE = '/api';
const ADMIN_BASE = '/admin';

async function apiFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => 'Unknown error');
    throw new Error(`API error ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export async function listDocuments(offset = 0): Promise<DocumentList> {
  return apiFetch<DocumentList>(`${API_BASE}/documents?offset=${offset}`);
}

export async function getDocument(id: string): Promise<DocumentStatus> {
  return apiFetch<DocumentStatus>(`${API_BASE}/documents/${id}`);
}

export async function uploadDocument(file: File): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${API_BASE}/documents`, {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => 'Unknown error');
    throw new Error(`Upload error ${res.status}: ${text}`);
  }
  return res.json() as Promise<UploadResponse>;
}

export async function getBlocks(docId: string): Promise<BlocksResponse> {
  return apiFetch<BlocksResponse>(`${API_BASE}/documents/${docId}/blocks`);
}

export async function retryDocument(
  docId: string,
  options: { force?: boolean; provider?: 'pdfplumber' | 'paddleocr' | 'auto' } = {},
): Promise<DocumentStatus> {
  const params = new URLSearchParams();
  if (options.force) params.set('force', 'true');
  if (options.provider) params.set('provider', options.provider);
  const qs = params.toString() ? `?${params.toString()}` : '';
  return apiFetch<DocumentStatus>(`${API_BASE}/documents/${docId}/retry${qs}`, {
    method: 'POST',
  });
}

export async function reextractPage(
  docId: string,
  pageNum: number,
  provider: 'pdfplumber' | 'paddleocr',
): Promise<DocumentStatus> {
  const qs = `?provider=${encodeURIComponent(provider)}`;
  return apiFetch<DocumentStatus>(
    `${API_BASE}/documents/${docId}/pages/${pageNum}/reextract${qs}`,
    { method: 'POST' },
  );
}

export async function startReextractSession(
  docId: string,
  pages: number[],
  provider: OcrProvider,
): Promise<ReextractSessionView> {
  return apiFetch<ReextractSessionView>(
    `${API_BASE}/documents/${docId}/reextract-sessions`,
    {
      method: 'POST',
      body: JSON.stringify({ pages, provider }),
    },
  );
}

export async function getReextractSession(
  docId: string,
  sessionId: string,
): Promise<ReextractSessionView> {
  return apiFetch<ReextractSessionView>(
    `${API_BASE}/documents/${docId}/reextract-sessions/${sessionId}`,
  );
}

export async function acceptReextractSession(
  docId: string,
  sessionId: string,
  acceptedPages?: number[],
): Promise<ReextractSessionView> {
  return apiFetch<ReextractSessionView>(
    `${API_BASE}/documents/${docId}/reextract-sessions/${sessionId}/accept`,
    {
      method: 'POST',
      body: JSON.stringify(acceptedPages ? { pages: acceptedPages } : {}),
    },
  );
}

export async function rejectReextractSession(
  docId: string,
  sessionId: string,
): Promise<ReextractSessionView> {
  return apiFetch<ReextractSessionView>(
    `${API_BASE}/documents/${docId}/reextract-sessions/${sessionId}/reject`,
    { method: 'POST' },
  );
}

export async function deleteDocument(docId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/documents/${docId}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 204) {
    const text = await res.text().catch(() => 'Unknown error');
    throw new Error(`Delete error ${res.status}: ${text}`);
  }
}

export function getPageImageUrl(docId: string, page: number): string {
  return `${API_BASE}/documents/${docId}/pages/${page}`;
}

export async function listTemplates(): Promise<TemplateInfo[]> {
  return apiFetch<TemplateInfo[]>(`${API_BASE}/templates`);
}

export async function createDraft(req: DraftCreateRequest): Promise<DraftCreateResponse> {
  return apiFetch<DraftCreateResponse>(`${API_BASE}/drafts`, {
    method: 'POST',
    body: JSON.stringify(req),
  });
}

export async function getDraft(id: string): Promise<DraftResponse> {
  return apiFetch<DraftResponse>(`${API_BASE}/drafts/${id}`);
}

export async function listDrafts(): Promise<DraftList> {
  return apiFetch<DraftList>(`${API_BASE}/drafts`);
}

export async function deleteDraft(draftId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/drafts/${draftId}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 204) {
    const text = await res.text().catch(() => 'Unknown error');
    throw new Error(`Delete error ${res.status}: ${text}`);
  }
}

export async function regenerateSection(
  draftId: string,
  sectionName: string,
): Promise<SectionView> {
  return apiFetch<SectionView>(
    `${API_BASE}/drafts/${draftId}/sections/${encodeURIComponent(sectionName)}/regenerate`,
    { method: 'POST' },
  );
}

export async function saveEdit(draftId: string, finalOutput: Record<string, unknown>): Promise<DraftResponse> {
  return apiFetch<DraftResponse>(`${API_BASE}/drafts/${draftId}/edit`, {
    method: 'POST',
    body: JSON.stringify({ final_output: finalOutput }),
  });
}

export async function getLlmStats(): Promise<LlmStats> {
  const res = await fetch(`${ADMIN_BASE}/llm-stats`);
  if (!res.ok) throw new Error(`Admin API error ${res.status}`);
  return res.json() as Promise<LlmStats>;
}

export async function runRuleExtractor(): Promise<RuleExtractorRunResponse> {
  const res = await fetch(`${ADMIN_BASE}/rule-extractor/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!res.ok) throw new Error(`Admin API error ${res.status}`);
  return res.json() as Promise<RuleExtractorRunResponse>;
}

export async function getAdminTemplates(): Promise<AdminTemplatesResponse> {
  const res = await fetch(`${ADMIN_BASE}/templates`);
  if (!res.ok) throw new Error(`Admin API error ${res.status}`);
  return res.json() as Promise<AdminTemplatesResponse>;
}

export async function getTemplateVersions(templateId: string): Promise<TemplateVersionsResponse> {
  const res = await fetch(`${ADMIN_BASE}/templates/${encodeURIComponent(templateId)}/versions`);
  if (!res.ok) throw new Error(`Admin API error ${res.status}`);
  return res.json() as Promise<TemplateVersionsResponse>;
}

export interface BlockEditResponse {
  block_id: string;
  affected_chunks: number;
  stale_citations: number;
  no_change: boolean;
}

export async function patchBlockText(
  docId: string,
  blockId: string,
  text: string,
): Promise<BlockEditResponse> {
  return apiFetch<BlockEditResponse>(
    `${API_BASE}/documents/${docId}/blocks/${blockId}`,
    { method: 'PATCH', body: JSON.stringify({ text }) },
  );
}

export interface RevalidateResponse {
  draft_id: string;
  revalidated: number;
  by_status: Record<string, number>;
}

export async function revalidateDraft(draftId: string): Promise<RevalidateResponse> {
  return apiFetch<RevalidateResponse>(`${API_BASE}/drafts/${draftId}/revalidate`, {
    method: 'POST',
  });
}

export async function getEditMetrics(
  templateId: string,
  days?: number,
): Promise<EditMetricsResponse> {
  const qs = days ? `?days=${days}` : '';
  return apiFetch<EditMetricsResponse>(
    `${API_BASE}/templates/${encodeURIComponent(templateId)}/edit-metrics${qs}`,
  );
}
