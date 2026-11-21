import { api } from './api';
import type { Job, Platform, Topic } from './types';

/** Keep the same request identity until the server acknowledges creation. */
export async function createDrafts(topic: Pick<Topic, 'id' | 'revision' | 'platforms'>, platforms: Platform[] = topic.platforms) {
  const key = `workbench:create-drafts:${topic.id}:${topic.revision}:${[...platforms].sort().join(',')}`;
  let requestId = '';
  try { requestId = sessionStorage.getItem(key) || ''; } catch { /* The in-flight request still has an identity without browser storage. */ }
  if (!requestId) {
    requestId = crypto.randomUUID();
    try { sessionStorage.setItem(key, requestId); } catch { /* Storage may be disabled. */ }
  }
  const result = await api<{ job: Job; topic: Topic }>(`/topics/${topic.id}/create-drafts`, 'POST', { expected_revision: topic.revision, platforms, request_id: requestId });
  try { sessionStorage.removeItem(key); } catch { /* Acknowledged requests no longer need retry storage. */ }
  return result;
}

export const readableError = (error: unknown) => error instanceof Error ? error.message : '操作暂未完成，请重试。';
