import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { QueryClient } from '@tanstack/react-query';
import type { ApplicationCommand, EventEnvelope } from '../../../../engine/contracts/types';
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

/** The native host's answer to requests that arrive before the engine process has finished starting. */
export function engineStarting(error: unknown): boolean {
  return error instanceof ApplicationError && error.message === 'engine is not ready';
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

// Entity events that only ever change their own meeting's views (transcript, speakers, capture).
// While recording these arrive several times a second; they must not reload global lists too.
const MEETING_ONLY = /^(utterances|utterance_revisions|words|transcript|speaker_\w+|meeting_speakers|timeline_gaps|citations|objects|entity_sources|capture_sources|media_segments)\./;

export async function subscribeEngine(client: QueryClient): Promise<() => void> {
  if (!nativeAvailable()) return () => undefined;
  const cursor = new EventCursor();
  let disposed = false;
  let refreshing = false;
  // Scope (meeting ID, or null for everything) -> whether global queries need reloading too.
  let pendingScopes = new Map<string | null, boolean>();
  const refresh = async (meetingId: string | null = null, global = true): Promise<void> => {
    pendingScopes.set(meetingId, global || pendingScopes.get(meetingId) === true);
    if (refreshing || disposed) return;
    refreshing = true;
    try {
      while (pendingScopes.size && !disposed) {
        const scopes = pendingScopes;
        pendingScopes = new Map();
        const anyGlobal = [...scopes.values()].some(Boolean);
        // Do not cancel native requests already in flight. Events received while
        // they finish are drained in the next batch, including the final update.
        await client.invalidateQueries({
          predicate: (query) => {
            if (query.queryKey[0] !== 'engine') return false;
            if (scopes.has(null)) return true;
            const scoped = object(query.queryKey[2]).meeting_id;
            return typeof scoped === 'string' && scoped ? scopes.has(scoped) : anyGlobal;
          },
        }, { cancelRefetch: false });
        // At most one reload per second: a live meeting emits several events a second and
        // each reload is served by the same engine loop that reads the audio.
        await new Promise((resolve) => setTimeout(resolve, 1000));
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
    if (payload.type === 'resync_required' || payload.type === 'engine.reconnected' || payload.type === 'native.engine.ready') {
      void reconcile().catch(() => refresh());
      return;
    }
    if (!cursor.accept(payload)) return;
    // Query keys include meeting IDs. Foreign events never replace another meeting's rows.
    void refresh(payload.meeting_id ?? null, !payload.meeting_id || !MEETING_ONLY.test(payload.type));
  });
  const onFocus = (): void => { void reconcile().catch(() => undefined); };
  window.addEventListener('focus', onFocus);
  await reconcile().catch(() => undefined);
  return () => { disposed = true; pendingScopes.clear(); unlisten(); window.removeEventListener('focus', onFocus); };
}
