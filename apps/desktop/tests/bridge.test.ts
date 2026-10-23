import { describe, expect, it } from 'vitest';
import { ApplicationError, EventCursor, nativeRequest } from '../src/lib/bridge';
import type { EventEnvelope } from '../../../packages/contracts/generated/types';

function event(changes: Partial<EventEnvelope> = {}): EventEnvelope {
  return { protocol_version: 1, seq: 1, event_id: 'event-one', type: 'transcript.utterance_updated', meeting_id: 'meeting-one', entity_id: 'utterance-one', revision: 1, occurred_at_ms: 1000, data: {}, ...changes };
}

describe('native-only transport', () => {
  it('fails honestly without an installed host rather than making a browser HTTP request', async () => {
    await expect(nativeRequest('engine_request')).rejects.toMatchObject({ code: 'native_unavailable', retryable: false });
  });
  it('preserves structured conflicts without exposing raw provider responses', () => {
    const error = new ApplicationError(JSON.stringify({ code: 'revision_conflict', message: 'A newer edit exists', retryable: false, details: { current_revision: 3 }, trace_id: 'trace-local' }));
    expect(error.code).toBe('revision_conflict');
    expect(error.details.current_revision).toBe(3);
    expect(error.traceId).toBe('trace-local');
  });
});

describe('durable event cursor', () => {
  it('deduplicates at-least-once delivery and stale entity revisions', () => {
    const cursor = new EventCursor();
    expect(cursor.accept(event())).toBe(true);
    expect(cursor.accept(event())).toBe(false);
    expect(cursor.accept(event({ event_id: 'new-id', seq: 2, revision: 1 }))).toBe(false);
    expect(cursor.accept(event({ event_id: 'late-speaker', seq: 3, revision: 2 }))).toBe(true);
    expect(cursor.sequence).toBe(3);
  });
  it('keeps the same entity ID in different meetings independent', () => {
    const cursor = new EventCursor();
    expect(cursor.accept(event({ revision: 5 }))).toBe(true);
    expect(cursor.accept(event({ meeting_id: 'meeting-two', event_id: 'other', seq: 2, revision: 1 }))).toBe(true);
  });
  it('rejects old sequence delivery and resumes after a consistent snapshot', () => {
    const cursor = new EventCursor(); cursor.snapshot(80);
    expect(cursor.accept(event({ seq: 79 }))).toBe(false);
    expect(cursor.accept(event({ seq: 81 }))).toBe(true);
  });
});
