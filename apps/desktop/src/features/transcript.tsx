import { useEffect, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { listen } from '@tauri-apps/api/event';
import { useCommand, useMeetingMedia, useResource } from '../lib/queries';
import { ApplicationError, nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useCursorResource } from '../lib/paged-query';
import { flushDrafts } from '../lib/drafts';
import { useInterface } from '../lib/ui-store';
import { array, clock, id, item, items, number, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandForm, Empty, ErrorNotice, Modal, Panel, Toolbar } from '../components/ui';
import styles from './workspace.module.css';

export function Transcript({ meetingId, recording }: { meetingId: string; recording: boolean }) {
  const query = useCursorResource('transcript.list', { meeting_id: meetingId, limit: 200 });
  const speakers = useResource('speakers.list', { meeting_id: meetingId });
  const sources = useResource('entity.list', { collection: 'capture_sources', filters: { meeting_id: meetingId } });
  const media = useMeetingMedia(meetingId, !recording);
  const selected = useInterface((state) => state.selectedUtterance);
  const rows: RecordData[] = query.data?.pages.flatMap((page) => items(page)).map((row) => {
    const assignments = array(row.speaker_assignments);
    const speaker = items(speakers.data).find((person) => id(person) === assignments[0]?.speaker_id);
    const source = items(sources.data).find((entry) => id(entry) === row.source_id);
    const retained = items(media.data).find((entry) => entry.source_id === row.source_id && number(entry.start_ms) <= number(row.start_ms) && number(entry.start_ms) + number(entry.duration_ms) > number(row.start_ms) && entry.state === 'committed');
    return { ...row, speaker_id: speaker?.id ?? null, speaker_revision: speaker?.revision ?? null, speaker_name: assignments.length > 1 ? 'Multiple speakers / overlap' : speaker?.display_name ?? (source?.source_kind === 'mic' ? 'Me' : source?.source_kind === 'system' ? 'System' : 'Unknown speaker'), media_id: retained?.id ?? null, media_available: !!retained, manual_lock: !!row.manual_text_lock, timing_estimated: row.timing_provenance === 'user_estimated' };
  }) ?? [];
  useEffect(() => {
    if (query.hasNextPage && !query.isFetchingNextPage && (recording || (selected && !rows.some((row) => id(row) === selected)))) void query.fetchNextPage();
  }, [query.hasNextPage, query.isFetchingNextPage, rows.length, selected, recording]);
  const combined = { ...query, data: { items: rows } };
  return <Panel title="Transcript" actions={<Badge>Active source revision</Badge>}><InterimCaptions meetingId={meetingId} recording={recording} finalRows={rows} /><AsyncView query={combined}>{() => <TranscriptRows meetingId={meetingId} rows={rows} recording={recording} />}</AsyncView>{query.hasNextPage && <button disabled={query.isFetchingNextPage} onClick={() => { void query.fetchNextPage(); }}>{query.isFetchingNextPage ? 'Loading transcript page…' : 'Load more transcript'}</button>}</Panel>;
}

function InterimCaptions({ meetingId, recording, finalRows }: { meetingId: string; recording: boolean; finalRows: RecordData[] }) {
  const [captions, setCaptions] = useState<RecordData[]>([]);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    if (!nativeAvailable() || !recording) { setCaptions([]); return; }
    let disposed = false;
    let cleanup: (() => void) | undefined;
    void listen<RecordData>('engine-event', ({ payload }) => {
      const data = item(payload.data);
      if (text(payload.meeting_id ?? data.meeting_id) !== meetingId) return;
      if (payload.type === 'transcript.utterance_final') { setCaptions((current) => current.filter((caption) => caption.id !== data.id)); return; }
      if (payload.type !== 'transcript.interim' || data.is_final === true) return;
      const key = `${text(data.source_id)}:${text(data.provider_result_id ?? data.id)}`;
      setCaptions((current) => [...current.filter((caption) => caption._key !== key), { ...data, _key: key }].slice(-4));
    }).then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; }).catch(setError);
    return () => { disposed = true; cleanup?.(); };
  }, [meetingId, recording]);
  useEffect(() => {
    setCaptions((current) => { const next = current.filter((caption) => !finalRows.some((row) => row.source_id === caption.source_id && number(row.end_ms) >= number(caption.end_ms))); return next.length === current.length ? current : next; });
  }, [finalRows]);
  return <>{captions.map((caption) => <p key={text(caption._key)} className={styles.preWrap}><Badge tone="warning">Interim — not saved evidence</Badge> {text(caption.text)}</p>)}{Boolean(error) && <ErrorNotice error={error} />}</>;
}

function TranscriptRows({ meetingId, rows, recording }: { meetingId: string; rows: RecordData[]; recording: boolean }) {
  const parent = useRef<HTMLDivElement>(null);
  const previousLength = useRef(rows.length);
  const [follow, setFollow] = useState(true);
  const [unread, setUnread] = useState(0);
  const selected = useInterface((state) => state.selectedUtterance);
  const select = useInterface((state) => state.selectUtterance);
  const [editing, setEditing] = useState<RecordData | null>(null);
  const [menu, setMenu] = useState<{ row: RecordData; action: string } | null>(null);
  const [playbackError, setPlaybackError] = useState<unknown>();
  const media = useMeetingMedia(meetingId, !recording);
  const virtualizer = useVirtualizer({ count: rows.length, getScrollElement: () => parent.current, estimateSize: () => 112, getItemKey: (index) => id(rows[index] ?? {}) || index, overscan: 6 });
  let lastFinal: RecordData | undefined;
  for (let index = rows.length - 1; index >= 0; index -= 1) { const candidate = rows[index]; if (candidate && candidate.is_final !== false && candidate.state !== 'interim') { lastFinal = candidate; break; } }

  useEffect(() => {
    const delta = Math.max(0, rows.length - previousLength.current);
    if (follow && rows.length) virtualizer.scrollToIndex(rows.length - 1, { align: 'end' });
    else if (delta) setUnread((count) => count + delta);
    previousLength.current = rows.length;
  }, [rows.length, follow, virtualizer]);

  useEffect(() => {
    if (!selected) return;
    const index = rows.findIndex((row) => id(row) === selected);
    if (index >= 0) { setFollow(false); virtualizer.scrollToIndex(index, { align: 'center' }); }
  }, [selected, rows, virtualizer]);

  const play = (row: RecordData): void => {
    setPlaybackError(undefined);
    const segment = items(media.data).find((entry) => entry.state === 'committed' && (!row.source_id || entry.source_id === row.source_id) && number(entry.start_ms) <= number(row.start_ms) && number(entry.start_ms) + number(entry.duration_ms) > number(row.start_ms));
    if (!segment) { setPlaybackError(new ApplicationError({ code: 'audio_unavailable', message: 'No retained source audio exists at this transcript timestamp. It may be off, expired, partial or deleted.', retryable: false })); return; }
    void nativeRequest('media_playback', { request: { operation: 'play', media_id: id(segment), position_ms: Math.max(0, number(row.start_ms) - 1000), end_ms: Math.ceil(number(row.end_ms)) + 1000 } }).catch(setPlaybackError);
  };

  if (!rows.length) return <Empty title={recording ? 'Listening for the first utterance' : 'No transcript yet'}>Source-labelled captions appear here after actual capture and ASR acknowledgement. Provider setup and errors are never replaced with generated sample text.</Empty>;
  return <><div className="sr-only" aria-live="polite" aria-atomic="true">{lastFinal ? `${text(lastFinal.speaker_name ?? lastFinal.source, 'Unknown')}: ${text(lastFinal.text)}` : ''}</div>{Boolean(playbackError) && <ErrorNotice error={playbackError} />}<div ref={parent} className={styles.transcriptScroll} role="region" aria-label="Meeting transcript" tabIndex={0} onScroll={() => { const element = parent.current; if (!element) return; const nearEnd = element.scrollHeight - element.scrollTop - element.clientHeight < 90; setFollow(nearEnd); if (nearEnd) setUnread(0); }}><div role="list" aria-label="Transcript utterances" style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>{virtualizer.getVirtualItems().map((virtualRow) => {
    const row = rows[virtualRow.index]; if (!row) return null;
    return <article role="listitem" key={virtualRow.key} ref={virtualizer.measureElement} data-index={virtualRow.index} className={`${styles.utterance} ${selected === id(row) ? styles.selected : ''}`} style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${virtualRow.start}px)` }}><header><button className={styles.timestamp} aria-label={`Play from ${clock(row.start_ms)}`} disabled={recording || row.media_available === false} title={recording ? 'Replay is paused during capture to prevent recording playback.' : 'Play retained audio with one second of context'} onClick={() => play(row)}>{clock(row.start_ms)}</button><DropdownMenu.Root><DropdownMenu.Trigger className={styles.speaker}>{text(row.speaker_name ?? row.speaker_label ?? row.source, 'Unknown speaker')} ▾</DropdownMenu.Trigger><DropdownMenu.Portal><DropdownMenu.Content className={styles.menu} sideOffset={5}>{['Reassign speaker', 'Rename speaker', 'Split utterance', 'Bookmark moment', 'Anchor human note'].map((action) => <DropdownMenu.Item className={styles.menuItem} key={action} onSelect={() => setMenu({ row, action })}>{action}</DropdownMenu.Item>)}</DropdownMenu.Content></DropdownMenu.Portal></DropdownMenu.Root>{(row.manual_lock === true || row.is_manual === true) && <Badge>Manual edit locked</Badge>}{(row.is_final === false || row.state === 'interim') && <Badge tone="warning">Interim</Badge>}</header><p tabIndex={0} aria-label="Transcript text; press Enter to edit" onDoubleClick={() => setEditing(row)} onKeyDown={(event) => { if (event.key === 'Enter') setEditing(row); }}>{text(row.text)}</p><Toolbar><button className={styles.quietButton} onClick={() => setEditing(row)}>Edit text</button><button className={styles.quietButton} onClick={() => { select(id(row)); setMenu({ row, action: 'Bookmark moment' }); }}>Mark important</button>{typeof row.confidence === 'number' && <small>Confidence {Math.round(row.confidence * 100)}%</small>}{row.timing_estimated === true && <small>Estimated timing</small>}</Toolbar></article>;
  })}</div></div>{(!follow || unread > 0) && <div className={styles.jumpLive}><span>{unread ? `${unread} unread utterances` : 'Auto-scroll paused'}</span><button onClick={() => { setFollow(true); setUnread(0); virtualizer.scrollToIndex(rows.length - 1, { align: 'end' }); }}>Jump to {recording ? 'live' : 'end'}</button></div>}{editing && <TranscriptEditor meetingId={meetingId} row={editing} onClose={() => setEditing(null)} />}{menu && <UtteranceAction meetingId={meetingId} row={menu.row} action={menu.action} onClose={() => setMenu(null)} />}</>;
}

function TranscriptEditor({ meetingId, row, onClose }: { meetingId: string; row: RecordData; onClose: () => void }) {
  const [draft, setDraft] = useState(text(row.text));
  const command = useCommand('transcript.edit');
  const save = (): void => command.mutate({ id: id(row), meeting_id: meetingId, expected_revision: revision(row), text: draft }, { onSuccess: onClose });
  return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title="Edit transcript" description="The original ASR revision remains in provenance. Saving locks this correction against late inference and invalidates derived results."><label className={styles.noteBlock}>Transcript text<textarea autoFocus rows={7} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); save(); } if (event.key === 'Escape') onClose(); }} /></label>{command.error && <ErrorNotice error={command.error} />}<Toolbar><button disabled={command.isPending} onClick={save}>Save correction</button><button onClick={onClose}>Cancel</button></Toolbar></Modal>;
}

function UtteranceAction({ meetingId, row, action, onClose }: { meetingId: string; row: RecordData; action: string; onClose: () => void }) {
  const speakers = useResource('speakers.list', { meeting_id: meetingId });
  let operation = 'moments.create';
  let fixed: RecordData = { meeting_id: meetingId, utterance_id: id(row), start_ms: row.start_ms ?? 0, end_ms: row.end_ms ?? 0, source_quote: row.text ?? '', expected_revision: revision(row) };
  let fields = [{ name: 'note', label: 'Human note (optional)', type: 'textarea' as const }, { name: 'include_in_minutes', label: 'Include quote and note in minutes', type: 'checkbox' as const, value: true }];
  if (action === 'Reassign speaker') {
    operation = 'transcript.reassign'; fixed = { id: id(row), meeting_id: meetingId, expected_revision: revision(row) };
    return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title={action}><CommandForm operation={operation} fixed={fixed} fields={[{ name: 'speaker_id', label: 'Speaker', type: 'select', required: true, options: [{ value: '', label: 'Select a speaker' }, ...items(speakers.data).map((speaker) => ({ value: id(speaker), label: text(speaker.display_name ?? speaker.name ?? speaker.label, id(speaker)) }))] }]} onSuccess={onClose} /></Modal>;
  }
  if (action === 'Rename speaker') {
    operation = 'speakers.rename'; fixed = { id: row.speaker_id ?? '', meeting_id: meetingId, expected_revision: row.speaker_revision ?? 1 };
    return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title={action}><CommandForm operation={operation} fixed={fixed} fields={[{ name: 'name', label: 'Speaker display name', value: text(row.speaker_name), required: true }]} onSuccess={onClose} /></Modal>;
  }
  if (action === 'Split utterance') {
    operation = 'transcript.split'; fixed = { id: id(row), meeting_id: meetingId, expected_revision: revision(row) };
    return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title={action} description="Choose a real word boundary when timing exists. Without word timing, your chosen timestamp is explicitly marked estimated."><CommandForm operation={operation} fixed={fixed} fields={[{ name: 'timestamp_ms', label: 'Split timestamp (milliseconds)', type: 'number', value: Math.round((number(row.start_ms) + number(row.end_ms)) / 2), min: number(row.start_ms), max: number(row.end_ms), required: true }, { name: 'text_offset', label: 'Text split character offset', type: 'number', value: Math.floor(text(row.text).length / 2), min: 1, max: text(row.text).length - 1, required: true }]} onSuccess={onClose} /></Modal>;
  }
  if (action === 'Anchor human note') return <AnchoredNote meetingId={meetingId} row={row} onClose={onClose} />;
  return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title={action}><blockquote>{text(row.text)}</blockquote><CommandForm operation={operation} fixed={fixed} fields={fields} onSuccess={onClose} /></Modal>;
}

function AnchoredNote({ meetingId, row, onClose }: { meetingId: string; row: RecordData; onClose: () => void }) {
  const [heading, setHeading] = useState('');
  const [body, setBody] = useState('');
  const client = useQueryClient();
  const save = useMutation({ mutationFn: async () => {
    await flushDrafts();
    const notes = item(await request('notes.get', { meeting_id: meetingId }));
    const blocks = array(notes.blocks);
    return request('notes.save', { meeting_id: meetingId, expected_revision: notes.revision ?? 0, blocks: [...blocks, { id: crypto.randomUUID(), heading, body, order: blocks.length, anchor: { utterance_id: id(row), start_ms: row.start_ms ?? 0 } }] });
  }, onSuccess: async () => { await client.invalidateQueries({ queryKey: ['engine'] }); onClose(); } });
  return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title="Anchor human note"><form onSubmit={(event) => { event.preventDefault(); save.mutate(); }}><label className={styles.noteBlock}>Heading<input value={heading} onChange={(event) => setHeading(event.target.value)} /></label><label className={styles.noteBlock}>Human note<textarea required value={body} onChange={(event) => setBody(event.target.value)} /></label>{save.error && <ErrorNotice error={save.error} />}<button disabled={save.isPending} type="submit">Save anchored note</button></form></Modal>;
}
