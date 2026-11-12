import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { useNavigate } from '@tanstack/react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useNativeResource, useResource } from '../lib/queries';
import { updatePreferences } from '../lib/preferences';
import { array, humanize, id, item, items, label, number, object, revision, text, type Json, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, FieldInput, Grid, Modal, Notice, Panel, TabSet, Toolbar, type Field } from '../components/ui';
import { FilterDisclosure } from '../components/controls';
import { LANGUAGES } from './library';
import styles from './workspace.module.css';

/** Settings tab labels in order; the tab set persists its selection under their joined key. */
export const settingsTabs = ['Transcription & AI', 'Audio', 'Meetings', 'Integrations', 'Privacy & data', 'App'];

/** Opens Settings on the Transcription & AI tab. */
export function SetupLink({ children = 'Set up transcription' }: { children?: ReactNode }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const open = useMutation({ mutationFn: async () => {
    if (nativeAvailable()) await updatePreferences({ selected_tabs: { [settingsTabs.join('|')]: 'Transcription & AI' } });
    await client.invalidateQueries({ queryKey: ['native'] });
    await navigate({ to: '/settings' });
  } });
  return <button type="button" disabled={open.isPending} onClick={() => open.mutate()}>{children}</button>;
}

const taskLabels: Record<string, string> = { asr: 'Transcription', diarization: 'Speakers', cues: 'Live suggestions', summary: 'Summary', actions: 'Actions', topics: 'Topics', artifacts: 'Artifacts', minutes: 'Minutes', briefings: 'Briefings', ask: 'Ask', digest: 'Digest', embeddings: 'Search by meaning', general: 'All AI tasks' };
const taskLabel = (task: string): string => taskLabels[task] ?? task;

const profileName = (profile: RecordData): string => text(profile.name) || text(profile.model_id) || label(profile);
const connectionName = (profile: RecordData, connections: RecordData[]): string => { const connection = connections.find((candidate) => id(candidate) === profile.connection_id); return connection ? label(connection) : ''; };
const profileLabel = (profile: RecordData, connections: RecordData[]): string => { const connection = connectionName(profile, connections); return connection ? `${profileName(profile)} (${connection})` : profileName(profile); };

function profileFields(connections: RecordData[]): Field[] {
  return [
    { name: 'name', label: 'Profile name', required: true },
    { name: 'role', label: 'Role', type: 'select', value: 'llm', options: ['llm', 'asr', 'diarization', 'embeddings'].map((value) => ({ value, label: value })) },
    { name: 'connection_id', label: 'Connection', type: 'select', required: true, options: [{ value: '', label: 'Choose connection' }, ...connections.map((connection) => ({ value: id(connection), label: `${label(connection)} (${text(connection.endpoint, 'local')})` }))] },
    { name: 'model_id', label: 'Model ID', required: true },
    { name: 'context_window', label: 'Context window (tokens)', type: 'number', min: 1, advanced: true },
    { name: 'options', label: 'Options (JSON)', type: 'json', value: {}, advanced: true },
  ];
}

export function SecureSecret({ label: promptLabel, onStored, action = 'Save credential…' }: { label: string; onStored: (reference: string) => void; action?: string }) {
  const command = useMutation({ mutationFn: () => nativeRequest('vault_prompt', { secretRef: `credential:${crypto.randomUUID()}`, label: promptLabel }), onSuccess: (result) => { if (result.stored === true) onStored(text(result.secret_ref)); } });
  return <div><button type="button" disabled={!nativeAvailable() || command.isPending} onClick={() => command.mutate()}>{command.isPending ? 'Waiting…' : action}</button>{Boolean(command.error) && <ErrorNotice error={command.error} />}</div>;
}

/** API key field: hidden text with a show toggle; stored by the engine on blur. `saved` = a key is already stored; leaving it empty keeps it. */
export function SecretInput({ label: fieldLabel, onStored, saved = false }: { label: string; onStored: (reference: string) => void; saved?: boolean }) {
  const [value, setValue] = useState('');
  const [shown, setShown] = useState(false);
  const inputId = useId();
  const store = useMutation({ mutationFn: (secret: string) => request('secrets.store', { value: secret }), onSuccess: (result) => onStored(text(result.secret_ref)) });
  return <div><label htmlFor={inputId}>{fieldLabel}</label><span className={styles.secret}><input id={inputId} type={shown ? 'text' : 'password'} autoComplete="off" spellCheck={false} placeholder={saved ? 'API key saved' : undefined} value={value} onChange={(event) => setValue(event.target.value)} onBlur={() => { if (value.trim()) store.mutate(value); }} /><button type="button" aria-label={`${shown ? 'Hide' : 'Show'} ${fieldLabel}`} onClick={() => setShown(!shown)}>{shown ? <EyeOff size={16} /> : <Eye size={16} />}</button></span>{Boolean(store.error) && <ErrorNotice error={store.error} />}</div>;
}

export function ProviderSettings() {
  const descriptors = useResource('providers.descriptors');
  const connections = useResource('entity.list', { collection: 'provider_connections' });
  const profiles = useResource('entity.list', { collection: 'model_profiles' });
  const policies = useResource('entity.list', { collection: 'task_policies' });
  return <TabSet tabs={[
    { label: 'Connections', content: <><Panel title="Connections"><AsyncView query={connections}>{(data) => items(data).length === 0 ? <Empty title="No connections" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{label(row)}</strong><p>{text(row.endpoint ?? row.base_url, 'Local runtime')}</p><Details value={row} title="Details" /></div><Toolbar><ConnectionTest connection={row} profiles={items(profiles.data)} /><ConnectionEdit row={row} /><ConfirmCommand trigger="Remove connection" title="Remove connection?" message="Tasks using this connection must finish first. Saved credentials stay in the vault." operation="entity.delete" payload={{ collection: 'provider_connections', id: id(row), expected_revision: revision(row) }} /></Toolbar></article>)}</AsyncView></Panel><NewConnection descriptors={items(descriptors.data)} /></> },
    { label: 'Model profiles', content: <Panel title="Model profiles" actions={<ModelProfileCreation connections={items(connections.data)} />}><AsyncView query={profiles}>{(data) => items(data).length === 0 ? <Empty title="No model profiles" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{profileName(row)}</strong><p>{[text(row.name) ? text(row.model_id) : '', connectionName(row, items(connections.data))].filter(Boolean).join(' · ')}</p><Details value={row} title="Details" /></div><Toolbar><EditResource operation="entity.update" row={{ ...row, name: profileName(row) }} fixed={{ collection: 'model_profiles' }} fields={profileFields(items(connections.data))} label="Edit model profile" /><ConfirmCommand trigger="Delete profile" title="Delete profile?" message="Tasks using this profile must finish first." operation="entity.delete" payload={{ collection: 'model_profiles', id: id(row), expected_revision: revision(row) }} /></Toolbar></article>)}</AsyncView></Panel> },
    { label: 'Task routing', content: <Panel title="Task routing" actions={<TaskRouting profiles={items(profiles.data)} connections={items(connections.data)} />}><AsyncView query={policies}>{(data) => items(data).length === 0 ? <Empty title="No task routing" /> : items(data).map((row) => { const assigned = items(profiles.data).find((profile) => id(profile) === text(row.model_profile_id)); return <article key={id(row)} className={styles.actionRow}><div><strong>{taskLabel(text(row.task))}</strong><p>{assigned ? profileLabel(assigned, items(connections.data)) : 'Profile not found'}</p><Details value={row} title="Details" /></div><TaskRouting row={row} profiles={items(profiles.data)} connections={items(connections.data)} /></article>; })}</AsyncView></Panel> },
  ]} />;
}

function taskPolicyFields(profiles: RecordData[], connections: RecordData[], row?: RecordData): Field[] {
  const current = (name: string, fallback: Json): Json => row?.[name] ?? fallback;
  const saved = text(row?.language);
  const languages = [{ value: '', label: 'Same as the meeting' }, ...LANGUAGES.map(([value, name]) => ({ value: value as string, label: name as string }))];
  // 'auto' is the old spelling of "same as the meeting".
  const language = saved === 'auto' ? '' : saved;
  return [
    { name: 'task', label: 'Task', type: 'select', value: current('task', 'summary'), options: Object.keys(taskLabels).map((value) => ({ value, label: taskLabel(value) })) },
    { name: 'model_profile_id', label: 'Model profile', type: 'select', required: true, value: current('model_profile_id', ''), options: [{ value: '', label: 'Select a model profile' }, ...profiles.map((profile) => ({ value: id(profile), label: profileLabel(profile, connections) }))] },
    { name: 'language', label: 'Output language', type: 'select', value: language, options: language && !languages.some((option) => option.value === language) ? [...languages, { value: language, label: language }] : languages, advanced: true },
    { name: 'deadline_ms', label: 'Deadline (seconds)', type: 'number', value: number(row?.deadline_ms, 60000) / 1000, min: 1, max: 1800, advanced: true },
    { name: 'token_limit', label: 'Maximum output tokens', type: 'number', value: current('token_limit', 4096), min: 1, max: 32768, advanced: true },
    { name: 'budget_limit', label: 'Max cost per task (0 = no cap)', type: 'number', value: current('budget_limit', 0), min: 0, advanced: true },
  ];
}

/** Add or edit a task route. Fallbacks are ticked in order and sent as the same id array; the deadline is shown in seconds and sent in milliseconds. */
function TaskRouting({ row, profiles, connections }: { row?: RecordData; profiles: RecordData[]; connections: RecordData[] }) {
  const [fallbacks, setFallbacks] = useState<string[]>(() => (Array.isArray(row?.fallback_ids) ? row.fallback_ids : []).map((value) => text(value)));
  const others = profiles.filter((profile) => id(profile) !== text(row?.model_profile_id));
  const title = row ? 'Edit task routing' : 'Add task routing';
  return <Modal trigger={<button>{title}</button>} title={title}>
    <CommandForm operation={row ? 'entity.update' : 'entity.create'} fixed={{ collection: 'task_policies', ...(row ? { id: row.id ?? '', expected_revision: row.revision ?? 1 } : {}) }} fields={taskPolicyFields(profiles, connections, row)} mapPayload={(payload) => ({ ...payload, deadline_ms: typeof payload.deadline_ms === 'number' ? Math.round(payload.deadline_ms * 1000) : payload.deadline_ms ?? null, fallback_ids: fallbacks.filter((fallback) => fallback !== payload.model_profile_id) })}>
      {others.length > 0 && <fieldset><legend>Fallback profiles (tried in the order ticked)</legend>{others.map((profile) => <label key={id(profile)} className={styles.check}><input type="checkbox" checked={fallbacks.includes(id(profile))} onChange={(event) => setFallbacks(event.target.checked ? [...fallbacks, id(profile)] : fallbacks.filter((fallback) => fallback !== id(profile)))} />{profileLabel(profile, connections)}</label>)}</fieldset>}
    </CommandForm>
  </Modal>;
}

const FIXED_HOSTS: Record<string, string> = { openai: 'api.openai.com', openrouter: 'openrouter.ai' };

/** Host that would receive meeting data, or '' when the endpoint is this device and needs no consent. */
export function consentHost(adapter: string, endpoint: string): string {
  if (adapter === 'ollama') return '';
  const fixed = FIXED_HOSTS[adapter];
  if (fixed) return fixed;
  const value = endpoint.trim();
  let host = '';
  try { host = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `http://${value}`).hostname; } catch { /* still typing */ }
  return host === 'localhost' || host === '[::1]' || /^127(\.\d{1,3}){3}$/.test(host) ? '' : host || 'this provider';
}

/** One form that creates the connection, profile, consent and task policies for summaries. */
export function LlmSetup() {
  const descriptors = items(useResource('providers.descriptors').data);
  const status = useResource('setup.status');
  const client = useQueryClient();
  const ollama = object(status.data?.ollama);
  const adapters = descriptors.filter((descriptor) => Array.isArray(descriptor.roles) && descriptor.roles.includes('llm')).map(id).sort((a, b) => Number(b === 'ollama') - Number(a === 'ollama'));
  const connections = items(useResource('entity.list', { collection: 'provider_connections' }).data);
  const profiles = items(useResource('entity.list', { collection: 'model_profiles' }).data);
  const active = object(status.data?.llm);
  const activeProfile = profiles.find((profile) => id(profile) === text(active.profile_id));
  const activeConnection = connections.find((connection) => id(connection) === activeProfile?.connection_id);
  const [choice, setChoice] = useState('');
  const [model, setModel] = useState<string | null>(null);
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [secretRef, setSecretRef] = useState('');
  const [contextWindow, setContextWindow] = useState<string | null>(null);
  const [agreed, setAgreed] = useState(false);
  const adapter = choice || text(active.adapter) || (ollama.available === true ? 'ollama' : adapters.find((candidate) => candidate !== 'ollama') ?? 'ollama');
  const onActive = adapter === text(active.adapter);
  const modelValue = model ?? (onActive ? text(active.model_id) : '');
  const endpointValue = endpoint ?? (onActive ? text(activeConnection?.endpoint) : '');
  const isOllama = adapter === 'ollama';
  const needsEndpoint = adapter === 'openai-compatible' || adapter === 'azure-openai';
  // The saved key is a vault reference; reuse it only for the same provider and endpoint so it never goes to another host.
  const savedRef = onActive && !isOllama && (!needsEndpoint || endpointValue === text(activeConnection?.endpoint)) ? text(activeConnection?.secret_ref) : '';
  const keyRef = secretRef || savedRef;
  const canList = isOllama || Boolean(keyRef && (adapter === 'openrouter' || adapter === 'openai' || (adapter === 'openai-compatible' && endpointValue)));
  const listed = useResource('setup.models', { adapter, ...(needsEndpoint && endpointValue ? { endpoint: endpointValue } : {}), ...(!isOllama && keyRef ? { secret_ref: keyRef } : {}) }, canList);
  const available = items(listed.data);
  const picked = available.find((row) => id(row) === modelValue)?.context_length;
  const savedWindow = onActive && model === null && typeof activeProfile?.context_window === 'number' ? String(activeProfile.context_window) : undefined;
  const windowValue = contextWindow ?? savedWindow ?? (typeof picked === 'number' ? String(picked) : isOllama ? '' : '128000');
  const destination = consentHost(adapter, endpointValue);
  const save = useMutation({
    mutationFn: () => request('setup.llm', { adapter, model: modelValue, ...(needsEndpoint ? { endpoint: endpointValue } : {}), ...(!isOllama && keyRef ? { secret_ref: keyRef } : {}), ...(windowValue ? { context_window: Number(windowValue) } : {}), ...(destination ? { accepted_destination: agreed } : {}) }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['engine'] }); },
  });
  const choose = (value: string): void => { setChoice(value); setModel(''); setEndpoint(''); setSecretRef(''); setContextWindow(null); setAgreed(false); save.reset(); };
  return <Panel title="AI for summaries" actions={status.data?.llm_ready === true ? <Badge tone="success">Ready</Badge> : undefined}>
    {adapters.length > 0 && <form className={styles.settingsForm} onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <FieldInput field={{ name: 'provider', label: 'Provider', type: 'select', options: adapters.map((value) => ({ value, label: value === 'ollama' && ollama.available === true ? 'ollama (running on this computer)' : value === 'openrouter' ? 'OpenRouter' : value === 'openai-compatible' ? 'OpenAI-compatible endpoint' : value })) }} value={adapter} onChange={(value) => choose(text(value))} />
      {needsEndpoint && <FieldInput field={{ name: 'endpoint', label: 'Endpoint', required: true }} value={endpointValue} onChange={(value) => setEndpoint(text(value))} />}
      {!isOllama && <SecretInput label="API key" saved={Boolean(savedRef) && !secretRef} onStored={setSecretRef} />}
      <FieldInput field={{ name: 'model', label: 'Model', required: true, ...(canList && available.length > 0 ? { suggestions: available.map(id) } : {}) }} value={modelValue} onChange={(value) => setModel(text(value))} />
      {canList && listed.isLoading && <p>Loading models…</p>}
      {canList && Boolean(listed.error) && <ErrorNotice error={listed.error} />}
      <FilterDisclosure title="Advanced"><FieldInput field={{ name: 'context_window', label: 'Context window (tokens)', type: 'number', min: 1 }} value={windowValue} onChange={(value) => setContextWindow(text(value))} /></FilterDisclosure>
      {Boolean(save.error) && <ErrorNotice error={save.error} />}
      {save.isSuccess && <Notice>AI for summaries is set up.</Notice>}
      {destination && <label className={styles.check}><input type="checkbox" checked={agreed} onChange={(event) => setAgreed(event.target.checked)} />Send meeting data to {destination}</label>}
      <button type="submit" disabled={!nativeAvailable() || !modelValue || (Boolean(destination) && !agreed) || save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</button>
    </form>}
  </Panel>;
}

function NewConnection({ descriptors }: { descriptors: RecordData[] }) {
  const [open, setOpen] = useState(false);
  const [secretRef, setSecretRef] = useState('');
  const [adapter, setAdapter] = useState('');
  const schemes = descriptors.find((candidate) => id(candidate) === adapter)?.auth_schemes;
  // Local runtimes need no credential and have a fixed local:// endpoint; Ollama is the exception that still takes a URL.
  const localRuntime = adapter !== 'ollama' && Array.isArray(schemes) && schemes.every((scheme) => scheme === 'none');
  const defaultAuth = Array.isArray(schemes) ? text(schemes[0], 'none') : 'none';
  const endpointField: Field[] = localRuntime ? [] : [{ name: 'endpoint', label: 'Endpoint URL', required: adapter !== 'ollama', value: adapter === 'ollama' ? 'http://127.0.0.1:11434' : undefined }];
  return <Modal open={open} onOpenChange={setOpen} trigger={<button>Add connection</button>} title="Add connection">
    <FieldInput field={{ name: 'adapter_id', label: 'Provider', type: 'select', required: true, options: [{ value: '', label: 'Choose provider' }, ...descriptors.map((candidate) => ({ value: id(candidate), label: id(candidate) === 'openai-compatible' ? 'OpenAI-compatible endpoint' : label(candidate) }))] }} value={adapter} onChange={(value) => { setAdapter(text(value)); setSecretRef(''); }} />
    {adapter && !localRuntime && <SecretInput key={adapter} label="API key" onStored={setSecretRef} />}
    {adapter && <CommandForm key={adapter} operation="entity.create" fixed={{ collection: 'provider_connections', adapter_id: adapter, secret_ref: secretRef || null, ...(localRuntime ? { endpoint: `local://${adapter}`, declared_execution: 'local' } : {}) }} fields={[{ name: 'display_name', label: 'Name', required: true }, ...endpointField, { name: 'auth_kind', label: 'Authentication', type: 'select', value: defaultAuth, options: ['none', 'bearer', 'api_key', 'entra'].map((value) => ({ value, label: value })), advanced: true }, { name: 'region', label: 'Region', advanced: true }, { name: 'nonsecret_settings', label: 'Extra settings (JSON)', type: 'json', value: {}, advanced: true }]} onSuccess={() => setOpen(false)} />}
  </Modal>;
}

function ConnectionTest({ connection, profiles }: { connection: RecordData; profiles: RecordData[] }) {
  const [result, setResult] = useState<RecordData | null>(null);
  return <Modal trigger={<button>Test…</button>} title="Test connection"><p>Destination: {text(connection.endpoint, 'Selected local runtime')}</p><CommandForm operation="providers.test" fixed={{ connection_id: id(connection) }} fields={[{ name: 'profile_id', label: 'Model profile', type: 'select', required: true, options: [{ value: '', label: 'Choose model profile' }, ...profiles.filter((profile) => profile.connection_id === id(connection)).map((profile) => ({ value: id(profile), label: label(profile) }))] }]} submit="Test" confirm="Send a test request to this destination." onSuccess={(data) => setResult(item(data))} />{result && <Details value={result} title="Details" />}</Modal>;
}

/** Where a connection sends data: the destination fingerprint the engine stores in consents (connection_value in policies.py). */
export function destinationOf(connection: RecordData): string {
  return text(object(connection.nonsecret_settings).destination_fingerprint ?? connection.destination_fingerprint ?? connection.endpoint);
}

/** Edit name, endpoint and options, and replace the API key (a new vault reference, as in Add connection). */
function ConnectionEdit({ row }: { row: RecordData }) {
  const [secretRef, setSecretRef] = useState('');
  const keyless = text(row.auth_kind, 'none') === 'none';
  const fields: Field[] = [{ name: 'display_name', label: 'Name', required: true }, { name: 'endpoint', label: 'Endpoint (no credentials in URL)' }, { name: 'region', label: 'Region', advanced: true }, { name: 'deployment', label: 'Azure deployment', advanced: true }];
  return <Modal trigger={<button>Edit</button>} title="Edit">
    {!text(row.endpoint).startsWith('local://') && <SecretInput label="Replace API key" saved={Boolean(row.secret_ref)} onStored={setSecretRef} />}
    <CommandForm operation="entity.update" fixed={{ collection: 'provider_connections', id: row.id ?? '', expected_revision: row.revision ?? 1, ...(secretRef ? { secret_ref: secretRef, ...(keyless ? { auth_kind: 'bearer' } : {}) } : {}) }} fields={fields.map((field) => ({ ...field, value: row[field.name] }))} />
  </Modal>;
}

const downloading = (entry: RecordData): boolean => ['downloading', 'extracting'].includes(text(object(entry.download).state));
const mib = (bytes: number): string => (bytes / 1024 / 1024).toFixed(0);

/** catalog.list, refreshed every second while a download runs and everything else once it ends. */
function useCatalog() {
  const client = useQueryClient();
  const query = useResource('catalog.list', {}, true, (data) => items(data).some(downloading));
  const busy = items(query.data).some(downloading);
  const wasBusy = useRef(false);
  useEffect(() => {
    if (wasBusy.current && !busy) void client.invalidateQueries({ queryKey: ['engine'] });
    wasBusy.current = busy;
  }, [busy]);
  return query;
}

export function ModelSettings() {
  const catalog = useCatalog();
  const status = useResource('setup.status');
  const used = aiRows.map(([role]) => text(object(status.data?.[role]).model_id)).filter(Boolean);
  return <Panel title="Models & runtime" actions={<ModelArchiveImport templates={items(catalog.data)} />}><AsyncView query={catalog}>{(data) => {
    // Runtimes are fetched with their model.
    const visible = items(data).filter((row) => row.role !== 'runtime').sort((a, b) => Number(b.installed === true) - Number(a.installed === true));
    return visible.length === 0 ? <Empty title="No models yet" /> : <Grid>{visible.map((row) => <ModelCard key={id(row)} entry={row} inUse={used.includes(id(row))} />)}</Grid>;
  }}</AsyncView></Panel>;
}

const modelBytes = (entry: RecordData): number => typeof entry.bytes === 'number' ? entry.bytes : array(entry.files).reduce((total, file) => total + (typeof file.bytes === 'number' ? file.bytes : 0), 0);

function ModelCard({ entry, inUse }: { entry: RecordData; inUse: boolean }) {
  const license = object(entry.license);
  return <Panel title={label(entry)} actions={<><Badge>{aiRows.find(([role]) => role === entry.role)?.[1] ?? humanize(entry.role)}</Badge>{entry.gpu === true && <Badge>GPU</Badge>}{entry.installed === true && <Badge>Installed</Badge>}</>}><dl><dt>{entry.installed === true ? 'Size' : 'Download'}</dt><dd>{(modelBytes(entry) / 1024 / 1024).toFixed(1)} MiB</dd><dt>License</dt><dd>{text(license.spdx ?? entry.license_spdx, 'Review license details')}</dd></dl>{entry.installed === true ? (inUse ? <Badge tone="success">In use</Badge> : <ConfirmCommand trigger="Delete" title={`Delete ${label(entry)}?`} message="Removes this model from this computer." operation="catalog.delete" payload={{ id: id(entry), confirmed: true, revision: text(entry.revision) }} />) : <ModelDownload entry={entry} />}</Panel>;
}

/** License review and download; the engine also switches the model's task to it. */
function ModelDownload({ entry }: { entry: RecordData }) {
  const download = object(entry.download);
  const done = number(download.done);
  const total = number(download.total);
  const active = downloading(entry);
  const [stalled, setStalled] = useState(false);
  // "Installing…" only while progress advances: a stuck download or extraction becomes an error with Retry.
  useEffect(() => {
    setStalled(false);
    if (!active) return undefined;
    const timer = window.setTimeout(() => setStalled(true), download.state === 'extracting' ? 300_000 : 60_000);
    return () => window.clearTimeout(timer);
  }, [active, download.state, done]);
  if (active && !stalled) return <span><progress value={done} max={total} /> <small>{download.state === 'extracting' ? 'Installing…' : `${Math.round(done / total * 100)}%, ${mib(done)} of ${mib(total)} MiB`}</small></span>;
  const failed = stalled || download.state === 'failed';
  return <>{failed && <Notice danger>{stalled ? 'The download stopped making progress.' : text(download.error, 'Download failed')}</Notice>}<ModelDownloadDialog entry={entry} retry={failed} /></>;
}

function ModelDownloadDialog({ entry, retry }: { entry: RecordData; retry: boolean }) {
  const [open, setOpen] = useState(false);
  const license = object(entry.license);
  return <Modal open={open} onOpenChange={setOpen} trigger={<button>{retry ? 'Retry' : 'Download…'}</button>} title={`Download ${label(entry)}`}><Details value={entry} title="Details" /><p className={styles.preWrap}>{text(license.text ?? entry.license_text)}</p><CommandForm operation="catalog.download" fixed={{ id: id(entry), accepted_license: true, accepted_destination: true }} fields={[]} submit="Download" confirm="I accept the license above." onSuccess={() => setOpen(false)} /></Modal>;
}

function ModelRow({ entry, current }: { entry: RecordData; current: string }) {
  const installed = entry.installed === true;
  return <article className={styles.aiRow}><div><strong>{label(entry)}</strong><span>{entry.gpu === true && <Badge>GPU</Badge>} {installed ? 'Installed' : `${(modelBytes(entry) / 1024 / 1024).toFixed(0)} MiB`}</span></div>{id(entry) === current ? <Badge tone="success">In use</Badge> : installed ? <CommandButton operation="catalog.download" payload={{ id: id(entry), accepted_license: true, accepted_destination: true }}>Use</CommandButton> : <ModelDownload entry={entry} />}</article>;
}

function ModelList({ role, current }: { role: string; current: string }) {
  const catalog = useCatalog();
  return <AsyncView query={catalog} isEmpty={!items(catalog.data).some((row) => row.role === role)} empty={<Empty title="No models for this component" />}>{(data) => items(data).filter((row) => row.role === role).sort((a, b) => Number(b.installed === true) - Number(a.installed === true)).map((row) => <ModelRow key={id(row)} entry={row} current={current} />)}</AsyncView>;
}

const aiRows = [['asr', 'Transcription', 'transcription model'], ['diarization', 'Speaker labels', 'speaker labels model'], ['llm', 'Summaries, actions & minutes', 'summaries model']] as const;

/** Speaker model worker state from setup.status (`stopped | loading | ready`). */
export const speakerStateLabel = (state: string): string => state === 'ready' ? 'Ready' : state === 'loading' ? 'Loading…' : 'Not running';

/** What transcription, speaker labels and summaries currently use, each with a Change button. The one place the speaker model state shows. */
export function AiSettings() {
  const status = useResource('setup.status', {}, true, (data) => text(object(data?.diarization).state) === 'loading');
  return <Panel><AsyncView query={status}>{(data) => aiRows.map(([role, title, noun]) => {
    const choice = data[role] ? object(data[role]) : null;
    const speakerState = role === 'diarization' ? text(choice?.state) : '';
    return <article key={role} className={styles.aiRow}><div><span className={styles.aiTitle}>{title}</span><strong>{choice ? <>{text(choice.label)}{(choice.gpu === true || text(choice.adapter) !== text(choice.label)) && <> <Badge>{choice.gpu === true ? 'GPU' : text(choice.adapter)}</Badge></>}{speakerState && <> <Badge tone={speakerState === 'ready' ? 'success' : 'neutral'}>{speakerStateLabel(speakerState)}</Badge></>}</> : 'Not set up'}</strong></div><Modal trigger={<button aria-label={`${choice ? 'Change' : 'Set up'} ${noun}`}>{choice ? 'Change' : 'Set up'}</button>} title={title}>{role === 'llm' ? <LlmSetup /> : <ModelList role={role} current={text(choice?.model_id, '')} />}</Modal></article>;
  })}</AsyncView></Panel>;
}

const GET_STARTED = 'get-started';

/** Open state for the Get started dialog; opens itself once on first launch while transcription is unset. */
export function useGetStarted() {
  const client = useQueryClient();
  const setup = useResource('setup.status');
  const preferences = useNativeResource('native_preferences_get');
  const [open, setOpen] = useState(false);
  const shown = text(object(object(preferences.data?.preferences ?? preferences.data).selected_tabs)[GET_STARTED]);
  useEffect(() => {
    if (setup.data?.asr_ready !== false || !preferences.isSuccess || shown || !nativeAvailable()) return;
    setOpen(true);
    void updatePreferences({ selected_tabs: { [GET_STARTED]: 'shown' } }).then(() => client.invalidateQueries({ queryKey: ['native'] }));
  }, [setup.data?.asr_ready, preferences.isSuccess]);
  return [open, setOpen] as const;
}

function SummarySetup() {
  const status = useResource('setup.status');
  const client = useQueryClient();
  const ollama = object(status.data?.ollama);
  const models = (Array.isArray(ollama.models) ? ollama.models : []).map((name) => text(name)).filter(Boolean);
  const [model, setModel] = useState('');
  const picked = model || models[0] || '';
  const use = useMutation({ mutationFn: () => request('setup.llm', { adapter: 'ollama', model: picked }), onSuccess: async () => { await client.invalidateQueries({ queryKey: ['engine'] }); } });
  if (ollama.available !== true || models.length === 0) return <LlmSetup />;
  return <div className={styles.settingsForm}>
    {models.length > 1 && <FieldInput field={{ name: 'model', label: 'Model', type: 'select', options: models.map((name) => ({ value: name, label: name })) }} value={picked} onChange={(value) => setModel(text(value))} />}
    <button type="button" disabled={!nativeAvailable() || use.isPending} onClick={() => use.mutate()}>{use.isPending ? 'Saving…' : `Use ${picked}`}</button>
    {Boolean(use.error) && <ErrorNotice error={use.error} />}
    {use.isSuccess && <Notice>Summaries are set up.</Notice>}
  </div>;
}

/** First-run setup: pick transcription, then summaries. */
export function GetStarted({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const status = useResource('setup.status');
  const catalog = useCatalog();
  const asr = items(catalog.data).filter((row) => row.role === 'asr' && !row.blocked_reason);
  const recommended = asr.find((row) => id(row) === 'whisper-large-v3-turbo' && row.gpu === true) ?? asr.find((row) => id(row) === 'parakeet-tdt-v3-int8') ?? asr[0];
  const current = text(object(status.data?.asr).model_id, '');
  return <Modal open={open} onOpenChange={onOpenChange} title="Get started">
    <h3>Transcription</h3>
    <AsyncView query={catalog}>{() => <>
      {recommended && <ModelRow entry={recommended} current={current} />}
      {asr.length > 1 && <FilterDisclosure title="Other models">{asr.filter((row) => row !== recommended).map((row) => <ModelRow key={id(row)} entry={row} current={current} />)}</FilterDisclosure>}
    </>}</AsyncView>
    <h3>Summaries</h3>
    <SummarySetup />
    <Toolbar>{status.data?.asr_ready !== true && <button type="button" onClick={() => onOpenChange(false)}>Skip for now</button>}<button type="button" onClick={() => onOpenChange(false)}>Done</button></Toolbar>
  </Modal>;
}

function ModelArchiveImport({ templates }: { templates: RecordData[] }) {
  const [templateId, setTemplateId] = useState('');
  const [unreviewed, setUnreviewed] = useState(false);
  const [licensed, setLicensed] = useState(false);
  const client = useQueryClient();
  const operation = useMutation({ mutationFn: async () => {
    const grant = await nativeRequest('file_grant', { purpose: 'import_model' });
    if (grant.cancelled) return { cancelled: true };
    return request('catalog.import_model', { template_id: templateId, grant_id: grant.grant_id ?? '', accepted_unreviewed: unreviewed, accepted_license: licensed });
  }, onSuccess: async () => { await client.invalidateQueries({ queryKey: ['engine'] }); } });
  const selected = templates.find((entry) => id(entry) === templateId);
  return <Modal trigger={<button>Import model archive…</button>} title="Import model archive">
    <FieldInput field={{ name: 'template_id', label: 'Model layout', type: 'select', options: [{ value: '', label: 'Choose layout' }, ...templates.filter((entry) => entry.kind !== 'runtime' && entry.executable !== true).map((entry) => ({ value: id(entry), label: label(entry) }))] }} value={templateId} onChange={(value) => setTemplateId(text(value))} />
    {selected && <Details title="Details" value={selected} />}
    <label className={styles.check}><input type="checkbox" checked={unreviewed} onChange={(event) => setUnreviewed(event.target.checked)} />I understand this archive is unreviewed.</label>
    <label className={styles.check}><input type="checkbox" checked={licensed} onChange={(event) => setLicensed(event.target.checked)} />I have the right to use this model under its license.</label>
    <button disabled={!nativeAvailable() || !templateId || !unreviewed || !licensed || operation.isPending} onClick={() => operation.mutate()}>{operation.isPending ? 'Importing…' : 'Choose archive and import'}</button>
    {Boolean(operation.error) && <ErrorNotice error={operation.error} />}
    {Boolean(operation.isSuccess) && <><Notice>Imported. Use its ID as the model ID in a profile.</Notice><Details title="Details" value={operation.data} /></>}
  </Modal>;
}

function ModelProfileCreation({ connections }: { connections: RecordData[] }) {
  const [selectedId, setSelectedId] = useState('');
  const [runtimeId, setRuntimeId] = useState('');
  const catalog = useResource('catalog.list');
  const selected = items(catalog.data).find((entry) => id(entry) === selectedId);
  const runtimes = items(catalog.data).filter((entry) => entry.role === 'runtime' && !entry.blocked_reason && object(object(entry.host_check).gpu).met !== false && text(object(entry.runtime).family) === text(object(selected?.runtime).family));
  const runtime = runtimeId || text(selected?.runtime_id);
  const gpu = object(object(runtimes.find((entry) => id(entry) === runtime)?.host_check).gpu).met === true;
  const fields = profileFields(connections).filter((field) => field.name !== 'model_id' || !selectedId).map((field): Field => field.name === 'role' && selected?.role ? { ...field, value: selected.role } : field.name === 'options' && selected ? { ...field, value: { catalog_id: selectedId, revision: text(selected.revision), ...(runtime ? { runtime_id: runtime } : {}), ...(gpu ? { device: 'cuda' } : {}) } } : field);
  return <Modal trigger={<button>Add model profile</button>} title="Add model profile">
    <FieldInput field={{ name: 'catalog_model', label: 'Model', type: 'select', options: [{ value: '', label: 'Enter a model ID manually' }, ...items(catalog.data).filter((entry) => entry.role !== 'runtime').map((entry) => ({ value: id(entry), label: label(entry) }))] }} value={selectedId} onChange={(value) => { setSelectedId(text(value)); setRuntimeId(''); }} />
    {selected && <Details title="Details" value={selected} />}
    {selected && <FilterDisclosure title="Advanced"><FieldInput field={{ name: 'runtime_descriptor', label: 'Runtime', type: 'select', options: [{ value: '', label: 'Choose runtime' }, ...runtimes.map((entry) => ({ value: id(entry), label: entry.host_check ? `${label(entry)} (GPU)` : label(entry) }))] }} value={runtime} onChange={(value) => setRuntimeId(text(value))} /></FilterDisclosure>}
    <CommandForm key={`${selectedId || 'manual'}:${runtime}`} operation="entity.create" fixed={{ collection: 'model_profiles', ...(selectedId ? { model_id: selectedId } : {}) }} fields={fields} />
  </Modal>;
}
