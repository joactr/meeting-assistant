import { useState } from 'react';
import { useResource } from '../lib/queries';
import { array, date, id, item, items, label, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, FieldInput, Grid, Modal, Notice, PageHeader, Panel, TabSet, Toolbar, type Field } from '../components/ui';
import { Citation, CitedResult } from './outcomes';
import styles from './workspace.module.css';

const vocabularyFields: Field[] = [
  { name: 'spelling', label: 'Preferred spelling', required: true },
  { name: 'aliases', label: 'Aliases (JSON array)', type: 'json', value: [] },
  { name: 'pronunciation', label: 'Pronunciation (optional)' },
  { name: 'language', label: 'Language (BCP-47)', value: 'en' },
  { name: 'priority', label: 'Priority', type: 'number', value: 50, min: 0, max: 100 },
  { name: 'enabled', label: 'Enabled', type: 'checkbox', value: true },
];

export function ProjectsPage() {
  const projects = useResource('projects.list');
  const [projectId, setProjectId] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const selected = items(projects.data).find((project) => id(project) === projectId);
  return <><PageHeader title="Projects"><Modal open={createOpen} onOpenChange={setCreateOpen} trigger={<button>New project</button>} title="Create a project"><CommandForm operation="projects.create" fields={[{ name: 'name', label: 'Project name', required: true }, { name: 'description', label: 'Purpose / context', type: 'textarea' }]} onSuccess={(result) => { setProjectId(id(item(result))); setCreateOpen(false); }} /></Modal></PageHeader><AsyncView query={projects}>{(data) => <Panel><FieldInput field={{ name: 'project', label: 'Project scope', type: 'select', options: [{ value: '', label: 'All projects' }, ...items(data).map((project) => ({ value: id(project), label: label(project) }))] }} value={projectId} onChange={(value) => setProjectId(text(value))} />{selected && <Toolbar><EditResource row={selected} operation="projects.update" label="Edit project" fields={[{ name: 'name', label: 'Name', required: true }, { name: 'description', label: 'Description', type: 'textarea' }]} /><ConfirmCommand trigger="Archive project" title="Archive project?" message="Existing meeting evidence is retained. Archived projects are excluded from normal active selectors." operation="projects.archive" payload={{ id: projectId, expected_revision: revision(selected) }} /></Toolbar>}</Panel>}</AsyncView><TabSet tabs={[
    { label: 'Vocabulary', content: <Vocabulary projectId={projectId} /> },
    { label: 'Context & briefings', content: projectId ? <ContextPanel projectId={projectId} /> : <Empty title="Select a project" /> },
    { label: 'Digests', content: projectId ? <Digests projectId={projectId} /> : <Empty title="Select a project" /> },
    { label: 'Templates', content: <Templates /> },
  ]} /></>;
}

export function Vocabulary({ projectId = '' }: { projectId?: string }) {
  const query = useResource('vocabulary.list', { project_id: projectId || null });
  const profiles = useResource('entity.list', { collection: 'model_profiles' });
  const [profileId, setProfileId] = useState('');
  const [compiled, setCompiled] = useState<RecordData | null>(null);
  return <Panel title={projectId ? 'Project vocabulary' : 'Workspace vocabulary'} actions={<Modal trigger={<button>Add term</button>} title="Add recognition hint"><CommandForm operation="vocabulary.save" fixed={{ project_id: projectId || null }} fields={vocabularyFields} /></Modal>}>
    <AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No vocabulary terms" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}>
      <div><strong>{text(row.spelling ?? row.term)}</strong><Toolbar><Badge>{row.enabled === false || row.enabled === 0 ? 'Disabled' : 'Enabled'}</Badge><small>{text(row.language)} · Priority {text(row.priority)} · {row.project_id ? 'Project' : 'Workspace'}</small></Toolbar><p>{Array.isArray(row.aliases) ? row.aliases.map((value) => text(value)).join(', ') : ''}</p></div>
      <Toolbar><EditResource row={row} operation="vocabulary.save" fields={vocabularyFields} label="Edit term" /><ConfirmCommand trigger="Remove term" title="Remove vocabulary hint?" message="Existing transcripts are not rewritten." operation="vocabulary.delete" payload={{ id: id(row), expected_revision: revision(row) }} /></Toolbar>
    </article>)}</AsyncView>
    <Modal trigger={<button>Inspect effective provider hints…</button>} title="Compile effective vocabulary"><FieldInput field={{ name: 'profile_id', label: 'ASR profile', type: 'select', options: [{ value: '', label: 'Select profile' }, ...items(profiles.data).filter((row) => row.role === 'asr').map((row) => ({ value: id(row), label: label(row) }))] }} value={profileId} onChange={(value) => { setProfileId(text(value)); setCompiled(null); }} />
    {profileId && <CommandForm operation="vocabulary.compile" fixed={{ project_id: projectId || null, profile_id: profileId }} fields={[]} submit="Inspect compilation" onSuccess={setCompiled} />}
    {compiled && <><Details value={compiled} />{compiled.acknowledgement_required === true ? <CommandButton operation="vocabulary.compile" payload={{ project_id: projectId || null, profile_id: profileId, acknowledgement: compiled.acknowledgement_key ?? '' }} onSuccess={setCompiled}>Acknowledge this exact omitted set</CommandButton> : <Notice>Compilation reviewed. The engine checks this exact vocabulary revision again before capture; any changed omissions require new acknowledgement.</Notice>}</>}</Modal>
  </Panel>;
}
function ContextPanel({ projectId }: { projectId: string }) {
  const memory = useResource('entity.list', { collection: 'memory_segments', filters: { project_id: projectId } });
  const briefings = useResource('entity.list', { collection: 'briefings', filters: { project_id: projectId } });
  return <Panel title="Cited project context"><AsyncView query={memory}>{(data) => items(data).length === 0 ? <Empty title="No project memory yet" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><p>{text(row.text ?? row.body ?? row.content)}</p>{row.stale === true && <Badge tone="warning">Stale evidence</Badge>}<Citation source={row} /><Details value={row} /></div></article>)}</AsyncView><Modal trigger={<button>Generate scoped pre-/post-brief…</button>} title="Generate a cited briefing"><CommandForm operation="analysis.generate" fixed={{ task: 'briefings', project_id: projectId, idempotency_key: crypto.randomUUID() }} fields={[{ name: 'meeting_id', label: 'Meeting to prepare / summarize', required: true }, { name: 'briefing_kind', label: 'Briefing', type: 'select', value: 'pre', options: [{ value: 'pre', label: 'Pre-brief: recent five relevant meetings' }, { value: 'post', label: 'Post-brief: changes against prior scoped facts' }] }, { name: 'human_agenda', label: 'Human agenda', type: 'textarea' }]} submit="Generate briefing" /></Modal><AsyncView query={briefings}>{(data) => items(data).map((row) => <CitedResult key={id(row)} result={row} />)}</AsyncView></Panel>;
}

function Templates() {
  const query = useResource('templates.list');
  return <Panel title="Meeting templates" actions={<Modal trigger={<button>Create template</button>} title="Create a template"><CommandForm operation="templates.save" fields={[{ name: 'name', label: 'Name', required: true }, { name: 'sections', label: 'Ordered section schema (JSON)', type: 'json', value: [{ id: 'summary', title: 'Summary', instructions: 'Summarize decisions with citations.' }], required: true }, { name: 'custom_fields', label: 'Custom fields (JSON data)', type: 'json', value: {} }]} /></Modal>}><AsyncView query={query}>{(data) => <Grid>{items(data).map((row) => <Panel key={id(row)} title={label(row)} actions={<Badge>v{text(row.version, '1')}</Badge>}><p>{text(row.description)}</p><ul>{array(row.sections).map((section) => <li key={id(section)}>{text(section.title ?? section.heading)}</li>)}</ul><Toolbar><CommandButton operation="templates.clone" payload={{ id: id(row), name: `${label(row)} copy` }}>Clone template</CommandButton>{row.builtin !== true && <EditResource operation="templates.save" row={row} label="Edit template" fields={[{ name: 'name', label: 'Name', required: true }, { name: 'sections', label: 'Section schema (JSON)', type: 'json', required: true }, { name: 'custom_fields', label: 'Custom fields (JSON data)', type: 'json' }]} />}</Toolbar><Details value={row} /></Panel>)}</Grid>}</AsyncView></Panel>;
}

function Digests({ projectId }: { projectId: string }) {
  const schedule = useResource('digest.schedule.get', { project_id: projectId });
  const inbox = useResource('digest.inbox', { project_id: projectId });
  const history = useResource('digest.history', { project_id: projectId });
  const destinations = useResource('delivery.list');
  const [preview, setPreview] = useState<RecordData | null>(null);
  const eligible = items(destinations.data).filter((row) => row.project_id === projectId && !row.meeting_id);
  return <>
    <Panel title="Project digest schedule">
      <AsyncView query={schedule}>{(data) => { const row = item(data); return <CommandForm key={`${projectId}:${revision(row)}`} operation="digest.schedule.save" fixed={{ project_id: projectId, expected_revision: row.revision ?? 0 }} fields={[
        { name: 'enabled', label: 'Enable local digest schedule', type: 'checkbox', value: row.enabled ?? false },
        { name: 'weekday', label: 'Weekday (Monday 0–Sunday 6)', type: 'number', value: row.weekday ?? 4, min: 0, max: 6 },
        { name: 'time', label: 'Local time', type: 'time', value: row.time ?? row.local_time ?? '16:00' },
        { name: 'timezone', label: 'IANA timezone', value: row.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone, required: true },
        { name: 'delivery_ids', label: 'Reviewed automatic destination IDs (empty array = local inbox only)', type: 'json', value: row.delivery_ids ?? [] },
      ]} submit="Save local schedule" />; }}</AsyncView>
      <Toolbar><CommandButton operation="digest.preview" payload={{ project_id: projectId }} onSuccess={(data) => setPreview(item(data))}>Preview without advancing schedule</CommandButton><CommandButton operation="digest.run" payload={{ project_id: projectId, manual: true, idempotency_key: crypto.randomUUID() }}>Generate digest now</CommandButton></Toolbar>
      {preview && <><CitedResult result={preview} /><Details value={preview} /><DigestDestination projectId={projectId} preview={preview} destinations={eligible} /></>}
      <Details value={{ items: eligible }} />
    </Panel>
    <TabSet tabs={[
      { label: 'Inbox', content: <Panel title="Local digest inbox"><AsyncView query={inbox}>{(data) => items(data).length === 0 ? <Empty title="No digest yet" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><Badge>{row.read ? 'Read' : 'Unread'}</Badge><CitedResult result={object(row.run)} /><Details value={row.run} /></div><CommandButton operation="digest.inbox" payload={{ project_id: projectId, id: id(row), expected_revision: revision(row), read: !row.read }}>Mark {row.read ? 'unread' : 'read'}</CommandButton></article>)}</AsyncView></Panel> },
      { label: 'Run history', content: <Panel title="Generation and delivery are separate"><AsyncView query={history}>{(data) => items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{date(row.period_end)}</strong><Badge tone={row.stale ? 'warning' : 'neutral'}>{row.stale ? 'Stale source revisions' : text(row.state ?? row.status)}</Badge><CitedResult result={row} /><Details value={row} /><DigestRunActions run={row} destinations={eligible} /></div></article>)}</AsyncView></Panel> },
    ]} />
  </>;
}

function DigestDestination({ projectId, preview, destinations }: { projectId: string; preview: RecordData; destinations: RecordData[] }) {
  const [destinationId, setDestinationId] = useState('');
  const selected = destinations.find((row) => id(row) === destinationId);
  return <Modal trigger={<button>Authorize fixed automatic delivery…</button>} title="Review scheduled digest delivery" description="Review recipients before authorizing fixed delivery."><FieldInput field={{ name: 'destination', label: 'Fixed configured destination', type: 'select', options: [{ value: '', label: 'Choose project-scoped destination' }, ...destinations.map((row) => ({ value: id(row), label: `${text(row.transport)} · ${text(row.destination)} · ${id(row)}` }))] }} value={destinationId} onChange={(value) => setDestinationId(text(value))} />
  {selected && <><Details value={selected} /><CommandForm operation="digest.delivery.configure" fixed={{ ...selected, project_id: projectId, expected_revision: revision(selected), automatic: true, enabled: true, preview_id: id(preview), preview_hash: preview.preview_hash ?? '', reviewed: true }} fields={[]} submit="Authorize this exact destination" confirm="I reviewed this digest preview, project scope, fixed destination and recipient disclosure." /></>}</Modal>;
}

function DigestRunActions({ run, destinations }: { run: RecordData; destinations: RecordData[] }) {
  return <>
    <Modal trigger={<button>Review a delivery attempt…</button>} title="Review fixed digest destination" description="Uncertain timeouts may mean duplicate delivery."><CommandForm operation="digest.delivery.send" fixed={{ run_id: id(run), reviewed: true }} fields={[{ name: 'delivery_id', label: 'Fixed scoped destination', type: 'select', required: true, options: [{ value: '', label: 'Choose destination' }, ...destinations.map((row) => ({ value: id(row), label: `${text(row.transport)} · ${text(row.destination)}` }))] }, { name: 'authorize_duplicate_risk', label: 'Explicitly authorize possible duplicate delivery after checking the recipient', type: 'checkbox', value: false }]} submit="Send reviewed digest" confirm="I reviewed the generated content, its freshness and the fixed destination/recipients." /></Modal>
    {array(run.deliveries).map((delivery) => <section key={id(delivery)}><Badge>{text(delivery.state)}</Badge><Details value={delivery} /><Modal trigger={<button>Reconcile recipient confirmation…</button>} title="Record real receipt evidence"><CommandForm operation="digest.delivery.reconcile" fixed={{ id: id(delivery), reviewed: true }} fields={[{ name: 'confirmation', label: 'Verified outcome at the fixed destination', type: 'select', required: true, options: [{ value: '', label: 'Choose only after actually checking' }, { value: 'confirmed_received', label: 'Recipient confirmed receipt' }, { value: 'confirmed_not_received', label: 'Recipient confirmed no receipt' }] }]} confirm="I actually checked the destination; this is not an assumed provider acknowledgement." /></Modal></section>)}
  </>;
}
