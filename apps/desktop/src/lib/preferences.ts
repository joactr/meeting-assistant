import { nativeRequest } from './bridge';
import { object, type Payload, type RecordData } from './data';

let queue: Promise<unknown> = Promise.resolve();

export function updatePreferences(changes: Payload): Promise<RecordData> {
  const update = queue.catch(() => undefined).then(async () => {
    const result = await nativeRequest('native_preferences_get');
    const saved = object(result.preferences ?? result);
    const next: RecordData = { ...saved, ...changes, schema_version: 1 };
    if (changes.selected_tabs) next.selected_tabs = { ...object(saved.selected_tabs), ...object(changes.selected_tabs) };
    if (changes.updates) next.updates = { ...object(saved.updates), ...object(changes.updates) };
    return nativeRequest('native_preferences_set', { preferences: next });
  });
  queue = update;
  return update;
}
