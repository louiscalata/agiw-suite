/** Decide whether the selected exact local inventory row is safe to control. */
export function modelControlView(model, { host, feedFresh, supported, reason, status, models = [] } = {}) {
  if (!model || model.host !== host) return { action: null, reason: 'Choose a local runtime model.' };
  if (!feedFresh) return { action: null, reason: 'Live feed is stale; control is unavailable.' };
  if (supported !== true) return { action: null, reason: reason || 'Local model control is unsupported.' };
  if (status?.status === 'running') return { action: null, reason: `${status.action || 'Model operation'} is running.` };
  if (model.source === 'lmstudio-api' && model.state === 'unloaded' && model.loaded === false && (host === 'windows' || model.modelKey === model.id)) {
    return { action: 'load', reason: 'Ready to load this exact local model.' };
  }
  const confirmedLists = Array.isArray(models) ? models.filter(row => row?.host === 'mac' && row.modelKey === model?.modelKey && Array.isArray(row.loadedInstanceIds)).map(row => row.loadedInstanceIds) : [];
  if (model.source === 'lms-ps' && model.state === 'idle' && model.queued === 0 && model.loaded === true && (host === 'windows' || (typeof model.modelKey === 'string' && model.instanceId === model.id && confirmedLists.length === 1 && confirmedLists[0].includes(model.id)))) {
    return { action: 'unload', reason: 'Ready to unload this idle local model.' };
  }
  if (model.state === 'stale') return { action: null, reason: 'Model evidence is stale.' };
  if (model.loaded === true && model.queued > 0) return { action: null, reason: 'Requests are queued; unload is unavailable.' };
  return { action: null, reason: 'Current source and state do not permit a safe load or unload.' };
}

/** Submit one user-triggered operation through an injectable fetch boundary. */
export async function submitModelControl(fetcher, action, modelId) {
  const response = await fetcher('/api/models/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, modelId }),
  });
  const result = await response.json().catch(() => ({}));
  return { ok: response.ok, statusCode: response.status, result };
}
