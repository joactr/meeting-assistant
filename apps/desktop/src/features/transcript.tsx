import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { listen } from '@tauri-apps/api/event';
import { useCommand, useMeetingMedia, useResource } from '../lib/queries';
import { ApplicationError, nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useCursorResource } from '../lib/paged-query';
import { flushDrafts } from '../lib/drafts';
import { useInterface } from '../lib/ui-store';
import { array, clock, id, item, items, number, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandForm, Empty, ErrorNotice, Modal, Notice, Panel, Toolbar } from '../components/ui';
import styles from './workspace.module.css';

export function Transcript({ meetingId, recording }: { meetingId: string; recording: boolean }) {
  const query = useCursorResource('transcript.list', { meeting_id: meetingId, limit: 200 });
  const speakers = useResource('speakers.list', { meeting_id: meetingId });
  const sources = useResource('entity.list', { collection: 'capture_sources', filters: { meeting_id: meetingId } });
  // entity.list returns 50 rows by default: past the 50th gap, dropped speech showed no badge.
  const gaps = useResource('entity.list', { collection: 'timeline_gaps', filters: { meeting_id: meetingId }, limit: 200 });
  const media = useMeetingMedia(meetingId, !recording);
  const selected = useInterface((state) => state.selectedUtterance);
  const rows: RecordData[] = query.data?.pages.flatMap((page) => items(page)).map((row) => {
    const assignments = array(row.speaker_assignments);
    const speaker = items(speakers.data).find((person) => id(person) === assignments[0]?.speaker_id);
    const source = items(sources.data).find((entry) => id(entry) === row.source_id);
    const retained = items(media.data).find((entry) => entry.source_id === row.source_id && number(entry.start_ms) <= number(row.start_ms) && number(entry.start_ms) + number(entry.duration_ms) > number(row.start_ms) && entry.state === 'committed');
    return { ...row, speaker_id: speaker?.id ?? null, speaker_revision: speaker?.revision ?? null, speaker_name: assignments.length > 1 ? 'Multiple speakers' : speaker?.display_name ?? (source?.source_kind === 'mic' ? 'Me' : source?.source_kind === 'system' ? 'System' : 'Unknown speaker'), media_id: retained?.id ?? null, media_available: !!retained, manual_lock: !!row.manual_text_lock, timing_estimated: row.timing_provenance === 'user_estimated' };
  }) ?? [];
  useEffect(() => {
    if (query.hasNextPage && !query.isFetchingNextPage && (recording || (selected && !rows.some((row) => id(row) === selected)))) void query.fetchNextPage();
  }, [query.hasNextPage, query.isFetchingNextPage, rows.length, selected, recording]);
  // Unacknowledged gaps (lost sources, dropped speech) sit in the timeline; pauses are deliberate.
  const loadedUntil = query.hasNextPage ? number(rows[rows.length - 1]?.start_ms) : Infinity;
  const timeline: RecordData[] = [...rows, ...items(gaps.data).filter((gap) => !gap.acknowledged && number(gap.start_ms) <= loadedUntil).map((gap): RecordData => ({ ...gap, id: `gap:${id(gap)}`, kind: 'gap' }))].sort((a, b) => number(a.start_ms) - number(b.start_ms));
  const combined = { ...query, data: { items: timeline } };
  return <Panel><AsyncView query={combined}>{() => <TranscriptRows meetingId={meetingId} rows={timeline} recording={recording} />}</AsyncView><InterimCaptions meetingId={meetingId} recording={recording} finalRows={rows} />{query.hasNextPage && <button disabled={query.isFetchingNextPage} onClick={() => { void query.fetchNextPage(); }}>{query.isFetchingNextPage ? 'Loading…' : 'Load more'}</button>}</Panel>;
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
  return <>{captions.map((caption) => <p key={text(caption._key)} className={styles.preWrap}><Badge tone="warning">Interim</Badge> {text(caption.text)}</p>)}{Boolean(error) && <ErrorNotice error={error} />}</>;
}

function TranscriptRows({ meetingId, rows, recording }: { meetingId: string; rows: RecordData[]; recording: boolean }) {
  const parent = useRef<HTMLDivElement>(null);
  const previousLength = useRef(rows.length);
  // A finished transcript opens at its start; a live one follows the newest line.
  const [follow, setFollow] = useState(recording);
  const [unread, setUnread] = useState(0);
  const selected = useInterface((state) => state.selectedUtterance);
  const select = useInterface((state) => state.selectUtterance);
  const [editing, setEditing] = useState<RecordData | null>(null);
  const [menu, setMenu] = useState<{ row: RecordData; action: string } | null>(null);
  const [playbackError, setPlaybackError] = useState<unknown>();
  const media = useMeetingMedia(meetingId, !recording);
  // The transcript scrolls inside a pane that ends at the window's bottom edge, leaving room for
  // whatever follows it in the tab (replay player, Load more), so the header and controls stay put.
  const [height, setHeight] = useState<number>();
  const hasRows = rows.length > 0;
  useLayoutEffect(() => {
    const pane = parent.current;
    const panel = pane?.closest('[role="tabpanel"]') ?? pane;
    if (!pane || !panel) return;
    const main = pane.closest('main');
    const padding = main ? parseFloat(getComputedStyle(main).paddingBottom) : 0;
    const fit = (): void => { const box = pane.getBoundingClientRect(); setHeight(Math.round(window.innerHeight - box.top - window.scrollY - (panel.getBoundingClientRect().bottom - box.bottom) - padding)); };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(panel);
    observer.observe(document.body);
    window.addEventListener('resize', fit);
    return () => { observer.disconnect(); window.removeEventListener('resize', fit); };
  }, [hasRows]);
  const virtualizer = useVirtualizer({ count: rows.length, estimateSize: () => 112, getItemKey: (index) => id(rows[index] ?? {}) || index, overscan: 6, getScrollElement: () => parent.current });
  let lastFinal: RecordData | undefined;
  for (let index = rows.length - 1; index >= 0; index -= 1) { const candidate = rows[index]; if (candidate && candidate.kind !== 'gap' && candidate.is_final !== false && candidate.state !== 'interim') { lastFinal = candidate; break; } }

  useEffect(() => {
    const delta = Math.max(0, rows.length - previousLength.current);
    if (follow && rows.length) virtualizer.scrollToIndex(rows.length - 1, { align: 'end' });
    else if (delta) setUnread((count) => count + delta);
    previousLength.current = rows.length;
  }, [rows.length, follow, virtualizer]);

  useEffect(() => { if (recording) setFollow(true); }, [recording]);

  // Scroll to a selection once; re-scrolling on every new live row would cancel "Jump to live".
  const scrolledTo = useRef<string | null>(null);
  useEffect(() => {
    if (!selected || scrolledTo.current === selected) return;
    const index = rows.findIndex((row) => id(row) === selected);
    if (index >= 0) { scrolledTo.current = selected; setFollow(false); virtualizer.scrollToIndex(index, { align: 'center' }); }
  }, [selected, rows, virtualizer]);

  const [renaming, setRenaming] = useState<string | null>(null);
  // Ctrl+F: null while the find bar is closed.
  const [find, setFind] = useState<string | null>(null);
  const [hit, setHit] = useState(0);
  const matchesFor = (query: string): number[] => query.trim() ? rows.flatMap((row, index) => row.kind !== 'gap' && text(row.text).toLowerCase().includes(query.trim().toLowerCase()) ? [index] : []) : [];
  const matches = matchesFor(find ?? '');
  const show = (list: number[], at: number): void => { const index = list[at]; const row = rows[index ?? -1]; if (index === undefined || !row) return; setHit(at); scrolledTo.current = id(row); select(id(row)); setFollow(false); virtualizer.scrollToIndex(index, { align: 'center' }); };
  const step = (delta: number): void => show(matches, (hit + delta + matches.length) % matches.length);
  const findInput = useRef<HTMLInputElement>(null);
  // Closing the find bar hands focus back to where it was, else the transcript itself.
  const opener = useRef<HTMLElement | null>(null);
  const closeFind = (): void => { setFind(null); const back = opener.current?.isConnected && opener.current !== document.body ? opener.current : parent.current; back?.focus(); };
  const [findRequest, setFindRequest] = useState(0);
  useEffect(() => { if (findRequest) { findInput.current?.focus(); findInput.current?.select(); } }, [findRequest]);
  useEffect(() => {
    const open = (): void => { const active = document.activeElement; if (active instanceof HTMLElement && !active.closest('[role="search"]')) opener.current = active; setFind((current) => current ?? ''); setFindRequest((count) => count + 1); };
    window.addEventListener('transcript-find', open);
    return () => window.removeEventListener('transcript-find', open);
  }, []);

  // Ctrl+Shift+B marks the newest final line important without leaving the keyboard.
  const mark = useCommand('moments.create');
  const latest = useRef(lastFinal);
  latest.current = lastFinal;
  useEffect(() => {
    const handler = (): void => { const row = latest.current; if (row) mark.mutate({ ...momentFor(meetingId, row), include_in_minutes: true }); };
    window.addEventListener('meeting-mark-moment', handler);
    return () => window.removeEventListener('meeting-mark-moment', handler);
  }, [meetingId, mark.mutate]);
  useEffect(() => { if (!mark.isSuccess) return; const timer = setTimeout(mark.reset, 3000); return () => clearTimeout(timer); }, [mark.isSuccess]);

  const play = (row: RecordData): void => {
    setPlaybackError(undefined);
    const segment = items(media.data).find((entry) => entry.state === 'committed' && (!row.source_id || entry.source_id === row.source_id) && number(entry.start_ms) <= number(row.start_ms) && number(entry.start_ms) + number(entry.duration_ms) > number(row.start_ms));
    if (!segment) { setPlaybackError(new ApplicationError({ code: 'audio_unavailable', message: 'No audio saved for this moment.', retryable: false })); return; }
    void nativeRequest('media_playback', { request: { operation: 'play', media_id: id(segment), position_ms: Math.max(0, number(row.start_ms) - 1000), end_ms: Math.ceil(number(row.end_ms)) + 1000 } }).catch(setPlaybackError);
  };

  if (!rows.length) return <Empty title={recording ? 'Listening for the first utterance' : 'No transcript yet'} />;
  return <><div className="sr-only" aria-live="polite" aria-atomic="true">{lastFinal ? `${text(lastFinal.speaker_name ?? lastFinal.source, 'Unknown')}: ${text(lastFinal.text)}` : ''}</div>{Boolean(playbackError) && <ErrorNotice error={playbackError} />}{mark.isSuccess && <Notice>Marked as important</Notice>}{mark.error && <ErrorNotice error={mark.error} />}{find !== null && <div className={styles.findBar} role="search"><input ref={findInput} id="transcript-find" aria-label="Find in transcript" placeholder="Find in transcript" value={find} onChange={(event) => { setFind(event.target.value); show(matchesFor(event.target.value), 0); }} onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); closeFind(); } else if (event.key === 'Enter' && matches.length) { event.preventDefault(); step(event.shiftKey ? -1 : 1); } }} /><span className={styles.findCount} aria-live="polite">{find.trim() ? matches.length ? `${hit + 1} of ${matches.length}` : 'No matches' : ''}</span><button type="button" className={styles.findStep} aria-label="Previous match" title="Previous match" disabled={!matches.length} onClick={() => step(-1)}><ChevronUp size={16} aria-hidden="true" /></button><button type="button" className={styles.findStep} aria-label="Next match" title="Next match" disabled={!matches.length} onClick={() => step(1)}><ChevronDown size={16} aria-hidden="true" /></button><button type="button" onClick={closeFind}>Close</button></div>}<div ref={parent} className={styles.transcriptScroll} style={{ height: height }} onScroll={() => { const element = parent.current; if (!element) return; const nearEnd = element.scrollHeight - element.scrollTop - element.clientHeight < 90; setFollow(nearEnd); if (nearEnd) setUnread(0); }} role="region" aria-label="Meeting transcript" tabIndex={0}><div role="list" aria-label="Transcript utterances" style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>{virtualizer.getVirtualItems().map((virtualRow) => {
    const row = rows[virtualRow.index]; if (!row) return null;
    if (row.kind === 'gap') return <article role="listitem" key={virtualRow.key} ref={virtualizer.measureElement} data-index={virtualRow.index} className={styles.utterance} style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${virtualRow.start}px)` }}><header><span className={styles.timestamp}>{clock(row.start_ms)}</span><Badge tone="warning">Audio gap</Badge></header></article>;
    const speakerName = text(row.speaker_name ?? row.speaker_label ?? row.source, 'Unknown speaker');
    return <article role="listitem" key={virtualRow.key} ref={virtualizer.measureElement} data-index={virtualRow.index} className={`${styles.utterance} ${selected === id(row) ? styles.selected : ''}`} style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${virtualRow.start}px)` }}><header><button className={styles.timestamp} aria-label={`Play from ${clock(row.start_ms)}`} disabled={recording || row.media_available === false} title={recording ? 'Paused while recording' : 'Play audio'} onClick={() => play(row)}>{clock(row.start_ms)}</button>{renaming === id(row) ? <SpeakerRename meetingId={meetingId} row={row} onDone={() => setRenaming(null)} /> : row.speaker_id ? <button className={styles.speaker} title="Rename this speaker on every line" onClick={() => setRenaming(id(row))}>{speakerName}</button> : <span className={styles.speaker}>{speakerName}</span>}{(row.manual_lock === true || row.is_manual === true) && <Badge>Edited</Badge>}{(row.is_final === false || row.state === 'interim') && <Badge tone="warning">Interim</Badge>}{typeof row.confidence === 'number' && row.confidence < 0.6 && <small className={styles.lowConfidence}>Low confidence</small>}{row.timing_estimated === true && <small>Estimated timing</small>}<span className={styles.rowActions}><button className={styles.quietButton} onClick={() => setEditing(row)}>Edit</button><button className={styles.quietButton} onClick={() => { select(id(row)); setMenu({ row, action: 'Mark important' }); }}>Mark important</button><DropdownMenu.Root><DropdownMenu.Trigger className={styles.quietButton}>More</DropdownMenu.Trigger><DropdownMenu.Portal><DropdownMenu.Content className={styles.menu} sideOffset={5}>{['Reassign speaker', 'Split utterance', 'Anchor note'].map((action) => <DropdownMenu.Item className={styles.menuItem} key={action} onSelect={() => setMenu({ row, action })}>{action === 'Anchor note' ? 'Add note here…' : `${action}…`}</DropdownMenu.Item>)}</DropdownMenu.Content></DropdownMenu.Portal></DropdownMenu.Root></span></header><p tabIndex={0} aria-label="Transcript text; press Enter to edit" onDoubleClick={() => setEditing(row)} onKeyDown={(event) => { if (event.key === 'Enter') setEditing(row); }}>{highlight(text(row.text), find)}</p></article>;
  })}</div>{recording && (!follow || unread > 0) && <div className={styles.jumpLive}><span>{unread ? `${unread} unread utterances` : 'Auto-scroll paused'}</span><button onClick={() => { setFollow(true); setUnread(0); virtualizer.scrollToIndex(rows.length - 1, { align: 'end' }); }}>Jump to live</button></div>}</div>{editing && <TranscriptEditor meetingId={meetingId} row={editing} onClose={() => setEditing(null)} />}{menu && <UtteranceAction meetingId={meetingId} row={menu.row} action={menu.action} onClose={() => setMenu(null)} />}</>;
}

/** A new important moment for one line; it is a new record, so no expected_revision. */
function momentFor(meetingId: string, row: RecordData): RecordData {
  return { meeting_id: meetingId, utterance_id: id(row), start_ms: row.start_ms ?? 0, label: text(row.speaker_name ?? row.speaker_label ?? row.source), quote_snapshot: row.text ?? '' };
}

function highlight(value: string, query: string | null): ReactNode {
  const needle = query?.trim();
  if (!needle) return value;
  return value.split(new RegExp(`(${needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'i')).map((part, index) => index % 2 ? <mark key={index}>{part}</mark> : part);
}

/** Inline speaker rename: the name applies to every line of that speaker. */
function SpeakerRename({ meetingId, row, onDone }: { meetingId: string; row: RecordData; onDone: () => void }) {
  const [name, setName] = useState(text(row.speaker_name ?? row.speaker_label));
  const command = useCommand('speakers.rename');
  const save = (): void => { const next = name.trim(); if (!next || next === text(row.speaker_name)) { onDone(); return; } command.mutate({ id: row.speaker_id ?? '', meeting_id: meetingId, expected_revision: row.speaker_revision ?? 1, name: next }, { onSuccess: onDone }); };
  return <><input autoFocus aria-label="Speaker name" className={styles.speakerInput} value={name} disabled={command.isPending} onChange={(event) => setName(event.target.value)} onFocus={(event) => event.target.select()} onBlur={save} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); save(); } if (event.key === 'Escape') { event.preventDefault(); onDone(); } }} />{command.error && <ErrorNotice error={command.error} />}</>;
}

function TranscriptEditor({ meetingId, row, onClose }: { meetingId: string; row: RecordData; onClose: () => void }) {
  const [draft, setDraft] = useState(text(row.text));
  const command = useCommand('transcript.edit');
  const save = (): void => command.mutate({ id: id(row), meeting_id: meetingId, expected_revision: revision(row), text: draft }, { onSuccess: onClose });
  return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title="Edit transcript"><label className={styles.noteBlock}>Transcript text<textarea autoFocus rows={7} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); save(); } if (event.key === 'Escape') onClose(); }} /></label>{command.error && <ErrorNotice error={command.error} />}<Toolbar><button disabled={command.isPending} onClick={save}>Save</button><button onClick={onClose}>Cancel</button></Toolbar></Modal>;
}

function UtteranceAction({ meetingId, row, action, onClose }: { meetingId: string; row: RecordData; action: string; onClose: () => void }) {
  const speakers = useResource('speakers.list', { meeting_id: meetingId });
  let operation = 'moments.create';
  let fixed: RecordData = momentFor(meetingId, row);
  let fields = [{ name: 'note', label: 'Note (optional)', type: 'textarea' as const }, { name: 'include_in_minutes', label: 'Include in minutes', type: 'checkbox' as const, value: true }];
  if (action === 'Reassign speaker') {
    operation = 'transcript.reassign'; fixed = { id: id(row), meeting_id: meetingId, expected_revision: revision(row) };
    return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title={action}><CommandForm operation={operation} fixed={fixed} fields={[{ name: 'speaker_id', label: 'Speaker', type: 'select', required: true, options: [{ value: '', label: 'Choose speaker' }, ...items(speakers.data).map((speaker) => ({ value: id(speaker), label: text(speaker.display_name ?? speaker.name ?? speaker.label, id(speaker)) }))] }]} submit="Reassign speaker" onSuccess={onClose} /></Modal>;
  }
  if (action === 'Split utterance') {
    operation = 'transcript.split'; fixed = { id: id(row), meeting_id: meetingId, expected_revision: revision(row) };
    return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title={action}><CommandForm operation={operation} fixed={fixed} fields={[{ name: 'timestamp_ms', label: 'Split at (ms)', type: 'number', value: Math.round((number(row.start_ms) + number(row.end_ms)) / 2), min: number(row.start_ms), max: number(row.end_ms), required: true }, { name: 'text_offset', label: 'Split after character', type: 'number', value: Math.floor(text(row.text).length / 2), min: 1, max: text(row.text).length - 1, required: true }]} submit="Split utterance" onSuccess={onClose} /></Modal>;
  }
  if (action === 'Anchor note') return <AnchoredNote meetingId={meetingId} row={row} onClose={onClose} />;
  return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title={action}><blockquote>{text(row.text)}</blockquote><CommandForm operation={operation} fixed={fixed} fields={fields} mapPayload={(payload) => ({ ...payload, note: payload.note ?? '' })} submit="Mark important" onSuccess={onClose} /></Modal>;
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
  return <Modal open onOpenChange={(open) => { if (!open) onClose(); }} title="Add note here"><form onSubmit={(event) => { event.preventDefault(); save.mutate(); }}><label className={styles.noteBlock}>Heading<input value={heading} onChange={(event) => setHeading(event.target.value)} /></label><label className={styles.noteBlock}>Note<textarea required value={body} onChange={(event) => setBody(event.target.value)} /></label>{save.error && <ErrorNotice error={save.error} />}<button disabled={save.isPending} type="submit">Save note</button></form></Modal>;
}
