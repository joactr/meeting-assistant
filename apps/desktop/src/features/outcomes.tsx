import { useState } from 'react';
import { Link, useNavigate } from '@tanstack/react-router';
import { useCommand, useResource } from '../lib/queries';
import { useInterface } from '../lib/ui-store';
import { nativeAvailable, nativeRequest } from '../lib/bridge';
import { array, clock, date, id, item, items, object, revision, text, type RecordData } from '../lib/data';
import { ActionMenu } from '../components/controls';
import { accuracyNotice, AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, Panel, Toolbar, useOpenState, type Field } from '../components/ui';
import styles from './workspace.module.css';

export const actionFields: Field[] = [
  { name: 'description', label: 'Action / commitment', required: true },
  { name: 'assignee_text', label: 'Assignee' },
  { name: 'due_date', label: 'Due date', type: 'date' },
  { name: 'priority', label: 'Priority', type: 'select', value: 'normal', options: ['low', 'normal', 'high', 'urgent'].map((value) => ({ value, label: value })) },
  { name: 'status', label: 'Status', type: 'select', value: 'open', options: ['open', 'in_progress', 'done', 'dismissed'].map((value) => ({ value, label: value.replaceAll('_', ' ') })) },
];

export function Citation({ source, number: citationNumber }: { source: RecordData; number?: number }) {
  const select = useInterface((state) => state.selectUtterance);
  const activeMeeting = useInterface((state) => state.activeMeeting);
  const meetingId = text(source.meeting_id) || activeMeeting || '';
  const utteranceId = text(source.utterance_id ?? source.source_utterance_id ?? source.start_utterance_id ?? source.source_id);
  if (source.type === 'human_note' || source.provenance === 'Human note') return <Badge>Human note · not spoken evidence</Badge>;
  if (!meetingId || !utteranceId) return <Badge>{citationNumber ? `[${citationNumber}] ` : ''}Source reference unavailable</Badge>;
  return <Link className={styles.citation} to="/meeting/$meetingId" params={{ meetingId }} onClick={() => select(utteranceId)} title={text(source.quote ?? source.quote_snapshot ?? source.excerpt, 'Open exact cited source')}><span>{citationNumber ? `[${citationNumber}]` : '↗'} {text(source.meeting_title, 'Source')} · {clock(source.start_ms)}</span>{(source.stale === true || source.stale === 1) && <Badge tone="warning">Stale revision</Badge>}</Link>;
}

export function CitedResult({ result }: { result: RecordData }) {
  const data = object(result.result ?? result.content ?? result.payload ?? result);
  const sections = array(data.sections);
  const groups = ['cues', 'questions', 'topics', 'decisions', 'risks', 'milestones', 'deliverables', 'follow_ups', 'candidates', 'actions'].filter((key) => Array.isArray(data[key]));
  const body = text(data.answer ?? data.summary ?? data.text ?? data.body ?? data.markdown);
  return <div className={styles.result}>
    {Boolean(result.state) && <Badge>{text(result.state)}</Badge>}
    {Boolean(result.stale) && <Badge tone="warning">Stale source revisions</Badge>}
    {Boolean(result.updated_at) && <small>Last successful generation {date(result.updated_at)}</small>}
    {sections.map((section, index) => <section key={id(section) || index}>
      <h3>{text(section.heading ?? section.title)}</h3>
      <p className={styles.preWrap}>{text(section.body ?? section.text)}</p>
      {Boolean(section.human_note) && <p className={styles.preWrap}>Human note: {text(section.human_note)}</p>}
      <Toolbar>
        {Boolean(section.source ?? section.provenance) && <Badge>{text(section.source ?? section.provenance, 'See detailed provenance')}</Badge>}
        {array(section.source_refs ?? section.citations).map((source, citationIndex) => <Citation key={id(source) || citationIndex} source={source} number={citationIndex + 1} />)}
      </Toolbar>
    </section>)}
    {!sections.length && body && <p className={styles.preWrap}>{body}</p>}
    {groups.map((key) => <section key={key}>
      <h3>{key.replaceAll('_', ' ')}</h3>
      {array(data[key]).map((entry, index) => <article key={id(entry) || index} className={styles.actionRow} style={{ marginLeft: entry.parent_key ? '1.25rem' : 0 }}>
        <strong>{text(entry.title ?? entry.description ?? entry.text ?? entry.question)}</strong>
        <Toolbar>
          {Boolean(entry.kind) && <Badge>{text(entry.kind)}</Badge>}
          {Boolean(entry.sentiment) && <Badge>Descriptive: {text(entry.sentiment)}</Badge>}
          {entry.start_ms !== undefined && <small>{clock(entry.start_ms)}–{clock(entry.end_ms)}</small>}
          {Boolean(entry.parent_key) && <small>Under {text(entry.parent_key)}</small>}
          {array(entry.source_refs).map((source, citationIndex) => <Citation key={citationIndex} source={source} number={citationIndex + 1} />)}
        </Toolbar>
      </article>)}
    </section>)}
    {array(data.citations).map((source, index) => <Citation key={id(source) || index} source={source} number={index + 1} />)}
    {Array.isArray(data.warnings) && data.warnings.length > 0 && <Notice>{data.warnings.map((warning) => typeof warning === 'string' ? warning : JSON.stringify(warning)).join(' · ')}</Notice>}
    {!sections.length && !body && !groups.length && <Details title="Structured result and provenance" value={data} />}
  </div>;
}

export function AnalysisPanel({ meetingId, task = 'summary' }: { meetingId: string; task?: string }) {
  const query = useResource('analysis.list', { meeting_id: meetingId, task });
  return <Panel title={task === 'summary' ? 'Summary' : 'Topics & outcomes'} actions={<CommandButton operation="analysis.generate" payload={{ meeting_id: meetingId, task, idempotency_key: crypto.randomUUID() }}>Generate {task}</CommandButton>}><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title={`No ${task} yet`}>Uses the AI provider you set up in Settings.</Empty> : <><Notice>{accuracyNotice}</Notice>{items(data).map((result) => <CitedResult key={id(result)} result={result} />)}</>}</AsyncView>{task === 'topics' && <Artifacts meetingId={meetingId} />}</Panel>;
}

function Artifacts({ meetingId }: { meetingId: string }) {
  const query = useResource('analysis.list', { meeting_id: meetingId, task: 'artifacts' });
  return <><hr /><Toolbar><h3>Decisions, risks, milestones & deliverables</h3><CommandButton operation="analysis.generate" payload={{ meeting_id: meetingId, task: 'artifacts', idempotency_key: crypto.randomUUID() }}>Generate outcomes</CommandButton></Toolbar><AsyncView query={query}>{(data) => items(data).map((row) => <CitedResult key={id(row)} result={row} />)}</AsyncView></>;
}

export function ActionsPanel({ meetingId }: { meetingId: string }) {
  const query = useResource('actions.list', { meeting_id: meetingId });
  const [open, setOpen] = useState(false);
  const [undo, setUndo] = useState<RecordData | null>(null);
  return <Panel title="Commitments & actions" actions={<><Modal open={open} onOpenChange={setOpen} trigger={<button>Add action</button>} title="Add a human-reviewed action"><CommandForm operation="actions.create" fixed={{ meeting_id: meetingId }} fields={actionFields} onSuccess={() => setOpen(false)} /></Modal><CommandButton operation="analysis.generate" payload={{ meeting_id: meetingId, task: 'actions', idempotency_key: crypto.randomUUID() }}>Extract actions</CommandButton></>}><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No actions yet">Only explicit commitments or requests should become actions. Unknown owners and dates remain unset.</Empty> : items(data).map((row) => <ActionRow key={id(row)} row={row} />)}</AsyncView><Toolbar><ConfirmCommand trigger="Clear actions" title="Clear this meeting's actions?" message="A supported bulk clear can be undone for ten minutes, unless newer edits conflict. Already sent remote issues are not undone." operation="actions.clear" payload={{ meeting_id: meetingId }} onSuccess={(result) => setUndo(result)} />{undo && <CommandButton operation="undo.apply" payload={{ id: text(undo.undo_id ?? item(undo).id) }}>Undo clear</CommandButton>}</Toolbar></Panel>;
}

export function ActionRow({ row }: { row: RecordData }) {
  const navigate = useNavigate();
  const update = useCommand('actions.update');
  const [dialog, setDialog] = useState<'edit' | 'github' | 'delete' | null>(null);
  const bind = (name: 'edit' | 'github' | 'delete') => ({ open: dialog === name, onOpenChange: (open: boolean) => setDialog(open ? name : null) });
  const description = text(row.description ?? row.title);
  const status = text(row.status, 'open');
  const priority = text(row.priority, 'normal');
  const done = status === 'done';
  const meetingId = text(row.meeting_id);
  return <article className={`${styles.taskRow} ${done ? styles.taskDone : ''}`}>
    <input type="checkbox" className={styles.taskCheck} checked={done} disabled={update.isPending || !nativeAvailable()} aria-label={`Mark "${description}" ${done ? 'not done' : 'done'}`} onChange={() => update.mutate({ id: id(row), expected_revision: revision(row), status: done ? 'open' : 'done' })} />
    <div className={styles.taskBody}>
      <strong>{description}</strong>
      <div className={styles.taskMeta}>
        {!done && status !== 'open' && <Badge>{status.replaceAll('_', ' ')}</Badge>}
        {(priority === 'high' || priority === 'urgent') && <Badge tone="warning">{priority}</Badge>}
        <span>{text(row.assignee ?? row.assignee_text, 'Unassigned')}</span>
        <span>{row.due_date ? `Due ${text(row.due_date)}` : 'No deadline'}</span>
        {array(row.source_refs).map((source, index) => <Citation key={index} source={source} />)}
      </div>
      {Boolean(row.deadline_text) && <small className={styles.taskNote}>Original wording: “{text(row.deadline_text)}”</small>}
      {update.error && <ErrorNotice error={update.error} />}
    </div>
    <ActionMenu label={`More actions for "${description}"`} actions={[
      { label: 'Edit…', onSelect: () => setDialog('edit') },
      { label: 'Review GitHub handoff…', onSelect: () => setDialog('github') },
      ...(meetingId ? [{ label: 'Open meeting', onSelect: () => { void navigate({ to: '/meeting/$meetingId', params: { meetingId } }); } }] : []),
      { label: 'Delete…', danger: true, onSelect: () => setDialog('delete') },
    ]} />
    <EditResource operation="actions.update" row={row} fields={actionFields} label="Edit action" {...bind('edit')} />
    <GitHubHandoff action={row} {...bind('github')} />
    <ConfirmCommand trigger="Delete action" title="Delete action?" message="This removes the local action. It cannot remove an issue or message already sent to an external service." operation="actions.delete" payload={{ id: id(row), expected_revision: revision(row) }} {...bind('delete')} />
  </article>;
}

export function QuestionsPanel({ meetingId }: { meetingId: string }) {
  const questions = useResource('questions.list', { meeting_id: meetingId });
  const cues = useResource('analysis.list', { meeting_id: meetingId, task: 'cues' });
  return <Panel title="Cues & questions" actions={<CommandButton operation="analysis.generate" payload={{ meeting_id: meetingId, task: 'cues', idempotency_key: crypto.randomUUID() }}>Generate grounded cues</CommandButton>}><AsyncView query={cues}>{(data) => items(data).map((row) => <CitedResult key={id(row)} result={row} />)}</AsyncView><h3>Questions from the conversation</h3><AsyncView query={questions}>{(data) => items(data).length === 0 ? <Empty title="No open questions">Confirmation tags are not automatically treated as questions. Inferred answers require evidence.</Empty> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><p>{text(row.text ?? row.question)}</p><Badge>{text(row.status, 'open')}</Badge>{array(row.source_refs).map((source, index) => <Citation key={index} source={source} />)}</div><Toolbar><CommandButton operation="questions.update" payload={{ id: id(row), expected_revision: revision(row), status: 'answered' }}>Mark answered</CommandButton><CommandButton operation="questions.update" payload={{ id: id(row), expected_revision: revision(row), status: 'dismissed' }}>Dismiss</CommandButton><CommandButton operation="questions.update" payload={{ id: id(row), expected_revision: revision(row), status: 'open' }}>Reopen</CommandButton></Toolbar></article>)}</AsyncView></Panel>;
}

export function ImportantPanel({ meetingId }: { meetingId: string }) {
  const query = useResource('moments.list', { meeting_id: meetingId });
  return <Panel title="Important moments"><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No moments marked">A human note and minutes inclusion can be edited without altering the quoted evidence.</Empty> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><blockquote>{text(row.source_quote ?? row.quote)}</blockquote><p>{text(row.note)}</p><Toolbar><Badge>{row.include_in_minutes === false ? 'Excluded from minutes' : 'Included verbatim in minutes'}</Badge><Citation source={{ ...row, meeting_id: meetingId }} /></Toolbar></div><Toolbar><EditResource operation="moments.update" row={row} label="Edit moment" fields={[{ name: 'note', label: 'Human note', type: 'textarea' }, { name: 'include_in_minutes', label: 'Include in minutes', type: 'checkbox' }, { name: 'status', label: 'Follow-up status', type: 'select', options: ['open', 'done', 'dismissed'].map((value) => ({ value, label: value })) }, { name: 'priority', label: 'Priority', type: 'select', options: ['low', 'normal', 'high'].map((value) => ({ value, label: value })) }, { name: 'assignee', label: 'Assignee' }]} /><ConfirmCommand trigger="Delete moment" title="Delete important moment?" message="The transcript remains unchanged." operation="moments.delete" payload={{ id: id(row), expected_revision: revision(row) }} /></Toolbar></article>)}</AsyncView></Panel>;
}

export function GitHubHandoff({ action, open: controlledOpen, onOpenChange }: { action: RecordData; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const { open, setOpen, controlled } = useOpenState(controlledOpen, onOpenChange);
  const [connectionId, setConnectionId] = useState('');
  const [prepared, setPrepared] = useState<RecordData | null>(null);
  const [externalError, setExternalError] = useState<unknown>();
  const connections = useResource('github.connection.list', {}, open);
  const repositories = useResource('github.repositories', { connection_id: connectionId }, open && !!connectionId);
  const reviewed = object(prepared?.request);
  const requestFields: Field[] = [
    { name: 'repository', label: 'Destination repository', type: 'select', required: true, options: [{ value: '', label: 'Choose an installed repository' }, ...items(repositories.data).map((row) => ({ value: text(row.full_name ?? row.id), label: text(row.full_name ?? row.name) }))] },
    { name: 'title', label: 'Issue title', value: text(action.description ?? action.title), required: true },
    { name: 'body', label: 'Reviewed issue body', type: 'textarea', value: text(action.description) },
    { name: 'labels', label: 'Labels (JSON array)', type: 'json', value: [] },
    { name: 'assignees', label: 'GitHub assignees (JSON array)', type: 'json', value: [] },
    { name: 'create_distinct', label: 'Explicitly create another distinct issue if this action already has one', type: 'checkbox', value: false },
  ];
  const acceptPrepared = (result: RecordData): void => setPrepared(item(result));
  const sendPayload = { id: prepared ? id(prepared) : '', request_hash: prepared?.request_hash ?? '', reviewed: true };
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>Review GitHub handoff…</button>} title="Review GitHub issue" description="Nothing is sent until you review the exact repository, title, body, labels and assignees, then explicitly confirm. Local deep links work only on this installation.">
    {!prepared ? <>
      <AsyncView query={connections}>{(data) => <FieldInput field={{ name: 'connection_id', label: 'Authorized GitHub connection', type: 'select', options: [{ value: '', label: 'Choose connection' }, ...items(data).map((row) => ({ value: id(row), label: text(row.api_origin, id(row)) }))] }} value={connectionId} onChange={(value) => setConnectionId(text(value))} />}</AsyncView>
      {connectionId ? <AsyncView query={repositories}>{() => <CommandForm operation="github.issue.prepare" fixed={{ connection_id: connectionId, action_id: id(action), meeting_id: action.meeting_id ?? '' }} fields={requestFields} submit="Prepare exact request" onSuccess={acceptPrepared} />}</AsyncView> : <Empty title="Select a configured GitHub connection">Connect and select repository scope in Settings first.</Empty>}
    </> : <>
      <Notice><strong>Destination: {text(prepared.repository)}</strong><h3>{text(reviewed.title)}</h3><p className={styles.preWrap}>{text(reviewed.body)}</p></Notice>
      <Details value={prepared} title="Exact labels, assignees, request hash and operation state" />
      {prepared.state === 'unknown' && <Notice danger>The provider may already have created this issue. Reconcile and review indexing delay/pagination before authorizing any duplicate-risk send.</Notice>}
      <Toolbar>
        <CommandButton operation="github.issue.send" payload={sendPayload} onSuccess={acceptPrepared} disabled={prepared.state === 'unknown' || prepared.state === 'created' || prepared.state === 'sending'}>Confirm and send exact issue</CommandButton>
        <CommandButton operation="github.issue.reconcile" payload={{ id: id(prepared) }} onSuccess={acceptPrepared}>Reconcile uncertain send</CommandButton>
        <CommandButton operation="github.issue.refresh" payload={{ id: id(prepared) }} onSuccess={acceptPrepared}>Refresh remote status</CommandButton>
        {Boolean(prepared.remote_url) && <button onClick={() => { void nativeRequest('external_open', { url: text(prepared.remote_url) }).catch(setExternalError); }}>Open created issue</button>}
        <button onClick={() => setPrepared(null)}>Review a distinct new request</button>
      </Toolbar>
      {prepared.state === 'unknown' && <Modal trigger={<button>Authorize duplicate risk…</button>} title="Explicit duplicate-risk authorization" description="A timeout can still mean an issue exists. Reconciliation can be delayed by provider indexing."><CommandForm operation="github.issue.send" fixed={{ ...sendPayload, authorize_duplicate_risk: true }} fields={[]} submit="Send with acknowledged duplicate risk" confirm="I checked the selected repository and understand this can create a duplicate issue." onSuccess={acceptPrepared} /></Modal>}
    </>}
    {Boolean(externalError) && <ErrorNotice error={externalError} />}
  </Modal>;
}
