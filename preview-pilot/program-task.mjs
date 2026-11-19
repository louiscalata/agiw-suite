import {readFileSync} from 'node:fs';
import {basename} from 'node:path';
import {fileURLToPath} from 'node:url';

export function programTask(programUrl) {
  try {
    const manifest = JSON.parse(readFileSync(new URL('./program-tasks.json', import.meta.url), 'utf8'));
    const task = manifest.tasks[basename(fileURLToPath(programUrl))];
    const text = s => typeof s === 'string' && s.trim().length > 0;
    if (manifest.schemaVersion === 1 && task && Object.keys(task).length === 5 &&
        ['taskId', 'mode', 'language'].every(k => text(task[k])) &&
        ['allowedFiles', 'acceptanceCriteria'].every(k => Array.isArray(task[k]) && task[k].length && task[k].every(text))) return task;
  } catch { /* Missing or malformed manifest. */ }
  throw Error('JEV_TASK_INVALID');
}
