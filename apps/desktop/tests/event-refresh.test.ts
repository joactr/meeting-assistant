import { QueryClient, QueryObserver } from '@tanstack/react-query';
import { waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { EventEnvelope } from '../../../engine/contracts/types';
import { subscribeEngine } from '../src/lib/bridge';

const nativeEvents = vi.hoisted(() => ({
  listener: undefined as ((event: { payload: EventEnvelope }) => void) | undefined,
}));
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(async () => ({ sequence: 0 })) }));
vi.mock('@tauri-apps/api/event', () => ({
  listen: async (_name: string, listener: (event: { payload: EventEnvelope }) => void) => {
    nativeEvents.listener = listener;
    return () => { nativeEvents.listener = undefined; };
  },
}));

it('bounds event-burst reads, catches the final revision and isolates foreign meetings', async () => {
  Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {}, configurable: true });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const cleanup = await subscribeEngine(client);
  const key = ['engine', 'transcript.list', { meeting_id: 'meeting-one' }];
  const foreignKey = ['engine', 'transcript.list', { meeting_id: 'meeting-two' }];
  client.setQueryData(key, { revision: 0 });
  client.setQueryData(foreignKey, { revision: 0 });
  let revision = 0;
  let activeReads = 0;
  let maximumActiveReads = 0;
  let foreignReads = 0;
  const reads: { revision: number; release: () => void }[] = [];
  const observer = new QueryObserver(client, {
    queryKey: key,
    staleTime: Infinity,
    queryFn: async () => {
      activeReads += 1;
      maximumActiveReads = Math.max(maximumActiveReads, activeReads);
      const requestedRevision = revision;
      await new Promise<void>((release) => { reads.push({ revision: requestedRevision, release }); });
      activeReads -= 1;
      return { revision: requestedRevision };
    },
  });
  const foreignObserver = new QueryObserver(client, {
    queryKey: foreignKey,
    staleTime: Infinity,
    queryFn: async () => { foreignReads += 1; return { revision: 0 }; },
  });
  const unsubscribe = observer.subscribe(() => undefined);
  const unsubscribeForeign = foreignObserver.subscribe(() => undefined);
  const emit = (sequence: number): void => {
    revision = sequence;
    nativeEvents.listener?.({ payload: {
      protocol_version: 1, seq: sequence, event_id: `event-${sequence}`,
      type: 'transcript.utterance_updated', meeting_id: 'meeting-one',
      entity_id: 'utterance-one', revision: sequence, occurred_at_ms: sequence, data: {},
    } });
  };
  try {
    emit(1);
    await waitFor(() => expect(reads).toHaveLength(1));
    for (let sequence = 2; sequence <= 30; sequence += 1) emit(sequence);
    expect(reads).toHaveLength(1);
    reads[0]?.release();
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(reads[1]?.revision).toBe(30);
    reads[1]?.release();
    await waitFor(() => expect(client.getQueryData(key)).toEqual({ revision: 30 }));
    expect(maximumActiveReads).toBe(1);
    expect(foreignReads).toBe(0);
    expect(client.getQueryData(foreignKey)).toEqual({ revision: 0 });
  } finally {
    cleanup();
    unsubscribe();
    unsubscribeForeign();
    for (const read of reads) read.release();
    client.clear();
    Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
  }
});
