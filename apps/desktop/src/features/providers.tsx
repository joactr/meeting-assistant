import { useState, type ReactNode } from 'react';
import { useNavigate } from '@tanstack/react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { updatePreferences } from '../lib/preferences';
import { array, id, item, items, label, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, FieldInput, Grid, Modal, Notice, Panel, TabSet, Toolbar, type Field } from '../components/ui';
import { FilterDisclosure } from '../components/controls';
import styles from './workspace.module.css';

/** Settings tab labels in order; the tab set persists its selection under their joined key. */
export const settingsTabs = ['Audio', 'Meetings', 'AI providers & models', 'Calendar & integrations', 'Privacy & backup', 'App & updates', 'Advanced', 'About'];

/** Opens Settings on the AI providers & models tab. */
export function SetupLink({ children = 'Set up transcription' }: { children?: ReactNode }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const open = useMutation({ mutationFn: async () => {
    if (nativeAvailable()) await updatePreferences({ selected_tabs: { [settingsTabs.join('|')]: 'AI providers & models' } });
    await client.invalidateQueries({ queryKey: ['native'] });
    await navigate({ to: '/settings' });
  } });
  return <button type="button" disabled={open.isPending} onClick={() => open.mutate()}>{children}</button>;
}

const taskLabels: Record<string, string> = { asr: 'Transcription', diarization: 'Speakers', cues: 'Live suggestions', summary: 'Summary', actions: 'Actions', topics: 'Topics', artifacts: 'Artifacts', minutes: 'Minutes', briefings: 'Briefings', ask: 'Ask', digest: 'Digest', embeddings: 'Search by meaning', general: 'All AI tasks' };
const taskLabel = (task: string): string => taskLabels[task] ?? task;

function profileFields(connections: RecordData[]): Field[] {
  return [
    { name: 'name', label: 'Profile name', required: true },
    { name: 'role', label: 'Role', type: 'select', value: 'llm', options: ['llm', 'asr', 'diarization', 'embeddings'].map((value) => ({ value, label: value })) },
    { name: 'connection_id', label: 'Connection', type: 'select', required: true, options: [{ value: '', label: 'Select actual configured destination' }, ...connections.map((connection) => ({ value: id(connection), label: `${label(connection)} · ${text(connection.endpoint)}` }))] },
    { name: 'model_id', label: 'Actual model / deployment ID', required: true },
    { name: 'context_window', label: 'Context window (tokens)', type: 'number', min: 1, advanced: true },
    { name: 'options', label: 'Explicit runtime/model options (JSON)', type: 'json', value: {}, advanced: true },
  ];
}

export function SecureSecret({ label: promptLabel, onStored }: { label: string; onStored: (reference: string) => void }) {
  const command = useMutation({ mutationFn: () => nativeRequest('vault_prompt', { secretRef: `credential:${crypto.randomUUID()}`, label: promptLabel }), onSuccess: (result) => { if (result.stored === true) onStored(text(result.secret_ref)); } });
  return <div><button type="button" disabled={!nativeAvailable() || command.isPending} onClick={() => command.mutate()}>{command.isPending ? 'Waiting for secure dialog…' : 'Store credential in native vault…'}</button>{Boolean(command.error) && <ErrorNotice error={command.error} />}</div>;
}

export function ProviderSettings() {
  const descriptors = useResource('providers.descriptors');
  const connections = useResource('entity.list', { collection: 'provider_connections' });
  const profiles = useResource('entity.list', { collection: 'model_profiles' });
  const policies = useResource('entity.list', { collection: 'task_policies' });
  return <TabSet tabs={[
    { label: 'Connections', content: <><LlmSetup descriptors={items(descriptors.data)} /><Panel title="Provider catalog"><AsyncView query={descriptors}>{(data) => <Grid>{items(data).map((row) => <section key={id(row)}><h3>{label(row)}</h3><Toolbar>{Array.isArray(row.roles) && row.roles.map((role) => <Badge key={text(role)}>{text(role)}</Badge>)}</Toolbar><Details value={row} title="Details" /></section>)}</Grid>}</AsyncView></Panel><Panel title="Your connections"><AsyncView query={connections}>{(data) => items(data).length === 0 ? <Empty title="No connections yet" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{label(row)}</strong><p>{text(row.adapter_id ?? row.adapter)} · {text(row.endpoint ?? row.base_url, 'Local runtime')}</p><Toolbar><Badge>{text(row.declared_execution, 'Unverified execution location')}</Badge></Toolbar><Details value={row} title="Details" /></div><Toolbar><ConnectionTest connection={row} profiles={items(profiles.data)} /><EditResource operation="entity.update" row={row} fixed={{ collection: 'provider_connections' }} fields={[{ name: 'display_name', label: 'Name', required: true }, { name: 'endpoint', label: 'Endpoint (no credentials in URL)' }, { name: 'region', label: 'Region', advanced: true }, { name: 'deployment', label: 'Azure deployment', advanced: true }]} label="Edit connection" /><ConfirmCommand trigger="Remove connection" title="Remove connection?" message="Dependent active tasks block deletion. Revoke consent to cancel active tasks before removing a connection. Stored credentials are deleted separately in the native vault." operation="entity.delete" payload={{ collection: 'provider_connections', id: id(row), expected_revision: revision(row) }} /></Toolbar></article>)}</AsyncView></Panel><NewConnection descriptors={items(descriptors.data)} /></> },
    { label: 'Model profiles', content: <Panel title="Model profiles" actions={<ModelProfileCreation connections={items(connections.data)} />}><AsyncView query={profiles}>{(data) => items(data).length === 0 ? <Empty title="No model profiles" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{label(row)}</strong><p>{text(row.model_id)} · {text(row.role)}</p><Details value={row} title="Details" /></div><Toolbar><EditResource operation="entity.update" row={row} fixed={{ collection: 'model_profiles' }} fields={profileFields(items(connections.data))} label="Edit profile" /><ConfirmCommand trigger="Delete profile" title="Delete model profile?" message="Active dependent tasks must drain first. This does not delete unrelated model files." operation="entity.delete" payload={{ collection: 'model_profiles', id: id(row), expected_revision: revision(row) }} /></Toolbar></article>)}</AsyncView></Panel> },
    { label: 'Task routing', content: <Panel title="Task routing" actions={<Modal trigger={<button>Add task policy</button>} title="Configure task routing"><CommandForm operation="entity.create" fixed={{ collection: 'task_policies' }} fields={taskPolicyFields(items(profiles.data))} /></Modal>}><AsyncView query={policies}>{(data) => items(data).length === 0 ? <Empty title="No task routing yet" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{taskLabel(text(row.task))}</strong><p>Profile {text(row.model_profile_id)} · Language {text(row.language, 'auto')}</p><Details value={row} title="Details" /></div><EditResource operation="entity.update" row={row} fixed={{ collection: 'task_policies' }} fields={taskPolicyFields(items(profiles.data))} label="Edit task policy" /></article>)}</AsyncView></Panel> },
  ]} />;
}

function taskPolicyFields(profiles: RecordData[]): Field[] {
  return [
    { name: 'task', label: 'Task', type: 'select', value: 'summary', options: Object.keys(taskLabels).map((value) => ({ value, label: taskLabel(value) })) },
    { name: 'model_profile_id', label: 'Model profile', type: 'select', required: true, options: [{ value: '', label: 'Select a model profile' }, ...profiles.map((profile) => ({ value: id(profile), label: label(profile) }))] },
    { name: 'language', label: 'Generated output language', value: 'auto', advanced: true },
    { name: 'deadline_ms', label: 'Deadline (milliseconds)', type: 'number', value: 60000, min: 1000, max: 1800000, advanced: true },
    { name: 'token_limit', label: 'Maximum output tokens', type: 'number', value: 4096, min: 1, max: 32768, advanced: true },
    { name: 'fallback_ids', label: 'Explicit fallback profile IDs (JSON array)', type: 'json', value: [], advanced: true },
    { name: 'budget_limit', label: 'Maximum estimated cost per task (0 = no monetary cap)', type: 'number', value: 0, min: 0, advanced: true },
  ];
}

/** One form that creates the connection, profile, consent and task policies for summaries. */
function LlmSetup({ descriptors }: { descriptors: RecordData[] }) {
  const status = useResource('setup.status');
  const client = useQueryClient();
  const ollama = object(status.data?.ollama);
  const models = (Array.isArray(ollama.models) ? ollama.models : []).map((name) => text(name)).filter(Boolean);
  const adapters = descriptors.filter((descriptor) => Array.isArray(descriptor.roles) && descriptor.roles.includes('llm')).map(id).sort((a, b) => Number(b === 'ollama') - Number(a === 'ollama'));
  const [choice, setChoice] = useState('');
  const [model, setModel] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [secretRef, setSecretRef] = useState('');
  const [contextWindow, setContextWindow] = useState<string | null>(null);
  const adapter = choice || (ollama.available === true ? 'ollama' : adapters.find((candidate) => candidate !== 'ollama') ?? 'ollama');
  const isOllama = adapter === 'ollama';
  const needsEndpoint = adapter === 'openai-compatible' || adapter === 'azure-openai';
  const windowValue = contextWindow ?? (isOllama ? '' : '128000');
  const save = useMutation({
    mutationFn: () => request('setup.llm', { adapter, model, ...(needsEndpoint ? { endpoint } : {}), ...(!isOllama && secretRef ? { secret_ref: secretRef } : {}), ...(windowValue ? { context_window: Number(windowValue) } : {}) }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['engine'] }); },
  });
  const choose = (value: string): void => { setChoice(value); setModel(''); setEndpoint(''); setSecretRef(''); setContextWindow(null); save.reset(); };
  return <Panel title="AI for summaries" actions={status.data?.llm_ready === true ? <Badge tone="success">Ready</Badge> : undefined}>
    {adapters.length > 0 && <form className={styles.settingsForm} onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <FieldInput field={{ name: 'provider', label: 'Provider', type: 'select', options: adapters.map((value) => ({ value, label: value === 'ollama' && ollama.available === true ? 'ollama (running on this computer)' : value })) }} value={adapter} onChange={(value) => choose(text(value))} />
      {needsEndpoint && <FieldInput field={{ name: 'endpoint', label: 'Endpoint', required: true }} value={endpoint} onChange={(value) => setEndpoint(text(value))} />}
      {!isOllama && <><SecureSecret label="Provider API key" onStored={setSecretRef} />{secretRef && <Badge>API key stored</Badge>}</>}
      <FieldInput field={isOllama && models.length > 0 ? { name: 'model', label: 'Model', type: 'select', required: true, options: [{ value: '', label: 'Choose a model' }, ...models.map((name) => ({ value: name, label: name }))] } : { name: 'model', label: 'Model', required: true }} value={model} onChange={(value) => setModel(text(value))} />
      <FilterDisclosure title="Advanced"><FieldInput field={{ name: 'context_window', label: 'Context window (tokens)', type: 'number', min: 1 }} value={windowValue} onChange={(value) => setContextWindow(text(value))} /></FilterDisclosure>
      {Boolean(save.error) && <ErrorNotice error={save.error} />}
      {save.isSuccess && <Notice>AI for summaries is set up.</Notice>}
      <button type="submit" disabled={!nativeAvailable() || !model || save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</button>
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
  const endpointField: Field[] = localRuntime ? [] : [{ name: 'endpoint', label: 'Endpoint / resource URL', required: adapter !== 'ollama', value: adapter === 'ollama' ? 'http://127.0.0.1:11434' : undefined }];
  return <Modal open={open} onOpenChange={setOpen} trigger={<button>Add connection</button>} title="Choose an inference destination" description="Secrets stay in the native vault. Generic OpenAI-compatible endpoints use Chat Completions rather than assumed Responses/tool APIs.">
    <FieldInput field={{ name: 'adapter_id', label: 'Adapter', type: 'select', required: true, options: [{ value: '', label: 'Choose protocol adapter' }, ...descriptors.map((candidate) => ({ value: id(candidate), label: label(candidate) }))] }} value={adapter} onChange={(value) => { setAdapter(text(value)); setSecretRef(''); }} />
    {adapter && !localRuntime && <SecureSecret label="Provider API credential" onStored={setSecretRef} />}
    {secretRef && <Badge>Credential stored — opaque reference only</Badge>}
    {adapter && <CommandForm key={adapter} operation="entity.create" fixed={{ collection: 'provider_connections', adapter_id: adapter, secret_ref: secretRef || null, ...(localRuntime ? { endpoint: `local://${adapter}` } : {}) }} fields={[{ name: 'display_name', label: 'Connection name', required: true }, ...endpointField, { name: 'auth_kind', label: 'Authentication protocol', type: 'select', value: defaultAuth, options: ['none', 'bearer', 'api_key', 'entra'].map((value) => ({ value, label: value })), advanced: true }, { name: 'region', label: 'Region (where required)', advanced: true }, { name: 'declared_execution', label: 'Execution location declaration', type: 'select', value: localRuntime ? 'local' : 'unknown', options: [{ value: 'unknown', label: 'Unknown — remote consent required' }, { value: 'local', label: 'This device (verified local runtime)' }, { value: 'remote', label: 'External provider / self-hosted server' }], advanced: true }, { name: 'nonsecret_settings', label: 'Adapter settings (JSON: api_version, destination_fingerprint)', type: 'json', value: {}, advanced: true }]} onSuccess={() => setOpen(false)} />}
  </Modal>;
}

function ConnectionTest({ connection, profiles }: { connection: RecordData; profiles: RecordData[] }) {
  const [result, setResult] = useState<RecordData | null>(null);
  return <Modal trigger={<button>Test / capabilities…</button>} title="Real connection test" description="This makes a small request to the reviewed destination. It does not send meeting history. Review/grant connection_test consent to this endpoint first if remote."><p>Destination: {text(connection.endpoint, 'Selected local runtime')}</p><CommandForm operation="providers.test" fixed={{ connection_id: id(connection) }} fields={[{ name: 'profile_id', label: 'Model profile to test', type: 'select', required: true, options: [{ value: '', label: 'Choose model profile' }, ...profiles.filter((profile) => profile.connection_id === id(connection)).map((profile) => ({ value: id(profile), label: label(profile) }))] }]} submit="Run actual connection test" confirm="I authorize this small outbound connection test to the displayed destination." onSuccess={(data) => setResult(item(data))} />{result && <Details value={result} title="Details" />}</Modal>;
}

export function ModelSettings() {
  const catalog = useResource('catalog.list');
  return <Panel title="Models & runtime" actions={<ModelArchiveImport templates={items(catalog.data)} />}><AsyncView query={catalog}>{(data) => {
    // Runtimes are fetched with their model; source descriptors can't be downloaded.
    const visible = items(data).filter((row) => row.role !== 'runtime' && !row.blocked_reason);
    return visible.length === 0 ? <Empty title="No models yet" /> : <Grid>{visible.map((row) => <ModelCard key={id(row)} entry={row} />)}</Grid>;
  }}</AsyncView></Panel>;
}

function ModelCard({ entry }: { entry: RecordData }) {
  const [open, setOpen] = useState(false);
  const license = object(entry.license);
  const bytes = typeof entry.bytes === 'number' ? entry.bytes : array(entry.files).reduce((total, file) => total + (typeof file.bytes === 'number' ? file.bytes : 0), 0);
  return <Panel title={label(entry)} actions={<><Badge>{text(entry.role)}</Badge>{entry.gpu === true && <Badge>GPU</Badge>}{entry.installed === true && <Badge>Installed</Badge>}</>}><dl><dt>Download</dt><dd>{(bytes / 1024 / 1024).toFixed(1)} MiB</dd><dt>License</dt><dd>{text(license.spdx ?? entry.license_spdx, 'Review license details')}</dd><dt>Revision</dt><dd>{text(entry.revision ?? entry.upstream_revision)}</dd></dl>{entry.installed === true ? <ConfirmCommand trigger="Delete" title={`Delete ${label(entry)}?`} message="Loaded models must finish their jobs before removal." operation="catalog.delete" payload={{ id: id(entry), confirmed: true, revision: text(entry.revision) }} /> : <Modal open={open} onOpenChange={setOpen} trigger={<button>Download…</button>} title={`Download ${label(entry)}`}><Details value={entry} title="Details" /><p className={styles.preWrap}>{text(license.text ?? entry.license_text)}</p><CommandForm operation="catalog.download" fixed={{ id: id(entry), accepted_license: true, accepted_destination: true }} fields={entry.gating && object(entry.gating).required ? [{ name: 'accepted_terms', label: 'I accepted the upstream access terms', type: 'checkbox', value: false }, { name: 'secret_ref', label: 'Hugging Face credential reference' }] : []} submit="Download" confirm="I accept the license above." onSuccess={() => setOpen(false)} /></Modal>}</Panel>;
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
  return <Modal trigger={<button>Import user-supplied model archive…</button>} title="Import unreviewed model weights" description="Choose a ZIP/tar weights archive for an exact compatible template. This imports model data only, never executables. File-set validation and computed SHA-256 do not establish upstream authenticity or license approval.">
    <FieldInput field={{ name: 'template_id', label: 'Compatible reviewed file-layout template', type: 'select', options: [{ value: '', label: 'Choose exact model layout' }, ...templates.filter((entry) => entry.kind !== 'runtime' && entry.executable !== true).map((entry) => ({ value: id(entry), label: label(entry) }))] }} value={templateId} onChange={(value) => setTemplateId(text(value))} />
    {selected && <Details title="Details" value={selected} />}
    <label className={styles.check}><input type="checkbox" checked={unreviewed} onChange={(event) => setUnreviewed(event.target.checked)} />I understand this archive is user-supplied and unreviewed; its descriptor will retain that provenance.</label>
    <label className={styles.check}><input type="checkbox" checked={licensed} onChange={(event) => setLicensed(event.target.checked)} />I reviewed the actual upstream model license and have the required usage/access rights.</label>
    <button disabled={!nativeAvailable() || !templateId || !unreviewed || !licensed || operation.isPending} onClick={() => operation.mutate()}>{operation.isPending ? 'Validating actual archive and hashing…' : 'Choose archive and import model data'}</button>
    {Boolean(operation.error) && <ErrorNotice error={operation.error} />}
    {Boolean(operation.isSuccess) && <><Notice>The result below is authoritative. Use its imported descriptor ID as the model ID in a compatible local profile.</Notice><Details title="Details" value={operation.data} /></>}
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
  return <Modal trigger={<button>Add model profile</button>} title="Add actual model profile" description="Choose an acquired/imported model descriptor or enter a manual provider model/deployment ID.">
    <FieldInput field={{ name: 'catalog_model', label: 'Acquired / catalog model descriptor', type: 'select', options: [{ value: '', label: 'Enter an arbitrary supported provider model ID manually' }, ...items(catalog.data).filter((entry) => entry.role !== 'runtime').map((entry) => ({ value: id(entry), label: `${label(entry)} · ${text(entry.provenance ?? entry.review_status, 'Catalog provenance')}` }))] }} value={selectedId} onChange={(value) => { setSelectedId(text(value)); setRuntimeId(''); }} />
    {selected && <Details title="Details" value={selected} />}
    {selected && <FieldInput field={{ name: 'runtime_descriptor', label: 'Runtime', type: 'select', options: [{ value: '', label: 'Choose runtime' }, ...runtimes.map((entry) => ({ value: id(entry), label: entry.host_check ? `${label(entry)} (GPU)` : label(entry) }))] }} value={runtime} onChange={(value) => setRuntimeId(text(value))} />}
    <CommandForm key={`${selectedId || 'manual'}:${runtime}`} operation="entity.create" fixed={{ collection: 'model_profiles', ...(selectedId ? { model_id: selectedId } : {}) }} fields={fields} />
  </Modal>;
}
