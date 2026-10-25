import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { listen } from '@tauri-apps/api/event';
import { ApplicationError, nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useNativeResource, useResource } from '../lib/queries';
import { array, clock, id, items, label, number, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, Panel, Toolbar, useOpenState } from '../components/ui';
import { SetupLink } from './providers';
import styles from './workspace.module.css';

export function CaptureControls({ meeting }: { meeting: RecordData }) {
  const client = useQueryClient();
  const [startOpen, setStartOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [mic, setMic] = useState('');
  const [system, setSystem] = useState('');
  const [recordAudio, setRecordAudio] = useState(false);
  const [vocabularyReviewed, setVocabularyReviewed] = useState(false);
  const intent = useRef<{ operation: string; payload: RecordData } | null>(null);
  const devices = useNativeResource('audio_devices', {}, startOpen);
  const vocabulary = useResource('meetings.vocabulary_check', { meeting_id: id(meeting) }, startOpen);
  const acknowledgementKey = text(vocabulary.data?.acknowledgement_key);
  const config = object(meeting.config_snapshot);
  const sourceMode = text(config.source, 'both');
  const state = text(meeting.state, 'draft');
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
    onSuccess: async () => { intent.current = null; setStartOpen(false); setStopOpen(false); await client.invalidateQueries({ queryKey: ['engine'] }); },
  });
  const deviceRows = items(devices.data).length ? items(devices.data) : array(devices.data?.devices);
  const micKey = mic || text(deviceRows.find((row) => row.kind === 'mic' && row.is_default)?.key);
  const systemKey = system || text(deviceRows.find((row) => row.kind === 'system' && row.is_default)?.key);
  const options = (kind: string) => [{ value: '', label: 'Choose device' }, ...deviceRows.filter((row) => row.kind === kind).map((row) => ({ value: text(row.key), label: row.is_default ? `${text(row.name)} (Windows default)` : text(row.name) }))];
  const omittedTerms = vocabulary.data?.omitted;
  const omitted = Array.isArray(omittedTerms) ? omittedTerms.map((term) => typeof term === 'string' ? term : label(object(term))) : [];
  const failure = command.error ? <ErrorNotice error={command.error}>{command.error instanceof ApplicationError && command.error.code === 'asr_unavailable' && <SetupLink />}</ErrorNotice> : null;
  return <>
    <Toolbar>
      {state === 'draft' && <button data-testid="start-meeting" onClick={() => setStartOpen(true)} disabled={!nativeAvailable()}>Start meeting</button>}
      {state === 'recording' && <button disabled={command.isPending} onClick={() => command.mutate('pause')}>Pause capture</button>}
      {state === 'paused' && <button disabled={command.isPending} onClick={() => command.mutate('resume')}>Resume capture</button>}
      {['recording', 'paused', 'interrupted', 'attention_required'].includes(state) && <button data-testid="stop-meeting" disabled={command.isPending} onClick={() => setStopOpen(true)}>{state === 'interrupted' ? 'Finish meeting' : 'Stop meeting'}</button>}
      {state === 'starting' && <Badge tone="warning">Starting…</Badge>}
      {state === 'stopping' && <Badge tone="warning">Saving…</Badge>}
    </Toolbar>
    {failure}
    <Modal open={startOpen} onOpenChange={setStartOpen} title="Ready to capture?"><AsyncView query={devices}>{() => <>
      {sourceMode !== 'system' && <FieldInput field={{ name: 'mic', label: 'Microphone', type: 'select', options: options('mic') }} value={micKey} onChange={(value) => { setMic(text(value)); intent.current = null; }} />}
      {sourceMode !== 'mic' && <FieldInput field={{ name: 'system', label: 'System audio', type: 'select', options: options('system') }} value={systemKey} onChange={(value) => { setSystem(text(value)); intent.current = null; }} />}
      <label className={styles.check}><input type="checkbox" checked={recordAudio} onChange={(event) => { setRecordAudio(event.target.checked); intent.current = null; }} />Keep the audio recording</label>
      {omitted.length > 0 && <><Notice>Some vocabulary terms will not be used for this meeting: {omitted.join(', ')}</Notice><label className={styles.check}><input type="checkbox" checked={vocabularyReviewed} onChange={(event) => { setVocabularyReviewed(event.target.checked); intent.current = null; }} />I've reviewed the omitted terms</label></>}
      <button disabled={command.isPending || (sourceMode !== 'system' && !micKey) || (sourceMode !== 'mic' && !systemKey) || (omitted.length > 0 && !vocabularyReviewed)} onClick={() => command.mutate('start')}>{command.isPending ? 'Starting…' : 'Start'}</button>
    </>}</AsyncView>
    {failure}</Modal>
    <Modal open={stopOpen} onOpenChange={setStopOpen} title="Stop this meeting?"><Toolbar><button disabled={command.isPending} onClick={() => command.mutate('stop')}>{command.isPending ? 'Stopping…' : 'Stop'}</button><button onClick={() => setStopOpen(false)}>Keep going</button></Toolbar>
    {command.error && <ErrorNotice error={command.error} />}</Modal>
  </>;
}

export function CaptureStatus({ meeting, children }: { meeting: RecordData; children?: ReactNode }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const recording = meeting.state === 'recording';
  const elapsed = recording && meeting.started_at ? now - number(meeting.started_at) : number(meeting.elapsed_ms);
  const sources = useResource('entity.list', { collection: 'capture_sources', filters: { meeting_id: id(meeting) } });
  const done = meeting.state === 'complete';
  return <div className={styles.captureStrip}><div>{!done && <Badge tone={recording ? 'success' : meeting.health === 'degraded' ? 'warning' : 'neutral'}>{text(meeting.state, 'draft').replaceAll('_', ' ')}</Badge>}<strong className={styles.elapsed}>{clock(elapsed)}</strong></div><AudioMeters /><div>{items(sources.data).filter((source) => !done || source.state === 'lost').map((source) => <span key={id(source)} className={styles.sourceState}><Badge tone={source.state === 'lost' ? 'danger' : 'neutral'}>{text(source.display_name) || text(source.source_kind)}: {text(source.state)}</Badge>{source.state === 'lost' && <CommandButton operation="meetings.source_retry" payload={{ source_id: id(source), expected_revision: revision(source) }}>Retry this source</CommandButton>}</span>)}</div>{children}{meeting.interrupted_reason && <Notice danger>{text(meeting.interrupted_reason)}</Notice>}</div>;
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
      setLevels((current) => [...current.filter((level) => level.source !== source), { source, rms: number(data.rms), peak: number(data.peak) }].slice(-2));
    }).then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; });
    return () => { disposed = true; cleanup?.(); };
  }, []);
  return <div className={styles.meters}>{levels.length ? levels.map((level) => <label key={level.source}>{level.source}<meter min={0} max={1} value={level.rms} aria-label={`${level.source} audio level`} />{level.peak >= .98 && <small>Clipping — lower the input gain</small>}</label>) : null}</div>;
}

export function SpeakersPanel({ meetingId }: { meetingId: string }) {
  const speakers = useResource('speakers.list', { meeting_id: meetingId });
  const analytics = useResource('speakers.analytics', { meeting_id: meetingId });
  const people = useResource('people.list');
  const [profilesOpen, setProfilesOpen] = useState(false);
  const personOptions = [{ value: '', label: 'Choose person' }, ...items(people.data).map((person) => ({ value: id(person), label: label(person) }))];
  return <Panel title="Speakers">
    <AsyncView query={speakers}>{(data) => items(data).length === 0 ? <Empty title="No speakers yet" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}>
      <div><strong>{text(row.display_name, 'Unknown speaker')}</strong><Badge>{text(row.source_kind, 'Meeting-scoped')}</Badge></div>
      <Toolbar>
        <EditResource operation="speakers.rename" row={row} fixed={{ meeting_id: meetingId }} label="Rename speaker" fields={[{ name: 'display_name', label: 'Display name', required: true }]} />
        <Modal trigger={<button>Merge speaker…</button>} title="Merge speaker"><CommandForm operation="speakers.merge" fixed={{ meeting_id: meetingId, source_id: id(row), expected_revision: revision(row) }} fields={[{ name: 'target_id', label: 'Merge into', type: 'select', required: true, options: [{ value: '', label: 'Choose speaker' }, ...items(data).filter((target) => id(target) !== id(row)).map((target) => ({ value: id(target), label: label(target) }))] }]} confirm="Merge into the selected speaker." /></Modal>
        <Modal trigger={<button>Split speaker…</button>} title="Split speaker"><CommandForm operation="speakers.split" fixed={{ meeting_id: meetingId, id: id(row), expected_revision: revision(row) }} fields={[{ name: 'display_name', label: 'New speaker name', required: true }, { name: 'utterance_ids', label: 'Utterance IDs (JSON array)', type: 'json', value: [], required: true }]} /></Modal>
        <Modal trigger={<button>Link to person…</button>} title="Link to person"><CommandForm operation="voice_profiles.adopt" fixed={{ speaker_id: id(row), expected_revision: revision(row), confirm: true }} fields={[{ name: 'person_id', label: 'Person', type: 'select', options: personOptions, required: true }]} confirm="I confirm this speaker is the selected person." /></Modal>
      </Toolbar>
    </article>)}</AsyncView>
    <h3>Speaking stats</h3>
    <AsyncView query={analytics}>{(data) => <><div className={styles.statsGrid}>{items(data).map((row) => <section key={id(row)}><strong>{text(row.display_name, 'Unknown')}</strong><dl><dt>Talk time</dt><dd>{clock(row.talk_time_ms)}</dd><dt>Turns</dt><dd>{number(row.turns)}</dd><dt>Overlap</dt><dd>{clock(row.overlap_ms)}</dd><dt>Interruptions</dt><dd>{text(row.interruptions, 'Not measured')}</dd><dt>Speaking rate</dt><dd>{typeof row.words_per_minute === 'number' ? row.words_per_minute.toFixed(1) : 'Unavailable'} words/min</dd><dt>Filler count</dt><dd>{number(row.filler_count)}</dd><dt>Participation</dt><dd>{typeof row.participation === 'number' ? `${Math.round(row.participation * 100)}%` : 'Unavailable'}</dd></dl></section>)}</div><Details value={data} /></>}</AsyncView>
    <Modal open={profilesOpen} onOpenChange={setProfilesOpen} trigger={<button>Create voice profile…</button>} title="Create voice profile"><CommandForm operation="voice_profiles.create" fixed={{ consent: true }} fields={[{ name: 'person_id', label: 'Person', type: 'select', options: personOptions, required: true }, { name: 'meeting_speaker_id', label: 'Speaker', type: 'select', options: [{ value: '', label: 'Choose speaker' }, ...items(speakers.data).map((row) => ({ value: id(row), label: label(row) }))], required: true }, { name: 'scope', label: 'Scope', type: 'select', value: 'project', options: [{ value: 'project', label: 'Selected project' }, { value: 'workspace', label: 'Workspace' }] }, { name: 'project_id', label: 'Project ID (for project scope)' }, { name: 'trusted_auto_match', label: 'Trust automatic matches', type: 'checkbox', value: false }]} confirm="I consent to storing this person's voice data on this device." onSuccess={() => setProfilesOpen(false)} /></Modal>
  </Panel>;
}

export function LanguageControl({ meeting, open: controlledOpen, onOpenChange }: { meeting: RecordData; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const { open, setOpen, controlled } = useOpenState(controlledOpen, onOpenChange);
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>Output language</button>} title="Generated output language"><CommandForm operation="language.set" fixed={{ meeting_id: id(meeting), expected_revision: revision(meeting) }} fields={[{ name: 'language', label: 'Language (BCP-47)', value: text(object(meeting.config_snapshot).output_language, 'auto'), required: true }]} /></Modal>;
}

