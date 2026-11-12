import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { listen } from '@tauri-apps/api/event';
import { ApplicationError, nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useMeetingMedia, useNativeResource, useResource } from '../lib/queries';
import { array, clock, id, item, items, label, number, object, revision, text, type RecordData } from '../lib/data';
import { useCursorResource } from '../lib/paged-query';
import { ActionMenu } from '../components/controls';
import { AsyncView, Badge, buttonClass, CommandButton, CommandForm, Details, EditResource, Empty, ErrorNotice, Modal, Notice, Panel, Toolbar, useOpenState } from '../components/ui';
import { SetupLink } from './providers';
import { useInterface } from '../lib/ui-store';
import { LANGUAGES } from './library';
import styles from './workspace.module.css';

/** The capture channel as people know it, not the engine's `mic` / `system`. */
export const channel = (kind: unknown): string => kind === 'mic' ? 'Microphone' : kind === 'system' ? 'Computer audio' : text(kind);

export function CaptureControls({ meeting }: { meeting: RecordData }) {
  const client = useQueryClient();
  const setPanel = useInterface((state) => state.setPanel);
  const [startOpen, setStartOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [vocabularyReviewed, setVocabularyReviewed] = useState(false);
  const intent = useRef<{ operation: string; payload: RecordData } | null>(null);
  const state = text(meeting.state, 'draft');
  // Devices and recording come from Settings; starting asks nothing unless vocabulary needs review.
  const draft = state === 'draft';
  const devices = useNativeResource('audio_devices', {}, draft);
  const settings = useResource('settings.get', {}, draft);
  const vocabulary = useResource('meetings.vocabulary_check', { meeting_id: id(meeting) }, draft);
  const acknowledgementKey = text(vocabulary.data?.acknowledgement_key);
  const config = object(meeting.config_snapshot);
  const sourceMode = text(config.source, 'both');
  const stored = item(settings.data);
  const saved = object(stored.nonsecret_settings ?? stored.settings);
  const recordAudio = object(saved.recording).enabled === true;
  const deviceRows = items(devices.data).length ? items(devices.data) : array(devices.data?.devices);
  // A saved device that is gone (unplugged) falls back to the Windows default.
  const pick = (kind: string, key: unknown) => text(deviceRows.find((row) => row.kind === kind && key && row.key === key)?.key) || text(deviceRows.find((row) => row.kind === kind && row.is_default)?.key);
  const micKey = pick('mic', object(saved.capture).microphone_device_key);
  const systemKey = pick('system', object(saved.capture).system_device_key);
  const missing = [sourceMode !== 'system' && !micKey && 'microphone', sourceMode !== 'mic' && !systemKey && 'computer audio'].filter(Boolean);
  const command = useMutation({
    mutationFn: async (operation: 'start' | 'pause' | 'resume' | 'stop') => {
      await flushDrafts();
      if (operation === 'start') await nativeRequest('audio_test_stop');
      const sources: RecordData[] = [];
      if (sourceMode !== 'system' && micKey) sources.push({ kind: 'mic', device_key: micKey });
      if (sourceMode !== 'mic' && systemKey) sources.push({ kind: 'system', device_key: systemKey });
      if (!intent.current || intent.current.operation !== operation) intent.current = { operation, payload: { meeting_id: id(meeting), expected_revision: revision(meeting), idempotency_key: crypto.randomUUID(), ...(operation === 'start' ? { sources, participant_notice_acknowledged: true, recording_enabled: recordAudio, recording_notice_acknowledged: recordAudio, ...(vocabularyReviewed && acknowledgementKey ? { vocabulary_acknowledgement: acknowledgementKey } : {}) } : {}) } };
      return request(`meetings.${operation}`, intent.current.payload);
    },
    onSuccess: async (_result, operation) => { intent.current = null; setStartOpen(false); setStopOpen(false); if (operation === 'stop') setPanel('Outcomes'); await client.invalidateQueries({ queryKey: ['engine'] }); },
  });
  const omittedTerms = vocabulary.data?.omitted;
  const omitted = Array.isArray(omittedTerms) ? omittedTerms.map((term) => typeof term === 'string' ? term : label(object(term))) : [];
  const ready = devices.isSuccess && settings.isSuccess && vocabulary.isSuccess && missing.length === 0;
  const autoStart = useInterface((s) => s.autoStart);
  const setAutoStart = useInterface((s) => s.setAutoStart);
  const begin = (): void => { if (omitted.length > 0) setStartOpen(true); else command.mutate('start'); };
  useEffect(() => {
    if (autoStart !== id(meeting) || !draft) return;
    // Missing devices: stop waiting and let the notice below explain.
    if (devices.isSuccess && missing.length > 0) { setAutoStart(null); return; }
    if (ready) { setAutoStart(null); begin(); }
  }, [autoStart, ready, draft, devices.isSuccess, missing.length]);
  const failure = command.error ? <ErrorNotice error={command.error}>{command.error instanceof ApplicationError && command.error.code === 'asr_unavailable' && <SetupLink />}</ErrorNotice> : null;
  return <>
    {/* Only states that can act get a control: processing, stopping, starting and complete show none. */}
    <Toolbar>
      {state === 'draft' && <button data-testid="start-meeting" className={buttonClass.primary} onClick={begin} disabled={!nativeAvailable() || !ready || command.isPending}>{command.isPending && !startOpen ? 'Starting…' : 'Start meeting'}</button>}
      {state === 'recording' && <button disabled={command.isPending} onClick={() => command.mutate('pause')}>Pause</button>}
      {state === 'paused' && <button disabled={command.isPending} onClick={() => command.mutate('resume')}>Resume</button>}
      {(state === 'recording' || state === 'paused') && <button data-testid="stop-meeting" className={buttonClass.primary} disabled={command.isPending} onClick={() => setStopOpen(true)}>Stop meeting</button>}
      {(state === 'interrupted' || state === 'attention_required') && <button data-testid="stop-meeting" className={buttonClass.primary} disabled={command.isPending} onClick={() => command.mutate('stop')}>{state === 'interrupted' ? 'Finish and process' : 'Stop and process'}</button>}
    </Toolbar>
    {draft && devices.isSuccess && missing.length > 0 && <Notice danger>No {missing.join(' or ')} found. Connect one or choose it in Settings.</Notice>}
    {failure}
    <Modal open={startOpen} onOpenChange={setStartOpen} title="Ready to capture?">
      <Notice>Some vocabulary terms will not be used for this meeting: {omitted.join(', ')}</Notice><label className={styles.check}><input type="checkbox" checked={vocabularyReviewed} onChange={(event) => { setVocabularyReviewed(event.target.checked); intent.current = null; }} />I’ve reviewed the omitted terms</label>
      <button className={buttonClass.primary} disabled={command.isPending || !vocabularyReviewed} onClick={() => command.mutate('start')}>{command.isPending ? 'Starting…' : 'Start'}</button>
    {failure}</Modal>
    <Modal open={stopOpen} onOpenChange={setStopOpen} title="Stop this meeting?"><Toolbar><button className={buttonClass.primary} disabled={command.isPending} onClick={() => command.mutate('stop')}>{command.isPending ? 'Stopping…' : 'Stop'}</button><button onClick={() => setStopOpen(false)}>Keep going</button></Toolbar>
    {command.error && <ErrorNotice error={command.error} />}</Modal>
  </>;
}

const STATUS: Record<string, string> = {
  draft: 'Not started',
  starting: 'Starting',
  recording: 'Recording',
  paused: 'Paused',
  stopping: 'Saving the audio',
  processing: 'Writing the summary and minutes',
  attention_required: 'Needs attention',
  interrupted: 'Capture stopped',
};

/** One line under the title: what the meeting is doing now. The clock ticks only while recording. */
export function MeetingStatus({ meeting }: { meeting: RecordData }) {
  const state = text(meeting.state, 'draft');
  const setPanel = useInterface((s) => s.setPanel);
  const live = state === 'recording' || state === 'paused';
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (state !== 'recording') return; const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, [state]);
  const elapsed = state === 'recording' && meeting.started_at ? now - number(meeting.started_at) : number(meeting.elapsed_ms);
  const sources = useResource('entity.list', { collection: 'capture_sources', filters: { meeting_id: id(meeting) } }, live);
  const degraded = state === 'complete' && meeting.health === 'degraded';
  const word = degraded ? 'Some AI results failed' : STATUS[state];
  const tone = state === 'recording' ? styles.live : state === 'attention_required' || state === 'interrupted' || degraded ? styles.attention : '';
  const media = useMeetingMedia(id(meeting), state === 'interrupted');
  const audioKept = items(media.data).some((row) => row.state === 'committed');
  const hint: ReactNode = state === 'attention_required' ? 'Processing stopped on a step. Stop and process, or open Minutes for details.' : state === 'interrupted' ? (audioKept ? 'The audio so far is kept.' : '') : degraded ? <>Retry them under <button type="button" className={buttonClass.quiet} onClick={() => setPanel('Outcomes')}>Outcomes</button></> : '';
  if (!word) return null;
  return <>
    <div className={styles.status} role="status">
      <span className={`${styles.statusWord} ${tone}`}>{word}</span>
      {live && <span className={styles.elapsed}>{clock(elapsed)}</span>}
      {hint && <span>{hint}</span>}
      {state === 'recording' && <AudioMeters />}
      {live && items(sources.data).filter((source) => source.state === 'lost').map((source) => <span key={id(source)} className={styles.sourceState}><Badge tone="danger">{text(source.display_name) || channel(source.source_kind)} lost</Badge><CommandButton operation="meetings.source_retry" payload={{ source_id: id(source), expected_revision: revision(source) }}>Retry this source</CommandButton></span>)}
    </div>
    {Boolean(meeting.interrupted_reason) && state !== 'complete' && <Notice danger>{text(meeting.interrupted_reason)}</Notice>}
  </>;
}

export function AudioMeters() {
  const [levels, setLevels] = useState<{ source: string; rms: number; peak: number }[]>([]);
  useEffect(() => {
    if (!nativeAvailable()) return;
    let disposed = false; let cleanup: (() => void) | undefined;
    void listen<RecordData>('engine-event', ({ payload }) => {
      if (text(payload.type) !== 'native.audio.level') return;
      const data = object(payload.data ?? payload);
      const source = data.source_kind === 'system' ? 'Computer audio' : data.source_kind === 'mic' ? 'Microphone' : 'Level';
      setLevels((current) => [...current.filter((level) => level.source !== source), { source, rms: number(data.rms), peak: number(data.peak) }].slice(-2).sort((a, b) => a.source.localeCompare(b.source)));
    }).then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; });
    return () => { disposed = true; cleanup?.(); };
  }, []);
  return <div className={styles.meters}>{levels.length ? levels.map((level) => <label key={level.source}>{level.source}<meter min={0} max={1} value={level.rms} aria-label={`${level.source} audio level`} />{level.peak >= .98 && <small>Clipping. Lower the input gain.</small>}</label>) : null}</div>;
}

const SPLIT_LINES_SHOWN = 200;

/** Pick the lines that belong to a new speaker; the engine takes their utterance ids. Loads the whole transcript in the background, lists only this speaker's lines. */
function SplitForm({ meetingId, row }: { meetingId: string; row: RecordData }) {
  const query = useCursorResource('transcript.list', { meeting_id: meetingId, limit: 200 });
  const [picked, setPicked] = useState<string[]>([]);
  const [filter, setFilter] = useState('');
  useEffect(() => { if (query.hasNextPage && !query.isFetchingNextPage) void query.fetchNextPage(); }, [query.hasNextPage, query.isFetchingNextPage, query.data?.pages.length]);
  const lines = (query.data?.pages ?? []).flatMap((page) => items(page)).filter((line) => array(line.speaker_assignments).some((assignment) => assignment.speaker_id === id(row)));
  const needle = filter.trim().toLowerCase();
  const matching = needle ? lines.filter((line) => text(line.text).toLowerCase().includes(needle)) : lines;
  const toggle = (lineId: string): void => setPicked((current) => current.includes(lineId) ? current.filter((entry) => entry !== lineId) : [...current, lineId]);
  return <CommandForm operation="speakers.split" fixed={{ meeting_id: meetingId, id: id(row), expected_revision: revision(row), utterance_ids: picked }} fields={[{ name: 'display_name', label: 'New speaker name', required: true }]} mapPayload={(payload) => { if (!picked.length) throw new Error('Choose at least one line to move to the new speaker.'); return payload; }} submit="Split speaker">
    <fieldset className={styles.splitLines}>
      <legend>Lines to move{picked.length > 0 && ` (${picked.length} chosen)`}</legend>
      {lines.length > SPLIT_LINES_SHOWN && <input type="search" aria-label="Filter lines" placeholder="Filter lines" value={filter} onChange={(event) => setFilter(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') event.preventDefault(); }} />}
      {query.isPending ? <small>Loading lines…</small> : query.isError ? <ErrorNotice error={query.error} /> : lines.length === 0 ? <small>This speaker has no lines.</small> : matching.slice(0, SPLIT_LINES_SHOWN).map((line) => <label key={id(line)} className={styles.check}><input type="checkbox" checked={picked.includes(id(line))} onChange={() => toggle(id(line))} /><span>{clock(line.start_ms)} · {text(line.text)}</span></label>)}
      {matching.length > SPLIT_LINES_SHOWN && <small>Showing the first {SPLIT_LINES_SHOWN} of {matching.length} lines. Filter to find the rest.</small>}
    </fieldset>
  </CommandForm>;
}

/** Rename stays on the row; the rarer actions sit in the ⋯ menu and open their own dialog. */
function SpeakerRow({ meetingId, row, speakers, personOptions }: { meetingId: string; row: RecordData; speakers: RecordData[]; personOptions: { value: string; label: string }[] }) {
  const [dialog, setDialog] = useState<'merge' | 'split' | 'link' | null>(null);
  const bind = (name: 'merge' | 'split' | 'link') => ({ open: dialog === name, onOpenChange: (open: boolean) => setDialog(open ? name : null) });
  const name = text(row.display_name, 'Unknown speaker');
  return <article className={styles.actionRow}>
    <div><strong>{name}</strong><Badge>{channel(row.source_kind) || 'Meeting-scoped'}</Badge></div>
    <Toolbar>
      <EditResource operation="speakers.rename" row={row} fixed={{ meeting_id: meetingId }} label="Rename speaker" fields={[{ name: 'display_name', label: 'Display name', required: true }]} />
      <ActionMenu label={`More actions for ${name}`} actions={[{ label: 'Merge speaker…', onSelect: () => setDialog('merge') }, { label: 'Split speaker…', onSelect: () => setDialog('split') }, { label: 'Link to person…', onSelect: () => setDialog('link') }]} />
    </Toolbar>
    <Modal title="Merge speaker" {...bind('merge')}><CommandForm operation="speakers.merge" fixed={{ meeting_id: meetingId, source_id: id(row), expected_revision: revision(row) }} fields={[{ name: 'target_id', label: 'Merge into', type: 'select', required: true, options: [{ value: '', label: 'Choose speaker' }, ...speakers.filter((target) => id(target) !== id(row)).map((target) => ({ value: id(target), label: label(target) }))] }]} submit="Merge speaker" confirm="Merge into the selected speaker." /></Modal>
    <Modal title="Split speaker" {...bind('split')}><SplitForm meetingId={meetingId} row={row} /></Modal>
    <Modal title="Link to person" {...bind('link')}><CommandForm operation="voice_profiles.adopt" fixed={{ speaker_id: id(row), expected_revision: revision(row), confirm: true }} fields={[{ name: 'person_id', label: 'Person', type: 'select', options: personOptions, required: true }]} submit="Link to person" confirm="I confirm this speaker is the selected person." /></Modal>
  </article>;
}

export function SpeakersPanel({ meetingId }: { meetingId: string }) {
  const speakers = useResource('speakers.list', { meeting_id: meetingId });
  const analytics = useResource('speakers.analytics', { meeting_id: meetingId });
  const people = useResource('people.list');
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [showUnused, setShowUnused] = useState(false);
  const personOptions = [{ value: '', label: 'Choose person' }, ...items(people.data).map((person) => ({ value: id(person), label: label(person) }))];
  return <Panel>
    <AsyncView query={speakers}>{(data) => {
      const all = items(data);
      // Talk time comes from the speaking stats; until they load, every speaker counts as used.
      const unused = analytics.isSuccess ? all.filter((row) => { const stats = items(analytics.data).find((entry) => entry.speaker_id === id(row)); return number(stats?.turns) === 0 && number(stats?.talk_time_ms) === 0; }) : [];
      const speakerRow = (row: RecordData) => <SpeakerRow key={id(row)} meetingId={meetingId} row={row} speakers={all} personOptions={personOptions} />;
      return all.length === 0 ? <Empty title="No speakers yet" /> : <>{all.filter((row) => !unused.includes(row)).map(speakerRow)}{unused.length > 0 && <button type="button" className={buttonClass.quiet} aria-expanded={showUnused} onClick={() => setShowUnused(!showUnused)}>{showUnused ? 'Hide' : 'Show'} unused speakers ({unused.length})</button>}{showUnused && unused.map(speakerRow)}</>;
    }}</AsyncView>
    <h3>Speaking stats</h3>
    <AsyncView query={analytics}>{(data) => <><div className={styles.statsGrid}>{items(data).map((row) => <section key={id(row)}><strong>{text(row.display_name, 'Unknown')}</strong><dl><dt>Talk time</dt><dd>{clock(row.talk_time_ms)}</dd><dt>Turns</dt><dd>{number(row.turns)}</dd><dt>Overlap</dt><dd>{clock(row.overlap_ms)}</dd><dt>Interruptions</dt><dd>{text(row.interruptions, '—')}</dd><dt>Speaking rate</dt><dd>{typeof row.words_per_minute === 'number' ? `${row.words_per_minute.toFixed(1)} words/min` : '—'}</dd><dt>Filler count</dt><dd>{number(row.filler_count)}</dd><dt>Participation</dt><dd>{typeof row.participation === 'number' ? `${Math.round(row.participation * 100)}%` : '—'}</dd></dl></section>)}</div><Details value={data} /></>}</AsyncView>
    <Modal open={profilesOpen} onOpenChange={setProfilesOpen} trigger={<button>Create voice profile…</button>} title="Create voice profile"><CommandForm operation="voice_profiles.create" fixed={{ consent: true }} fields={[{ name: 'person_id', label: 'Person', type: 'select', options: personOptions, required: true }, { name: 'meeting_speaker_id', label: 'Speaker', type: 'select', options: [{ value: '', label: 'Choose speaker' }, ...items(speakers.data).map((row) => ({ value: id(row), label: label(row) }))], required: true }, { name: 'scope', label: 'Scope', type: 'select', value: 'project', options: [{ value: 'project', label: 'Selected project' }, { value: 'workspace', label: 'Workspace' }] }, { name: 'project_id', label: 'Project ID (for project scope)' }, { name: 'trusted_auto_match', label: 'Trust automatic matches', type: 'checkbox', value: false }]} submit="Create voice profile" confirm="I consent to storing this person's voice data on this device." onSuccess={() => setProfilesOpen(false)} /></Modal>
  </Panel>;
}

export function LanguageControl({ meeting, open: controlledOpen, onOpenChange }: { meeting: RecordData; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const { open, setOpen, controlled } = useOpenState(controlledOpen, onOpenChange);
  const current = text(object(meeting.config_snapshot).output_language, 'auto');
  const options = [{ value: 'auto', label: 'Automatic' }, ...LANGUAGES.map(([value, name]) => ({ value: value as string, label: name as string }))];
  if (!options.some((option) => option.value === current)) options.push({ value: current, label: current });
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>Summary language</button>} title="Summary language"><CommandForm operation="language.set" fixed={{ meeting_id: id(meeting), expected_revision: revision(meeting) }} fields={[{ name: 'language', label: 'Language', type: 'select', value: current, options, required: true }]} submit="Save language" /></Modal>;
}

