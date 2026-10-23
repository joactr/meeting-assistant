import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest } from '../lib/bridge';
import { useMeetingMedia, useResource } from '../lib/queries';
import { clock, id, items, label, number, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandForm, ConfirmCommand, Details, Empty, ErrorNotice, FieldInput, Modal, Notice, Panel, Toolbar } from '../components/ui';
import styles from './workspace.module.css';

export function ReplayPanel({ meetingId, recording }: { meetingId: string; recording: boolean }) {
  const media = useMeetingMedia(meetingId);
  const captureSources = useResource('entity.list', { collection: 'capture_sources', filters: { meeting_id: meetingId } });
  const [mediaId, setMediaId] = useState('');
  const [speed, setSpeed] = useState(1);
  const [muted, setMuted] = useState<string[]>([]);
  const [focused, setFocused] = useState(false);
  const client = useQueryClient();
  const player = useQuery({ queryKey: ['native', 'playback', meetingId], queryFn: () => nativeRequest('media_playback', { request: { operation: 'state' } }), enabled: nativeAvailable() && !!mediaId, refetchInterval: focused ? 750 : 2500, retry: false });
  const control = useMutation({ mutationFn: (operation: RecordData) => nativeRequest('media_playback', { request: { media_id: mediaId, ...operation } }), onSuccess: (result) => client.setQueryData(['native', 'playback', meetingId], result) });
  useEffect(() => { if (recording && nativeAvailable()) control.mutate({ operation: 'pause' }); }, [recording]);
  useEffect(() => () => { if (nativeAvailable()) void nativeRequest('media_playback', { request: { operation: 'close' } }).catch(() => undefined); }, [meetingId]);
  const state = object(player.data);
  const segments: RecordData[] = items(media.data).map((row) => ({ ...row, end_ms: number(row.start_ms) + number(row.duration_ms), source_kind: text(items(captureSources.data).find((source) => id(source) === row.source_id)?.source_kind, text(row.source_id, 'file')) }));
  const playable = segments.filter((row) => row.state === 'committed');
  const chosen = playable.find((row) => id(row) === mediaId);
  const timelineEnd = playable.reduce((end, segment) => Math.max(end, number(segment.end_ms)), 0);
  const toggle = (): void => control.mutate({ operation: state.playing ? 'pause' : 'play', position_ms: number(state.position_ms, number(chosen?.start_ms)), speed });
  return <Panel title="Retained audio" actions={<Badge>{recording ? 'Replay paused during capture' : 'Native playback'}</Badge>}>
    <AsyncView query={media}>{() => playable.length === 0 ? <Empty title="Audio unavailable">Recording may have been off, expired, partial or deleted. Transcripts/citations remain usable. Re-transcription requires retained audio or an explicitly granted matching original.</Empty> : <div role="region" aria-label="Native audio player" className={styles.player} tabIndex={0} onFocus={() => setFocused(true)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false); }} onKeyDown={(event) => { if (event.key === ' ' && !['INPUT', 'SELECT', 'BUTTON', 'TEXTAREA'].includes((event.target as HTMLElement).tagName)) { event.preventDefault(); if (mediaId && !recording) toggle(); } }}>
      <FieldInput field={{ name: 'media', label: 'Stored source segment', type: 'select', options: [{ value: '', label: 'Select retained track' }, ...playable.map((row) => ({ value: id(row), label: `${row.source_kind} · ${clock(row.start_ms)}–${clock(row.end_ms)} · stored` }))] }} value={mediaId} onChange={(value) => { setMediaId(text(value)); control.reset(); }} />
      <Toolbar>
        <button disabled={!mediaId || recording || control.isPending} onClick={toggle}>{state.playing ? 'Pause replay' : 'Play selected track'}</button>
        <label>Speed<select value={speed} disabled={!mediaId || recording} onChange={(event) => { const value = Number(event.target.value); setSpeed(value); control.mutate({ operation: 'speed', speed: value }); }}>{[.75, 1, 1.25, 1.5, 2].map((value) => <option key={value} value={value}>{value}×</option>)}</select></label>
        <span>{clock(state.position_ms)} / {clock(timelineEnd)}</span>
      </Toolbar>
      <label>Seek<input type="range" aria-label="Replay position" min={0} max={Math.max(timelineEnd, 1)} value={number(state.position_ms)} disabled={!mediaId || recording} onChange={(event) => control.mutate({ operation: 'seek', position_ms: Number(event.target.value) })} /></label>
      <Toolbar>{['mic', 'system'].map((source) => <label className={styles.check} key={source}><input type="checkbox" checked={!muted.includes(source)} disabled={!mediaId || recording} onChange={(event) => { const next = event.target.checked ? muted.filter((entry) => entry !== source) : [...muted, source]; setMuted(next); control.mutate({ operation: 'sources', muted_sources: next }); }} />{source} audible</label>)}</Toolbar>
      {chosen && <Details value={chosen} title="Actual storage, retention, expiry and partial-media state" />}
    </div>}</AsyncView>
    {(control.error || player.error) && <ErrorNotice error={control.error ?? player.error} />}
    <Details title="All indexed media, including unavailable or expired segments" value={{ items: segments }} />
    <Toolbar><Retranscribe meetingId={meetingId} hasMedia={playable.length > 0} disabled={recording} />{chosen && <ConfirmCommand trigger="Delete selected retained segment" title="Delete selected media?" message="Closes playback handles and queues physical purge. The transcript/citations remain. Unlinking is not secure erasure; Windows open-file errors remain actionable." operation="media.delete" payload={{ id: id(chosen), expected_revision: revision(chosen), confirm: true }} />}</Toolbar>
  </Panel>;
}

function Retranscribe({ meetingId, hasMedia, disabled }: { meetingId: string; hasMedia: boolean; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [grant, setGrant] = useState<RecordData | null>(null);
  const [grantError, setGrantError] = useState<unknown>();
  const profiles = useResource('entity.list', { collection: 'model_profiles' }, open);
  return <Modal open={open} onOpenChange={setOpen} trigger={<button disabled={disabled}>Re-transcribe…</button>} title="Create a transcript candidate" description="The active transcript never changes during generation. Review the complete diff and manual correction conflicts before explicit promotion.">
    {!hasMedia && <><Notice>No retained audio exists. A newly granted matching original is required.</Notice><button onClick={() => { void nativeRequest('file_grant', { purpose: 'import_audio' }).then((selected) => setGrant(selected.cancelled ? null : selected)).catch(setGrantError); }}>Choose matching original</button></>}
    {grant && <Badge>Original file explicitly granted</Badge>}
    {Boolean(grantError) && <ErrorNotice error={grantError} />}
    {profiles.error && <ErrorNotice error={profiles.error} />}
    {(hasMedia || grant) && <CommandForm operation="media.retranscribe" fixed={{ meeting_id: meetingId, ...(grant ? { file_grant: grant.grant_id ?? '' } : {}), idempotency_key: crypto.randomUUID() }} fields={[
      { name: 'profile_id', label: 'Explicit ASR profile', type: 'select', options: [{ value: '', label: 'Choose ASR profile' }, ...items(profiles.data).filter((profile) => profile.role === 'asr').map((profile) => ({ value: id(profile), label: label(profile) }))], required: true },
      { name: 'start_ms', label: 'Range start (ms; 0 for full)', type: 'number', value: 0, min: 0 },
      { name: 'end_ms', label: 'Range end (ms; blank for full)', type: 'number', min: 0 },
      { name: 'language', label: 'Language / auto', value: 'auto' },
      { name: 'temporary_decode_consent', label: 'Allow private temporary decoded audio, removed on completion/cancel', type: 'checkbox', value: false, required: true },
    ]} mapPayload={(payload) => { const { profile_id, end_ms, ...rest } = payload; return { ...rest, asr_policy: { model_profile_id: profile_id ?? '' }, ...(typeof end_ms === 'number' ? { end_ms } : {}) }; }} submit="Generate separate candidate" confirm="I reviewed the selected provider destination/cost and authorize this audio processing." onSuccess={() => setOpen(false)} />}
  </Modal>;
}

export function CandidatePanel({ meetingId }: { meetingId: string }) {
  const runs = useResource('entity.list', { collection: 'transcript_runs', filters: { meeting_id: meetingId } });
  const [candidateId, setCandidateId] = useState('');
  const selected = items(runs.data).find((run) => id(run) === candidateId);
  const diff = useResource('transcript.diff', { meeting_id: meetingId, run_id: candidateId }, !!candidateId);
  return <Panel title="Transcript candidates & review">
    <AsyncView query={runs}>{(data) => <>
      <FieldInput field={{ name: 'candidate', label: 'Completed candidate run', type: 'select', options: [{ value: '', label: 'Choose a completed candidate' }, ...items(data).filter((run) => run.state === 'candidate' && !!run.completed_at).map((run) => ({ value: id(run), label: `${id(run).slice(0, 8)} · ${text(run.source)} · ${text(run.state)}` }))] }} value={candidateId} onChange={(value) => setCandidateId(text(value))} />
      {!items(data).some((run) => run.state === 'candidate' && !!run.completed_at) && <Empty title="No complete candidate yet">Generation never streams over active edits. In-progress and failed jobs stay in the meeting job panel.</Empty>}
    </>}</AsyncView>
    {selected && <AsyncView query={diff}>{(data) => <>
      <pre className={styles.preWrap} aria-label="Complete active-to-candidate transcript diff">{Array.isArray(data.diff) ? data.diff.map((line) => text(line)).join('\n') : text(data.diff, 'No text differences')}</pre>
      <Details title="Manual correction conflicts and source/run mapping consequences" value={data} />
      <CommandForm operation="transcript.promote" fixed={{ meeting_id: meetingId, run_id: candidateId, expected_revision: revision(selected) }} fields={[{ name: 'acknowledge_manual_conflicts', label: 'I reviewed every listed conflict and the resolved transcript preserving manual corrections and speaker locks', type: 'checkbox', value: false, required: items({ items: data.manual_conflicts }).length > 0 }]} submit="Promote reviewed completed candidate" confirm="I reviewed the complete resolved diff and mapping/stale-source consequences." onSuccess={() => setCandidateId('')} />
    </>}</AsyncView>}
  </Panel>;
}
