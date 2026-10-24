import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useNativeResource, useResource } from '../lib/queries';
import { updatePreferences } from '../lib/preferences';
import { id, item, items, object, revision, text, type Payload, type RecordData } from '../lib/data';
import { accuracyNotice, AsyncView, Badge, CommandForm, ConfirmCommand, Details, Empty, ErrorNotice, FieldInput, Modal, Notice, PageHeader, Panel, TabSet, Toolbar, type Field } from '../components/ui';
import { AudioMeters } from './capture';
import { CalendarSettings, IntegrationSettings } from './connections';
import { ModelSettings, ProviderSettings } from './providers';
import { PrivacySettings } from './privacy';
import styles from './workspace.module.css';

const groups: { id: string; title: string; notice?: string; fields: Field[] }[] = [
  { id: 'capture', title: 'Capture processing', fields: [
    { name: 'mode', label: 'Default capture mode', type: 'select', value: 'both', options: ['both', 'mic', 'system'].map((value) => ({ value, label: value })) },
    { name: 'microphone_device_key', label: 'Selected microphone stable device ID' },
    { name: 'system_device_key', label: 'Selected system endpoint stable ID' },
    { name: 'vad_threshold', label: 'VAD threshold', type: 'number', value: .5, min: 0, max: 1 },
    { name: 'pre_roll_ms', label: 'Pre-roll (ms)', type: 'number', value: 300, min: 0, max: 2000 },
    { name: 'post_silence_ms', label: 'Post-silence (ms)', type: 'number', value: 400, min: 0, max: 3000 },
    { name: 'minimum_true_speech_ms', label: 'Minimum true speech (ms, excluding padding)', type: 'number', value: 250, min: 0, max: 2000 },
    { name: 'maximum_utterance_ms', label: 'Maximum utterance (ms)', type: 'number', value: 12000, min: 1000, max: 30000 },
    { name: 'noise_suppression', label: 'Optional microphone noise suppression', type: 'checkbox', value: false },
    { name: 'echo_cancellation', label: 'Calibrated acoustic echo cancellation', type: 'checkbox', value: false },
    { name: 'source_failure_policy', label: 'Source failure policy', type: 'select', value: 'warn', options: [{ value: 'warn', label: 'Persistent warning; user chooses retry/pause/continue' }, { value: 'pause', label: 'Pause capture on source loss' }] },
  ] },
  { id: 'recording', title: 'Recording & retention', notice: 'Recording is off by default. You are responsible for participant permission.', fields: [
    { name: 'enabled', label: 'Retain separate source audio tracks', type: 'checkbox', value: false },
    { name: 'retain_days', label: 'Audio retention', type: 'select', value: '7', options: [{ value: '1', label: '1 day' }, { value: '7', label: '7 days (default)' }, { value: '30', label: '30 days' }, { value: 'forever', label: 'Keep until manual deletion' }] },
    { name: 'byte_cap', label: 'Workspace media cap (bytes)', type: 'number', value: 10737418240, min: 104857600 },
    { name: 'max_import_bytes', label: 'Maximum file import bytes', type: 'number', value: 2147483648, min: 1024 },
    { name: 'max_import_seconds', label: 'Maximum import duration (seconds)', type: 'number', value: 28800, min: 1 },
  ] },
  { id: 'analysis', title: 'Analysis & language', fields: [
    { name: 'output_language', label: 'Output language (BCP-47)', value: 'auto' },
    { name: 'topic_sentiment', label: 'Optional descriptive topic sentiment', type: 'checkbox', value: false },
    { name: 'live_cues_enabled', label: 'Live transcript-grounded cues', type: 'checkbox', value: true },
    { name: 'question_detection_enabled', label: 'Question detection', type: 'checkbox', value: true },
    { name: 'rolling_summary_seconds', label: 'Rolling summary interval (seconds)', type: 'number', value: 60, min: 60 },
    { name: 'checkpoint_seconds', label: 'Topics/outcomes checkpoint (seconds)', type: 'number', value: 300, min: 60 },
  ] },
  { id: 'speakers', title: 'Speaker processing', fields: [
    { name: 'diarization_enabled', label: 'Enable selected ready diarization', type: 'checkbox', value: false },
    { name: 'voice_profiles_enabled', label: 'Opt into scoped recurring voice matching', type: 'checkbox', value: false },
    { name: 'match_threshold', label: 'Model-calibrated match threshold', type: 'number', value: .72, min: 0, max: 1 },
    { name: 'match_margin', label: 'Minimum match margin', type: 'number', value: .08, min: 0, max: 1 },
  ] },
  { id: 'retrieval', title: 'Retrieval & indexing', fields: [
    { name: 'mode', label: 'Default retrieval', type: 'select', value: 'keyword', options: ['keyword', 'semantic', 'hybrid'].map((value) => ({ value, label: value })) },
    { name: 'embedding_profile_id', label: 'Explicit embedding model profile ID' },
    { name: 'semantic_enabled', label: 'Enable configured semantic index', type: 'checkbox', value: false },
    { name: 'chunk_tokens', label: 'Chunk token target', type: 'number', value: 256, min: 32, max: 512 },
    { name: 'overlap_tokens', label: 'Chunk overlap tokens', type: 'number', value: 32, min: 0, max: 128 },
  ] },
  { id: 'jobs', title: 'Advanced bounded jobs', fields: [
    { name: 'max_concurrent_inference', label: 'Concurrent inference jobs', type: 'number', value: 1, min: 1, max: 4 },
    { name: 'queue_limit', label: 'Bounded pending jobs', type: 'number', value: 100, min: 1, max: 1000 },
    { name: 'worker_startup_seconds', label: 'Worker startup deadline (seconds)', type: 'number', value: 120, min: 1, max: 600 },
    { name: 'pyannote_startup_seconds', label: 'Community-1 startup deadline', type: 'number', value: 180, min: 1, max: 600 },
    { name: 'shutdown_grace_seconds', label: 'Cooperative shutdown grace', type: 'number', value: 5, min: 1, max: 60 },
  ] },
  { id: 'privacy', title: 'Privacy policy', fields: [
    { name: 'network_disabled', label: 'Offline privacy mode: deny outbound provider/integration requests', type: 'checkbox', value: false },
    { name: 'diagnostic_retention_days', label: 'Local diagnostic retention days', type: 'number', value: 14, min: 1, max: 14 },
    { name: 'max_log_bytes', label: 'Rotated log size (bytes)', type: 'number', value: 5242880, min: 1024, max: 5242880 },
  ] },
  { id: 'exports', title: 'Export defaults', fields: [
    { name: 'include_transcript', label: 'Include full transcript only when explicitly selected', type: 'checkbox', value: false },
    { name: 'include_provenance', label: 'Include source provenance', type: 'checkbox', value: true },
    { name: 'default_format', label: 'Preferred format', type: 'select', value: 'markdown', options: ['markdown', 'pdf', 'srt', 'json'].map((value) => ({ value, label: value })) },
  ] },
];

export function SettingsPage() {
  return <><PageHeader title="Settings" /><TabSet tabs={[
    { label: 'Capture & test', content: <><AudioSettings /><SettingsGroup groupId="capture" /></> },
    { label: 'Recording', content: <SettingsGroup groupId="recording" /> },
    { label: 'Providers & routing', content: <ProviderSettings /> },
    { label: 'Models / runtimes', content: <ModelSettings /> },
    { label: 'Analysis & language', content: <><SettingsGroup groupId="analysis" /><SettingsGroup groupId="retrieval" /><SettingsGroup groupId="jobs" /><SettingsGroup groupId="exports" /></> },
    { label: 'Speakers / profiles', content: <><SettingsGroup groupId="speakers" /><Profiles /></> },
    { label: 'Calendar', content: <CalendarSettings /> },
    { label: 'Integrations', content: <IntegrationSettings /> },
    { label: 'Privacy & backup', content: <><SettingsGroup groupId="privacy" /><PrivacySettings /></> },
    { label: 'Window', content: <WindowSettings /> },
    { label: 'Updates & diagnostics', content: <UpdatesSettings /> },
    { label: 'About', content: <About /> },
  ]} /></>;
}

const credits = [
  ['Whisper small', 'OpenAI; CTranslate2 conversion by SYSTRAN', 'MIT', 'https://huggingface.co/Systran/faster-whisper-small'],
  ['Whisper large-v3-turbo', 'OpenAI; CTranslate2 conversion by Mobius Labs', 'MIT', 'https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo'],
  ['Whisper large-v3', 'OpenAI; CTranslate2 conversion by SYSTRAN', 'MIT', 'https://huggingface.co/Systran/faster-whisper-large-v3'],
  ['Parakeet TDT 0.6B v3', 'NVIDIA; converted to ONNX and int8-quantized by sherpa-onnx (modified)', 'CC BY 4.0 (creativecommons.org/licenses/by/4.0)', 'https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3'],
  ['Phonon-2.5', 'Fermion Research; re-trained for 25 languages and compressed from NVIDIA Parakeet TDT 0.6B v3 (modified)', 'CC BY 4.0 (creativecommons.org/licenses/by/4.0)', 'https://huggingface.co/FermionResearch/Phonon-2.5'],
  ['speaker-diarization-community-1', 'pyannote', 'CC BY 4.0 (creativecommons.org/licenses/by/4.0)', 'https://huggingface.co/pyannote/speaker-diarization-community-1'],
  ['multilingual-e5-small', 'Microsoft (intfloat)', 'MIT', 'https://huggingface.co/intfloat/multilingual-e5-small'],
];

function About() {
  return <Panel title="About, credits & limitations"><Notice>{accuracyNotice} Do not use output as the sole basis for legal, medical, employment or financial decisions. Provided "as is", without warranty (MIT license).</Notice><h3>Model credits</h3><ul>{credits.map(([name, author, license, source]) => <li key={name}><strong>{name}</strong>: {author}. License: {license}. Source: <code>{source}</code></li>)}</ul><p>Runtimes: faster-whisper (MIT), sherpa-onnx (Apache-2.0), fermion-research (Apache-2.0) with PyTorch (BSD-3-Clause), pyannote.audio (MIT), ONNX Runtime (MIT). The optional GPU Whisper pack includes NVIDIA cuBLAS, cuDNN and NVRTC under NVIDIA's license terms (not MIT), shipped inside the pack. The optional Azure Speech pack uses Microsoft's Speech SDK license.</p></Panel>;
}

function SettingsGroup({ groupId }: { groupId: string }) {
  const query = useResource('settings.get');
  const group = groups.find((candidate) => candidate.id === groupId);
  if (!group) return null;
  return <Panel title={group.title}><AsyncView query={query}>{(data) => { const saved = item(data); const settings = object(saved.nonsecret_settings ?? saved.settings); const values = object(settings[groupId]); return <>{group.notice && <Notice>{group.notice}</Notice>}<CommandForm key={`${groupId}:${revision(saved)}`} operation="settings.save" fixed={{ expected_revision: revision(saved) }} fields={group.fields.map((field) => ({ ...field, value: values[field.name] ?? field.value }))} mapPayload={(payload) => { const { expected_revision, ...changes } = payload; const merged = { ...values, ...changes }; if (groupId === 'recording') merged.retain_days = merged.retain_days === 'forever' ? null : Number(merged.retain_days); return { expected_revision: expected_revision ?? 1, settings: { [groupId]: merged } }; }} submit={`Save ${group.title.toLowerCase()}`} /></>; }}</AsyncView></Panel>;
}

function AudioSettings() {
  const devices = useNativeResource('audio_devices');
  return <Panel title="Devices & local audio test"><AsyncView query={devices}>{(data) => <><div className={styles.deviceGrid}>{items(data).map((row) => <section key={text(row.key)}><strong>{text(row.name)}</strong><Badge>{text(row.kind)}</Badge><code>{text(row.key)}</code>{row.available === false && <Notice danger>Device unavailable. Reconnect the selected endpoint or correct permissions; choose another device explicitly.</Notice>}</section>)}</div><AudioTest devices={items(data)} /><AudioCalibration devices={items(data)} /><Details title="Permission and device enumeration result" value={data} /></>}</AsyncView></Panel>;
}

function AudioTest({ devices }: { devices: RecordData[] }) {
  const [open, setOpen] = useState(false);
  const [deviceKey, setDeviceKey] = useState('');
  const [lease, setLease] = useState<RecordData | null>(null);
  const openRef = useRef(open); openRef.current = open;
  const start = useMutation({ mutationFn: () => nativeRequest('audio_test_start', { deviceKey }), onSuccess: async (result) => { if (openRef.current) setLease(result); else await nativeRequest('audio_test_stop'); } });
  const stop = useMutation({ mutationFn: () => nativeRequest('audio_test_stop'), onSuccess: () => setLease(null) });
  useEffect(() => () => { if (nativeAvailable()) void nativeRequest('audio_test_stop').catch(() => undefined); }, []);
  return <Modal open={open} onOpenChange={(next) => { setOpen(next); if (!next && nativeAvailable()) stop.mutate(); }} trigger={<button>Open local audio test…</button>} title="Local device test / calibration" description="No ASR, cloud processing or durable audio. This lease closes when the dialog closes or meeting capture starts. No setting changes implicitly."><FieldInput field={{ name: 'device', label: 'Exact device', type: 'select', required: true, options: [{ value: '', label: 'Choose device' }, ...devices.map((device) => ({ value: text(device.key), label: text(device.name) }))] }} value={deviceKey} onChange={(value) => setDeviceKey(text(value))} /><AudioMeters /><Toolbar><button disabled={!deviceKey || start.isPending || !!lease} onClick={() => start.mutate()}>Start local test</button><button disabled={!lease || stop.isPending} onClick={() => stop.mutate()}>Stop test</button></Toolbar>{lease && <Notice>Test active · lease {text(lease.lease_id)}. Speak normally and check each source level. High peaks indicate clipping; missing system signal can be an inactive endpoint.</Notice>}{(start.error || stop.error) && <ErrorNotice error={start.error ?? stop.error} />}</Modal>;
}

function AudioCalibration({ devices }: { devices: RecordData[] }) {
  const [mic, setMic] = useState('');
  const [system, setSystem] = useState('');
  const run = useMutation({ mutationFn: async () => { await nativeRequest('audio_test_stop'); return nativeRequest('audio_calibrate', { request: { mic_key: mic, system_key: system, seconds: 5 } }); } });
  const options = (kind: string) => [{ value: '', label: 'Choose exact endpoint' }, ...devices.filter((device) => device.kind === kind).map((device) => ({ value: text(device.key), label: text(device.name) }))];
  return <Modal trigger={<button>Measure echo calibration…</button>} title="Real microphone / system echo calibration" description="Stop any active meeting first. Play actual system audio through speakers that the microphone can hear during this five-second RAM-only measurement. No audio is retained or sent to ASR/cloud. Headphones remain recommended.">
    <FieldInput field={{ name: 'mic', label: 'Calibration microphone', type: 'select', options: options('mic') }} value={mic} onChange={(value) => setMic(text(value))} />
    <FieldInput field={{ name: 'system', label: 'Calibration system endpoint', type: 'select', options: options('system') }} value={system} onChange={(value) => setSystem(text(value))} />
    <button disabled={!nativeAvailable() || !mic || !system || run.isPending} onClick={() => run.mutate()}>{run.isPending ? 'Measuring real signals…' : 'Measure five seconds of real audio'}</button>
    {run.error && <ErrorNotice error={run.error} />}
    {run.data && <><Badge tone={run.data.passed === true ? 'success' : 'warning'}>{run.data.passed === true ? 'Measured calibration passed' : 'AEC unavailable — calibration did not pass'}</Badge><Details title="Measured delay, correlation, drift and reason" value={run.data} /></>}
  </Modal>;
}

function Profiles() {
  const people = useResource('people.list');
  const profiles = useResource('voice_profiles.list');
  return <Panel title="People & sensitive voice data"><Modal trigger={<button>Add person</button>} title="Create a person label"><CommandForm operation="people.create" fields={[{ name: 'display_name', label: 'Person display name', required: true }]} /></Modal><AsyncView query={people}>{(data) => items(data).map((row) => <article className={styles.listRow} key={id(row)}><strong>{text(row.display_name)}</strong><ConfirmCommand trigger="Delete person" title="Delete person identity?" message="Transcripts remain; profile references and voice data are handled independently." operation="people.delete" payload={{ id: id(row), expected_revision: revision(row) }} /></article>)}</AsyncView><AsyncView query={profiles}>{(data) => items(data).length === 0 ? <Empty title="No recurring voice profiles">Enrollment requires explicit biometric consent in a meeting's Speakers panel. Matching suggestions are not adopted as identity without approval.</Empty> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><strong>{text(row.person_id)}</strong><p>Scope {text(object(row.matching_scope).scope)} · {text(row.model_fingerprint)}</p><Details value={row} title="Matching policy / model fingerprint" /></div><ConfirmCommand trigger="Delete voice data" title="Delete this voice embedding?" message="Deletes biometric voice data independently of transcript text and invalidates all matching references." operation="voice_profiles.delete" payload={{ id: id(row), expected_revision: revision(row) }} /></article>)}</AsyncView></Panel>;
}

export function WindowSettings() {
  const query = useNativeResource('native_preferences_get');
  return <Panel title="Native window preferences"><AsyncView query={query}>{(data) => <NativePreferences saved={object(data.preferences ?? data)} />}</AsyncView></Panel>;
}

function NativePreferences({ saved }: { saved: RecordData }) {
  const [values, setValues] = useState<Payload>({ theme: saved.theme ?? 'system', tray_enabled: saved.tray_enabled ?? false, always_on_top: saved.always_on_top ?? true, sidebar_collapsed: saved.sidebar_collapsed ?? false });
  const client = useQueryClient();
  const save = useMutation({ mutationFn: () => updatePreferences(values), onSuccess: async () => { document.documentElement.dataset.theme = text(values.theme); await client.invalidateQueries({ queryKey: ['native'] }); } });
  return <form className={styles.settingsForm} onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>{[{ name: 'theme', label: 'Theme', type: 'select' as const, options: ['system', 'light', 'dark'].map((value) => ({ value, label: value })) }, { name: 'always_on_top', label: 'Always on top', type: 'checkbox' as const }, { name: 'tray_enabled', label: 'Tray icon', type: 'checkbox' as const }, { name: 'sidebar_collapsed', label: 'Compact sidebar', type: 'checkbox' as const }].map((field) => <FieldInput key={field.name} field={field} value={values[field.name] ?? false} onChange={(value) => setValues((current) => ({ ...current, [field.name]: value }))} />)}<button disabled={save.isPending || !nativeAvailable()} type="submit">Apply & save native preferences</button>{save.error && <ErrorNotice error={save.error} />}{save.isSuccess && <Notice>Native preferences saved.</Notice>}</form>;
}

export function UpdatesSettings() {
  const preferences = useNativeResource('native_preferences_get');
  const [status, setStatus] = useState<RecordData | null>(null);
  const [consent, setConsent] = useState(false);
  const [privacy, setPrivacy] = useState(false);
  const [channel, setChannel] = useState('stable');
  const settings = object(preferences.data?.preferences ?? preferences.data);
  useEffect(() => { const updates = object(settings.updates); setConsent(updates.consent === true); setPrivacy(updates.privacy_mode === true); setChannel(text(updates.channel, 'stable')); }, [preferences.data]);
  const update = useMutation({ mutationFn: async (operation: 'check' | 'install' | 'save') => {
    if (operation === 'save') return updatePreferences({ updates: { consent, channel, privacy_mode: privacy } });
    await flushDrafts();
    return nativeRequest(operation === 'check' ? 'update_check' : 'update_install');
  }, onSuccess: setStatus });
  return <Panel title="Signed updates & safe shutdown"><label className={styles.check}><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />Check GitHub releases for updates automatically and ask before installing</label><label className={styles.check}><input type="checkbox" checked={privacy} onChange={(event) => setPrivacy(event.target.checked)} />Privacy mode: no update network requests</label><Toolbar><button disabled={!nativeAvailable() || update.isPending} onClick={() => update.mutate('save')}>Save update preferences</button><button disabled={!nativeAvailable() || !consent || privacy || update.isPending} onClick={() => update.mutate('check')}>Check now</button><Modal trigger={<button disabled={!status || update.isPending}>Review install…</button>} title="Install a verified update?" description="Capture must stop and committed state must be verified before the native updater installs. You can cancel the shutdown reservation instead of installing."><Details title="Actual release status and verification" value={status} /><button disabled={!nativeAvailable() || update.isPending} onClick={() => update.mutate('install')}>Preserve state & install verified update</button><PrepareExit cancelOnly /></Modal><PrepareExit /></Toolbar>{update.error && <ErrorNotice error={update.error} />}{status && <Details title="Update status, verified artifact and progress" value={status} />}<Diagnostics /></Panel>;
}

function Diagnostics() {
  const query = useResource('workspace.status');
  return <AsyncView query={query}>{(data) => <Details value={data} title="Build, protocol, local workspace readiness and real capabilities" />}</AsyncView>;
}

export function PrepareExit({ cancelOnly = false }: { cancelOnly?: boolean }) {
  const [result, setResult] = useState<RecordData | null>(null);
  const exit = useMutation({ mutationFn: async () => { await flushDrafts(); return nativeRequest('prepare_exit', { cancel: cancelOnly }); }, onSuccess: setResult });
  return <span><button disabled={!nativeAvailable() || exit.isPending} onClick={() => exit.mutate()}>{cancelOnly ? 'Cancel shutdown reservation' : 'Prepare safe quit'}</button>{exit.error && <ErrorNotice error={exit.error} />}{result && <Details value={result} title="Preservation reservation / blockers" />}</span>;
}
