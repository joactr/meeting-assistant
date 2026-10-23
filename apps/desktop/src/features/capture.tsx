import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { listen } from '@tauri-apps/api/event';
import { ApplicationError, nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useNativeResource, useResource } from '../lib/queries';
import { array, clock, id, items, label, number, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, Panel, Toolbar, useOpenState } from '../components/ui';
import styles from './workspace.module.css';

export function CaptureControls({ meeting }: { meeting: RecordData }) {
  const client = useQueryClient();
  const [startOpen, setStartOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [mic, setMic] = useState('');
  const [system, setSystem] = useState('');
  const [permission, setPermission] = useState(false);
  const [recordAudio, setRecordAudio] = useState(false);
  const [vocabularyAcknowledgement, setVocabularyAcknowledgement] = useState('');
  const intent = useRef<{ operation: string; payload: RecordData } | null>(null);
  const devices = useNativeResource('audio_devices', {}, startOpen);
  const config = object(meeting.config_snapshot);
  const sourceMode = text(config.source, 'both');
  const state = text(meeting.state, 'draft');
  const command = useMutation({
    mutationFn: async (operation: 'start' | 'pause' | 'resume' | 'stop') => {
      await flushDrafts();
      if (operation === 'start') await nativeRequest('audio_test_stop');
      const sources: RecordData[] = [];
      if (sourceMode !== 'system' && mic) sources.push({ kind: 'mic', device_key: mic });
      if (sourceMode !== 'mic' && system) sources.push({ kind: 'system', device_key: system });
      if (!intent.current || intent.current.operation !== operation) intent.current = { operation, payload: { meeting_id: id(meeting), expected_revision: revision(meeting), idempotency_key: crypto.randomUUID(), ...(operation === 'start' ? { sources, participant_notice_acknowledged: permission, recording_enabled: recordAudio, recording_notice_acknowledged: permission, ...(vocabularyAcknowledgement ? { vocabulary_acknowledgement: vocabularyAcknowledgement } : {}) } : {}) } };
      return request(`meetings.${operation}`, intent.current.payload);
    },
    onSuccess: async () => { intent.current = null; setStartOpen(false); setStopOpen(false); await client.invalidateQueries({ queryKey: ['engine'] }); },
  });
  const deviceRows = items(devices.data).length ? items(devices.data) : array(devices.data?.devices);
  const options = (kind: string) => [{ value: '', label: 'Choose a stable device — no automatic substitution' }, ...deviceRows.filter((row) => row.kind === kind).map((row) => ({ value: text(row.key), label: text(row.name) }))];
  const vocabularyIssue = command.error instanceof ApplicationError && command.error.code === 'vocabulary_acknowledgement_required' ? command.error.details : null;
  return <>
    <Toolbar>
      {state === 'draft' && <button data-testid="start-meeting" onClick={() => setStartOpen(true)} disabled={!nativeAvailable()}>Start meeting</button>}
      {state === 'recording' && <button disabled={command.isPending} onClick={() => command.mutate('pause')}>Pause capture</button>}
      {state === 'paused' && <button disabled={command.isPending} onClick={() => command.mutate('resume')}>Resume capture</button>}
      {['recording', 'paused', 'interrupted', 'attention_required'].includes(state) && <button data-testid="stop-meeting" disabled={command.isPending} onClick={() => setStopOpen(true)}>{state === 'interrupted' ? 'Preserve & finalize' : 'Stop & preserve'}</button>}
      {state === 'starting' && <Badge tone="warning">Waiting for device acknowledgement</Badge>}
      {state === 'stopping' && <Badge tone="warning">Draining and saving…</Badge>}
    </Toolbar>
    {command.error && <ErrorNotice error={command.error} />}
    <Modal open={startOpen} onOpenChange={setStartOpen} title="Ready to capture?" description="Start is explicit. Selected ASR must pass actual preflight. Optional AI/speaker failures do not silently switch providers or block unrelated human editing.">
      <AsyncView query={devices}>{() => <>
        <Notice>Capture sources: {sourceMode}. This app does not establish other participants' consent. Participant notice/legal compliance remains your responsibility. Audio retention is a separate opt-in below and cannot be enabled retroactively.</Notice>
        {sourceMode !== 'system' && <FieldInput field={{ name: 'mic', label: 'Microphone', type: 'select', options: options('mic') }} value={mic} onChange={(value) => { setMic(text(value)); intent.current = null; }} />}
        {sourceMode !== 'mic' && <FieldInput field={{ name: 'system', label: 'System endpoint', type: 'select', options: options('system') }} value={system} onChange={(value) => { setSystem(text(value)); intent.current = null; }} />}
        <label className={styles.check}><input type="checkbox" checked={recordAudio} onChange={(event) => { setRecordAudio(event.target.checked); intent.current = null; }} />Retain separate microphone/system audio under the configured retention and media cap. I reviewed participant recording permission.</label>
        <label className={styles.check}><input type="checkbox" checked={permission} onChange={(event) => { setPermission(event.target.checked); intent.current = null; }} />I checked participant notice/permission, selected devices and the actual processing destinations.</label>
        {vocabularyIssue && <><Details title="Exact effective vocabulary and omitted terms requiring review" value={vocabularyIssue} /><button onClick={() => { setVocabularyAcknowledgement(text(vocabularyIssue.acknowledgement_key)); intent.current = null; }}>Acknowledge this exact omitted vocabulary set</button></>}
        {vocabularyAcknowledgement && <Badge>Omissions explicitly reviewed for this compilation</Badge>}
        <button disabled={!permission || command.isPending || (sourceMode !== 'system' && !mic) || (sourceMode !== 'mic' && !system)} onClick={() => command.mutate('start')}>{command.isPending ? 'Checking and opening sources…' : 'Start selected sources'}</button>
      </>}</AsyncView>
      {command.error && <ErrorNotice error={command.error} />}
    </Modal>
    <Modal open={stopOpen} onOpenChange={setStopOpen} title="Stop and preserve this meeting?" description="Notes flush first. Capture stops only after source boundary acknowledgement and durable preservation. Finalization stays visible and retryable.">
      <Toolbar><button disabled={command.isPending} onClick={() => command.mutate('stop')}>{command.isPending ? 'Preserving…' : 'Confirm stop & preserve'}</button><button onClick={() => setStopOpen(false)}>Continue meeting</button></Toolbar>
      {command.error && <ErrorNotice error={command.error} />}
    </Modal>
  </>;
}

export function CaptureStatus({ meeting, children }: { meeting: RecordData; children?: ReactNode }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const recording = meeting.state === 'recording';
  const elapsed = recording && meeting.started_at ? now - number(meeting.started_at) : number(meeting.elapsed_ms);
  const sources = useResource('entity.list', { collection: 'capture_sources', filters: { meeting_id: id(meeting) } });
  return <div className={styles.captureStrip}><div><Badge tone={recording ? 'success' : meeting.health === 'degraded' ? 'warning' : 'neutral'}>{text(meeting.state, 'draft').replaceAll('_', ' ')}</Badge><strong className={styles.elapsed}>{clock(elapsed)}</strong></div><AudioMeters /><div>{items(sources.data).map((source) => <span key={id(source)} className={styles.sourceState}><Badge tone={source.state === 'lost' ? 'danger' : 'neutral'}>{text(source.display_name ?? source.source_kind)}: {text(source.state)}</Badge>{source.state === 'lost' && <CommandButton operation="meetings.source_retry" payload={{ source_id: id(source), expected_revision: revision(source) }}>Retry this source</CommandButton>}</span>)}</div>{children}{meeting.interrupted_reason && <Notice danger>{text(meeting.interrupted_reason)}</Notice>}</div>;
}

export function AudioMeters() {
  const [levels, setLevels] = useState<{ source: string; rms: number; peak: number }[]>([]);
  useEffect(() => {
    if (!nativeAvailable()) return;
    let disposed = false; let cleanup: (() => void) | undefined;
    void listen<RecordData>('engine-event', ({ payload }) => {
      if (text(payload.type) !== 'native.audio.level') return;
      const data = object(payload.data ?? payload);
      const source = text(data.source_kind ?? data.source_index, 'Source');
      setLevels((current) => [...current.filter((level) => level.source !== source), { source, rms: number(data.rms), peak: number(data.peak) }].slice(-2));
    }).then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; });
    return () => { disposed = true; cleanup?.(); };
  }, []);
  return <div className={styles.meters}>{levels.length ? levels.map((level) => <label key={level.source}>{level.source}<meter min={0} max={1} value={level.rms} aria-label={`${level.source} audio level`} /><small>{level.peak >= .98 ? 'Clipping — lower input gain' : 'Signal level'}</small></label>) : null}</div>;
}

export function SpeakersPanel({ meetingId }: { meetingId: string }) {
  const speakers = useResource('speakers.list', { meeting_id: meetingId });
  const analytics = useResource('speakers.analytics', { meeting_id: meetingId });
  const people = useResource('people.list');
  const [profilesOpen, setProfilesOpen] = useState(false);
  const personOptions = [{ value: '', label: 'Select an explicitly created person' }, ...items(people.data).map((person) => ({ value: id(person), label: label(person) }))];
  return <Panel title="Speakers & participation">
    <AsyncView query={speakers}>{(data) => items(data).length === 0 ? <Empty title="Source labels until speaker evidence exists">Diarization is opt-in and never delays captions.</Empty> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}>
      <div><strong>{text(row.display_name, 'Unknown speaker')}</strong><Badge>{text(row.source_kind, 'Meeting-scoped')}</Badge></div>
      <Toolbar>
        <EditResource operation="speakers.rename" row={row} fixed={{ meeting_id: meetingId }} label="Rename speaker" fields={[{ name: 'display_name', label: 'Display name', required: true }]} />
        <Modal trigger={<button>Merge speaker…</button>} title="Merge speaker assignments" description="Remaps assignments transactionally without changing transcript IDs or pretending to separate audio."><CommandForm operation="speakers.merge" fixed={{ meeting_id: meetingId, source_id: id(row), expected_revision: revision(row) }} fields={[{ name: 'target_id', label: 'Merge into speaker', type: 'select', required: true, options: [{ value: '', label: 'Choose a different speaker' }, ...items(data).filter((target) => id(target) !== id(row)).map((target) => ({ value: id(target), label: label(target) }))] }]} confirm="Merge assignments while preserving manual identity decisions." /></Modal>
        <Modal trigger={<button>Split speaker…</button>} title="Split selected speaker utterances"><CommandForm operation="speakers.split" fixed={{ meeting_id: meetingId, id: id(row), expected_revision: revision(row) }} fields={[{ name: 'display_name', label: 'New speaker name', required: true }, { name: 'utterance_ids', label: 'Selected utterance IDs (JSON array)', type: 'json', value: [], required: true }]} /></Modal>
        <Modal trigger={<button>Adopt recurring person…</button>} title="Explicitly adopt a person identity" description="Your reviewed label decision does not claim a voice match proves identity."><CommandForm operation="voice_profiles.adopt" fixed={{ speaker_id: id(row), expected_revision: revision(row), confirm: true }} fields={[{ name: 'person_id', label: 'Reviewed person', type: 'select', options: personOptions, required: true }]} confirm="I reviewed this speaker and explicitly adopt the selected person identity." /></Modal>
      </Toolbar>
    </article>)}</AsyncView>
    <h3>Speaking coaching</h3>
    <AsyncView query={analytics}>{(data) => <><div className={styles.statsGrid}>{items(data).map((row) => <section key={id(row)}><strong>{text(row.display_name, 'Unknown')}</strong><dl><dt>Talk time</dt><dd>{clock(row.talk_time_ms)}</dd><dt>Turns</dt><dd>{number(row.turns)}</dd><dt>Overlap</dt><dd>{clock(row.overlap_ms)}</dd><dt>Interruptions</dt><dd>{text(row.interruptions, 'Unavailable — not measured')}</dd><dt>Speaking rate</dt><dd>{typeof row.words_per_minute === 'number' ? row.words_per_minute.toFixed(1) : 'Unavailable'} words/min</dd><dt>Filler count</dt><dd>{number(row.filler_count)}</dd><dt>Participation</dt><dd>{typeof row.participation === 'number' ? `${Math.round(row.participation * 100)}%` : 'Unavailable'}</dd></dl></section>)}</div><Details value={data} title="Formula, balance and uncertainty" /></>}</AsyncView>
    <Modal open={profilesOpen} onOpenChange={setProfilesOpen} trigger={<button>Opt into recurring voice profile…</button>} title="Sensitive voice profile opt-in" description="Voice vectors are biometric-sensitive data. Enrollment requires actual diarization evidence, stays local and is scoped explicitly. It does not guarantee identity.">
      <CommandForm operation="voice_profiles.create" fixed={{ consent: true }} fields={[{ name: 'person_id', label: 'Consenting person', type: 'select', options: personOptions, required: true }, { name: 'meeting_speaker_id', label: 'Speaker evidence to enroll', type: 'select', options: [{ value: '', label: 'Choose speaker' }, ...items(speakers.data).map((row) => ({ value: id(row), label: label(row) }))], required: true }, { name: 'scope', label: 'Matching scope', type: 'select', value: 'project', options: [{ value: 'project', label: 'Selected project' }, { value: 'workspace', label: 'Workspace' }] }, { name: 'project_id', label: 'Project ID (required for project scope)' }, { name: 'trusted_auto_match', label: 'Explicitly trust automatic matches for this profile', type: 'checkbox', value: false }]} confirm="I explicitly consent to storing this sensitive voice vector in the selected matching scope." onSuccess={() => setProfilesOpen(false)} />
    </Modal>
  </Panel>;
}

export function LanguageControl({ meeting, open: controlledOpen, onOpenChange }: { meeting: RecordData; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const { open, setOpen, controlled } = useOpenState(controlledOpen, onOpenChange);
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>Output language</button>} title="Generated output language" description="Changes apply to new generated output, not stored transcript, quotes, names or human notes. In-flight older-policy output cannot replace a newer selection."><CommandForm operation="language.set" fixed={{ meeting_id: id(meeting), expected_revision: revision(meeting) }} fields={[{ name: 'language', label: 'Language (BCP-47)', value: text(object(meeting.config_snapshot).output_language, 'auto'), required: true }]} /></Modal>;
}

