import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { registerDraft } from '../lib/drafts';
import { array, id, item, number, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, ErrorNotice, Notice, Panel, Toolbar } from '../components/ui';
import styles from './workspace.module.css';

export function HumanNotes({ meetingId }: { meetingId: string }) {
  const query = useResource('notes.get', { meeting_id: meetingId });
  return <Panel title="Your notes"><AsyncView query={query}>{(data) => <NotesEditor key={meetingId} meetingId={meetingId} saved={item(data)} />}</AsyncView></Panel>;
}

function NotesEditor({ meetingId, saved }: { meetingId: string; saved: RecordData }) {
  const client = useQueryClient();
  const [blocks, setBlocks] = useState<RecordData[]>(() => array(saved.blocks));
  const [state, setState] = useState<'saved' | 'dirty' | 'saving' | 'error'>('saved');
  const [error, setError] = useState<unknown>();
  const [pendingRemote, setPendingRemote] = useState(false);
  const current = useRef({ blocks, revision: revision(saved), dirty: false, generation: 0 });
  const inFlight = useRef<Promise<void> | null>(null);
  const mounted = useRef(true);

  const flush = useCallback(async (): Promise<void> => {
    if (inFlight.current) {
      await inFlight.current;
      if (!current.current.dirty) return;
    }
    if (!current.current.dirty || !nativeAvailable()) return;
    const snapshot = { ...current.current };
    if (mounted.current) setState('saving');
    const saving = request('notes.save', { meeting_id: meetingId, expected_revision: snapshot.revision, blocks: snapshot.blocks }).then(async (result) => {
      const persisted = item(result);
      current.current.revision = revision(persisted);
      current.current.dirty = current.current.generation !== snapshot.generation;
      if (mounted.current) { setState(current.current.dirty ? 'dirty' : 'saved'); setError(undefined); setPendingRemote(false); }
      await client.invalidateQueries({ queryKey: ['engine', 'notes.get', { meeting_id: meetingId }] });
    }).catch((failure: unknown) => {
      if (mounted.current) { setError(failure); setState('error'); }
      throw failure;
    }).finally(() => { inFlight.current = null; });
    inFlight.current = saving;
    await saving;
  }, [client, meetingId]);

  useEffect(() => {
    mounted.current = true;
    const unregister = registerDraft(flush);
    return () => { unregister(); void flush().catch(() => undefined); mounted.current = false; };
  }, [flush]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void flush().catch(() => undefined); }, 500);
    return () => window.clearTimeout(timer);
  }, [blocks, flush]);

  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent): void => { if (current.current.dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, []);

  useEffect(() => {
    if (revision(saved) > current.current.revision) {
      if (current.current.dirty) setPendingRemote(true);
      else { current.current = { blocks: array(saved.blocks), revision: revision(saved), dirty: false, generation: current.current.generation }; setBlocks(array(saved.blocks)); }
    }
  }, [saved]);

  const edit = (next: RecordData[]): void => {
    const ordered = next.map((block, order) => ({ ...block, order }));
    current.current = { ...current.current, blocks: ordered, dirty: true, generation: current.current.generation + 1 };
    setBlocks(ordered); setState('dirty');
  };
  const changeBlock = (blockId: string, field: string, value: string): void => edit(blocks.map((block) => id(block) === blockId ? { ...block, [field]: value } : block));
  const move = (index: number, direction: number): void => {
    const next = [...blocks];
    const other = next[index + direction]; const block = next[index];
    if (!other || !block) return;
    next[index] = other; next[index + direction] = block; edit(next);
  };
  return <div><div role="status" aria-live="polite">{state !== 'saved' ? <Badge tone={state === 'error' ? 'danger' : 'warning'}>{state === 'dirty' ? 'Unsaved changes' : state === 'saving' ? 'Saving…' : 'Save failed. Your draft is kept here.'}</Badge> : null}</div>{pendingRemote && <Notice danger>These notes were changed elsewhere. Compare before saving again.<details><summary>View the newer version</summary><pre>{JSON.stringify(saved.blocks, null, 2)}</pre></details><button onClick={() => { current.current.revision = revision(saved); setPendingRemote(false); }}>Keep my version</button></Notice>}{Boolean(error) && <ErrorNotice error={error} retry={() => { void flush().catch(() => undefined); }} />}{blocks.map((block, index) => <section className={styles.noteBlock} key={id(block)}><label>Heading<input value={text(block.heading)} onChange={(event) => changeBlock(id(block), 'heading', event.target.value)} onBlur={() => { void flush().catch(() => undefined); }} /></label><label>Note<textarea rows={4} value={text(block.body)} onChange={(event) => changeBlock(id(block), 'body', event.target.value)} onBlur={() => { void flush().catch(() => undefined); }} /></label>{block.anchor && <small>Linked to transcript at {Math.round(number(object(block.anchor).start_ms) / 1000)} s</small>}<Toolbar><button aria-label={`Move note ${index + 1} up`} disabled={index === 0} onClick={() => move(index, -1)}>Move up</button><button aria-label={`Move note ${index + 1} down`} disabled={index === blocks.length - 1} onClick={() => move(index, 1)}>Move down</button><button onClick={() => edit(blocks.filter((candidate) => id(candidate) !== id(block)))}>Remove</button></Toolbar></section>)}<Toolbar><button onClick={() => edit([...blocks, { id: crypto.randomUUID(), heading: '', body: '', order: blocks.length }])}>Add note</button>{state !== 'saved' && <button disabled={state === 'saving'} onClick={() => { void flush().catch(() => undefined); }}>Save now</button>}</Toolbar></div>;
}
