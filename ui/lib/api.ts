import type {
  DocumentList,
  DocumentStatus,
  UploadResponse,
  BlocksResponse,
  TemplateInfo,
  DraftCreateRequest,
  DraftCreateResponse,
  DraftResponse,
  LlmStats,
  RuleExtractorRunResponse,
  AdminTemplatesResponse,
  TemplateVersionsResponse,
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

export async function regenerateSection(draftId: string, sectionName: string): Promise<void> {
  await apiFetch<void>(`${API_BASE}/drafts/${draftId}/sections/${encodeURIComponent(sectionName)}/regenerate`, {
    method: 'POST',
  });
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
