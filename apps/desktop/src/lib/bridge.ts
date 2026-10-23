import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { QueryClient } from '@tanstack/react-query';
import type { ApplicationCommand, EventEnvelope } from '../../../../packages/contracts/generated/types';
import { object, text, number, type Payload, type RecordData } from './data';

export class ApplicationError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly details: RecordData;
  readonly traceId: string;

  constructor(value: unknown) {
    let parsed = value;
    if (typeof value === 'string') {
      try { parsed = JSON.parse(value) as unknown; } catch { parsed = { message: value }; }
    }
    const data = object(parsed);
    super(text(data.message, value instanceof Error ? value.message : 'The operation could not be completed.'));
    this.name = 'ApplicationError';
    this.code = text(data.code, 'unavailable');
    this.retryable = data.retryable === true;
    this.details = object(data.details);
    this.traceId = text(data.trace_id);
  }
}

export function nativeAvailable(): boolean {
  return '__TAURI_INTERNALS__' in window;
}

export async function nativeRequest<T = RecordData>(command: string, args: Payload = {}): Promise<T> {
  if (!nativeAvailable()) {
    throw new ApplicationError({
      code: 'native_unavailable',
      message: 'This feature requires the installed desktop application. Browser development has no native engine, capture, files, credentials or playback.',
      retryable: false,
    });
  }
  try { return await invoke<T>(command, args); } catch (error) { throw new ApplicationError(error); }
}

export async function request(operationId: string, payload: Payload = {}): Promise<RecordData> {
  const command: ApplicationCommand = { operation_id: operationId, payload };
  return nativeRequest('engine_request', { command: { ...command } });
}

export type EngineEvent = EventEnvelope;

// The bounded cursor is transport metadata, not a second copy of domain state.
export class EventCursor {
  sequence = 0;
  private readonly seen = new Set<string>();
  private readonly revisions = new Map<string, number>();

  accept(event: EngineEvent): boolean {
    if (event.protocol_version !== 1 || this.seen.has(event.event_id)) return false;
    if (event.seq && event.seq < this.sequence) return false;
    const key = `${event.meeting_id ?? ''}:${event.entity_id ?? ''}`;
    const previous = this.revisions.get(key) ?? -1;
    if (event.revision != null && event.revision <= previous) return false;
    this.seen.add(event.event_id);
    if (this.seen.size > 4096) {
      const oldest = this.seen.values().next().value;
      if (oldest !== undefined) this.seen.delete(oldest);
    }
    if (event.entity_id && event.revision != null) this.revisions.set(key, event.revision);
    if (this.revisions.size > 4096) this.revisions.delete(this.revisions.keys().next().value ?? '');
    this.sequence = Math.max(this.sequence, event.seq || 0);
    return true;
  }

  snapshot(sequence: number): void {
    this.sequence = sequence;
    this.seen.clear();
    this.revisions.clear();
  }
}

export async function subscribeEngine(client: QueryClient): Promise<() => void> {
  if (!nativeAvailable()) return () => undefined;
  const cursor = new EventCursor();
  let disposed = false;
  let refreshing = false;
  let pendingScopes = new Set<string | null>();
  const refresh = async (meetingId: string | null = null): Promise<void> => {
    pendingScopes.add(meetingId);
    if (refreshing || disposed) return;
    refreshing = true;
    try {
      while (pendingScopes.size && !disposed) {
        const scopes = pendingScopes;
        pendingScopes = new Set();
        // Do not cancel native requests already in flight. Events received while
        // they finish are drained in the next batch, including the final update.
        await client.invalidateQueries({
          predicate: (query) => {
            if (query.queryKey[0] !== 'engine') return false;
            const args = object(query.queryKey[2]);
            return !args.meeting_id || scopes.has(null)
              || (typeof args.meeting_id === 'string' && scopes.has(args.meeting_id));
          },
        }, { cancelRefetch: false });
      }
    } finally { refreshing = false; }
  };
  let reconciling = false;
  const reconcile = async (): Promise<void> => {
    if (reconciling) return;
    reconciling = true;
    try {
      const snapshot = await request('events.snapshot', { after_seq: cursor.sequence });
      cursor.snapshot(number(snapshot.sequence ?? snapshot.seq ?? snapshot.watermark));
      await refresh();
    } finally { reconciling = false; }
  };
  const unlisten = await listen<EngineEvent>('engine-event', ({ payload }) => {
    if (payload.type === 'resync_required' || payload.type === 'engine.reconnected') {
      void reconcile().catch(() => refresh());
      return;
    }
    if (!cursor.accept(payload)) return;
    // Query keys include meeting IDs. Foreign events never replace another meeting's rows.
    void refresh(payload.meeting_id ?? null);
  });
  const onFocus = (): void => { void reconcile().catch(() => undefined); };
  window.addEventListener('focus', onFocus);
  await reconcile().catch(() => undefined);
  return () => { disposed = true; pendingScopes.clear(); unlisten(); window.removeEventListener('focus', onFocus); };
}
