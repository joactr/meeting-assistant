import { useState } from 'react';
import { useResource } from '../lib/queries';
import { array, date, id, item, items, label, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, FieldInput, Modal, Notice, PageHeader, Panel, TabSet, Toolbar, type Field } from '../components/ui';
import { Citation, CitedResult } from './outcomes';
import styles from './workspace.module.css';

const vocabularyFields: Field[] = [
  { name: 'spelling', label: 'Preferred spelling', required: true },
  { name: 'aliases', label: 'Aliases (JSON array)', type: 'json', value: [] },
  { name: 'pronunciation', label: 'Pronunciation (optional)' },
  { name: 'language', label: 'Language code', value: 'en' },
  { name: 'priority', label: 'Priority', type: 'number', value: 50, min: 0, max: 100 },
  { name: 'enabled', label: 'Enabled', type: 'checkbox', value: true },
];

export function ProjectsPage() {
  const projects = useResource('projects.list');
  const [projectId, setProjectId] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const selected = items(projects.data).find((project) => id(project) === projectId);
  return <><PageHeader title="Projects"><Modal open={createOpen} onOpenChange={setCreateOpen} trigger={<button>New project</button>} title="New project"><CommandForm operation="projects.create" fields={[{ name: 'name', label: 'Name', required: true }, { name: 'description', label: 'Description', type: 'textarea' }]} onSuccess={(result) => { setProjectId(id(item(result))); setCreateOpen(false); }} /></Modal></PageHeader><AsyncView query={projects}>{(data) => <Panel><FieldInput field={{ name: 'project', label: 'Project', type: 'select', options: [{ value: '', label: 'All projects' }, ...items(data).map((project) => ({ value: id(project), label: label(project) }))] }} value={projectId} onChange={(value) => setProjectId(text(value))} />{selected && <Toolbar><EditResource row={selected} operation="projects.update" label="Edit project" fields={[{ name: 'name', label: 'Name', required: true }, { name: 'description', label: 'Description', type: 'textarea' }]} /><ConfirmCommand trigger="Archive project" title="Archive project?" message="Its meetings are kept; the project is hidden from lists." operation="projects.archive" payload={{ id: projectId, expected_revision: revision(selected) }} /></Toolbar>}</Panel>}</AsyncView><TabSet tabs={[
    { label: 'Vocabulary', content: <Vocabulary projectId={projectId} /> },
    { label: 'Briefings', content: projectId ? <ContextPanel projectId={projectId} /> : <Empty title="Select a project" /> },
    { label: 'Digests', content: projectId ? <Digests projectId={projectId} /> : <Empty title="Select a project" /> },
  ]} /></>;
}

export function Vocabulary({ projectId = '' }: { projectId?: string }) {
  const query = useResource('vocabulary.list', { project_id: projectId || null });
  const profiles = useResource('entity.list', { collection: 'model_profiles' });
  const [profileId, setProfileId] = useState('');
  const [compiled, setCompiled] = useState<RecordData | null>(null);
  return <Panel title={projectId ? 'Project vocabulary' : 'Workspace vocabulary'} actions={<Modal trigger={<button>Add term</button>} title="Add term"><CommandForm operation="vocabulary.save" fixed={{ project_id: projectId || null }} fields={vocabularyFields} /></Modal>}>
    <AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No vocabulary terms" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}>
      <div><strong>{text(row.spelling ?? row.term)}</strong><Toolbar>{(row.enabled === false || row.enabled === 0) && <Badge>Disabled</Badge>}<small>{text(row.language)}, priority {text(row.priority)}, {row.project_id ? 'this project' : 'all projects'}</small></Toolbar><p>{Array.isArray(row.aliases) ? row.aliases.map((value) => text(value)).join(', ') : ''}</p></div>
      <Toolbar><EditResource row={row} operation="vocabulary.save" fields={vocabularyFields} label="Edit term" /><ConfirmCommand trigger="Remove term" title="Remove term?" message="Existing transcripts are not changed." operation="vocabulary.delete" payload={{ id: id(row), expected_revision: revision(row) }} /></Toolbar>
    </article>)}</AsyncView>
    <Modal trigger={<button>Check vocabulary…</button>} title="Check vocabulary"><FieldInput field={{ name: 'profile_id', label: 'Transcription profile', type: 'select', options: [{ value: '', label: 'Choose profile' }, ...items(profiles.data).filter((row) => row.role === 'asr').map((row) => ({ value: id(row), label: label(row) }))] }} value={profileId} onChange={(value) => { setProfileId(text(value)); setCompiled(null); }} />
    {profileId && <CommandForm operation="vocabulary.compile" fixed={{ project_id: projectId || null, profile_id: profileId }} fields={[]} submit="Check" onSuccess={setCompiled} />}
    {compiled && <><Details value={compiled} />{compiled.acknowledgement_required === true ? <CommandButton operation="vocabulary.compile" payload={{ project_id: projectId || null, profile_id: profileId, acknowledgement: compiled.acknowledgement_key ?? '' }} onSuccess={setCompiled}>Acknowledge omitted terms</CommandButton> : <Notice>Reviewed.</Notice>}</>}</Modal>
  </Panel>;
}
function ContextPanel({ projectId }: { projectId: string }) {
  const memory = useResource('entity.list', { collection: 'memory_segments', filters: { project_id: projectId } });
  const briefings = useResource('entity.list', { collection: 'briefings', filters: { project_id: projectId } });
  return <Panel title="Context"><AsyncView query={memory}>{(data) => items(data).length === 0 ? <Empty title="No context yet" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><p>{text(row.text ?? row.body ?? row.content)}</p>{row.stale === true && <Badge tone="warning">Out of date</Badge>}<Citation source={row} /><Details value={row} /></div></article>)}</AsyncView><Modal trigger={<button>Generate briefing…</button>} title="Generate briefing"><CommandForm operation="analysis.generate" fixed={{ task: 'briefings', project_id: projectId, idempotency_key: crypto.randomUUID() }} fields={[{ name: 'meeting_id', label: 'Meeting', required: true }, { name: 'briefing_kind', label: 'Briefing', type: 'select', value: 'pre', options: [{ value: 'pre', label: 'Before: recent related meetings' }, { value: 'post', label: 'After: what changed' }] }, { name: 'human_agenda', label: 'Agenda', type: 'textarea' }]} submit="Generate" /></Modal><AsyncView query={briefings}>{(data) => items(data).map((row) => <CitedResult key={id(row)} result={row} />)}</AsyncView></Panel>;
}

function Digests({ projectId }: { projectId: string }) {
  const schedule = useResource('digest.schedule.get', { project_id: projectId });
  const inbox = useResource('digest.inbox', { project_id: projectId });
  const history = useResource('digest.history', { project_id: projectId });
  const destinations = useResource('delivery.list');
  const [preview, setPreview] = useState<RecordData | null>(null);
  const eligible = items(destinations.data).filter((row) => row.project_id === projectId && !row.meeting_id);
  return <>
    <Panel title="Schedule">
      <AsyncView query={schedule}>{(data) => { const row = item(data); return <CommandForm key={`${projectId}:${revision(row)}`} operation="digest.schedule.save" fixed={{ project_id: projectId, expected_revision: row.revision ?? 0 }} fields={[
        { name: 'enabled', label: 'Enable schedule', type: 'checkbox', value: row.enabled ?? false },
        { name: 'weekday', label: 'Weekday (0 = Monday)', type: 'number', value: row.weekday ?? 4, min: 0, max: 6 },
        { name: 'time', label: 'Local time', type: 'time', value: row.time ?? row.local_time ?? '16:00' },
        { name: 'timezone', label: 'Timezone', value: row.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone, required: true },
        { name: 'delivery_ids', label: 'Delivery IDs (JSON array; empty = inbox only)', type: 'json', value: row.delivery_ids ?? [] },
      ]} submit="Save" />; }}</AsyncView>
      <Toolbar><CommandButton operation="digest.preview" payload={{ project_id: projectId }} onSuccess={(data) => setPreview(item(data))}>Preview</CommandButton><CommandButton operation="digest.run" payload={{ project_id: projectId, manual: true, idempotency_key: crypto.randomUUID() }}>Generate now</CommandButton></Toolbar>
      {preview && <><CitedResult result={preview} /><Details value={preview} /><DigestDestination projectId={projectId} preview={preview} destinations={eligible} /></>}
      <Details value={{ items: eligible }} />
    </Panel>
    <TabSet tabs={[
      { label: 'Inbox', content: <Panel title="Inbox"><AsyncView query={inbox}>{(data) => items(data).length === 0 ? <Empty title="No digests yet" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><Badge>{row.read ? 'Read' : 'Unread'}</Badge><CitedResult result={object(row.run)} /><Details value={row.run} /></div><CommandButton operation="digest.inbox" payload={{ project_id: projectId, id: id(row), expected_revision: revision(row), read: !row.read }}>Mark {row.read ? 'unread' : 'read'}</CommandButton></article>)}</AsyncView></Panel> },
      { label: 'Run history', content: <Panel title="History"><AsyncView query={history}>{(data) => items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{date(row.period_end)}</strong><Badge tone={row.stale ? 'warning' : 'neutral'}>{row.stale ? 'Out of date' : text(row.state ?? row.status)}</Badge><CitedResult result={row} /><Details value={row} /><DigestRunActions run={row} destinations={eligible} /></div></article>)}</AsyncView></Panel> },
    ]} />
  </>;
}

function DigestDestination({ projectId, preview, destinations }: { projectId: string; preview: RecordData; destinations: RecordData[] }) {
  const [destinationId, setDestinationId] = useState('');
  const selected = destinations.find((row) => id(row) === destinationId);
  return <Modal trigger={<button>Set up automatic delivery…</button>} title="Automatic delivery"><FieldInput field={{ name: 'destination', label: 'Destination', type: 'select', options: [{ value: '', label: 'Choose destination' }, ...destinations.map((row) => ({ value: id(row), label: `${text(row.transport)}: ${text(row.destination)}` }))] }} value={destinationId} onChange={(value) => setDestinationId(text(value))} />
  {selected && <><Details value={selected} /><CommandForm operation="digest.delivery.configure" fixed={{ ...selected, project_id: projectId, expected_revision: revision(selected), automatic: true, enabled: true, preview_id: id(preview), preview_hash: preview.preview_hash ?? '', reviewed: true }} fields={[]} submit="Authorize" confirm="I reviewed the preview, recipients and destination." /></>}</Modal>;
}

function DigestRunActions({ run, destinations }: { run: RecordData; destinations: RecordData[] }) {
  return <>
    <Modal trigger={<button>Send…</button>} title="Send digest"><CommandForm operation="digest.delivery.send" fixed={{ run_id: id(run), reviewed: true }} fields={[{ name: 'delivery_id', label: 'Destination', type: 'select', required: true, options: [{ value: '', label: 'Choose destination' }, ...destinations.map((row) => ({ value: id(row), label: `${text(row.transport)}: ${text(row.destination)}` }))] }, { name: 'authorize_duplicate_risk', label: 'Allow possible duplicate delivery', type: 'checkbox', value: false }]} submit="Send" confirm="I reviewed the content and recipients." /></Modal>
    {array(run.deliveries).map((delivery) => <section key={id(delivery)}><Badge>{text(delivery.state)}</Badge><Details value={delivery} /><Modal trigger={<button>Confirm receipt…</button>} title="Confirm receipt"><CommandForm operation="digest.delivery.reconcile" fixed={{ id: id(delivery), reviewed: true }} fields={[{ name: 'confirmation', label: 'Outcome', type: 'select', required: true, options: [{ value: '', label: 'Choose outcome' }, { value: 'confirmed_received', label: 'Received' }, { value: 'confirmed_not_received', label: 'Not received' }] }]} confirm="I checked the destination." /></Modal></section>)}
  </>;
}
