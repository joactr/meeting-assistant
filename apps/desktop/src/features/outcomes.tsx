import { useState, type ReactNode } from 'react';
import { Link, useNavigate } from '@tanstack/react-router';
import { useCommand, useResource } from '../lib/queries';
import { useInterface } from '../lib/ui-store';
import { nativeAvailable, nativeRequest } from '../lib/bridge';
import { array, clock, date, humanize, id, item, items, object, revision, text, type RecordData } from '../lib/data';
import { ActionMenu } from '../components/controls';
import { AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, Panel, Toolbar, useOpenState, type Field } from '../components/ui';
import { SetupLink } from './providers';
import styles from './workspace.module.css';

export const actionFields: Field[] = [
  { name: 'description', label: 'Action', required: true },
  { name: 'assignee_text', label: 'Assignee' },
  { name: 'due_date', label: 'Due date', type: 'date' },
  { name: 'priority', label: 'Priority', type: 'select', value: 'normal', options: ['low', 'normal', 'high', 'urgent'].map((value) => ({ value, label: humanize(value) })) },
  { name: 'status', label: 'Status', type: 'select', value: 'open', options: ['open', 'in_progress', 'done', 'dismissed'].map((value) => ({ value, label: humanize(value) })) },
];

export const NO_TRANSCRIPT = 'Nothing transcribed yet';

/** Latest state of one AI task from analysis.status; the finalize stages report their soft failures there too. */
export function useTask(meetingId: string, task: string) {
  const status = useResource('analysis.status', { meeting_id: meetingId }, true, (data) => items(data).some(running));
  const row = items(status.data).find((entry) => entry.task === task);
  return { row, generating: !!row && running(row), failed: row?.status === 'failed' };
}

/** A rejected key looks the same everywhere: error code `auth`, or HTTP 401/403 in the message. */
export function AuthHint({ code, message }: { code?: unknown; message?: unknown }) {
  return /^(auth|unauthori[sz]ed)/i.test(text(code)) || /\b40[13]\b/.test(text(message)) ? <SetupLink>Check the API key in Settings</SetupLink> : null;
}

/** Why a task failed, with the way to fix a rejected key. The retry is the task's own button. */
export function FailureNote({ error, code }: { error: unknown; code?: unknown }) {
  return <Toolbar><small role="alert">Failed: {text(error, 'unknown error')}</small><AuthHint code={code} message={error} /></Toolbar>;
}

/** A draft meeting has no transcript, so there is nothing to generate from. A failed task offers Retry instead of Generate. */
function GenerateButton({ meetingId, task, children }: { meetingId: string; task: string; children: ReactNode }) {
  const meeting = useResource('meetings.get', { id: meetingId });
  const { failed } = useTask(meetingId, task);
  const empty = text(item(meeting.data).state) === 'draft';
  return <span title={empty ? NO_TRANSCRIPT : undefined}><CommandButton operation="analysis.generate" payload={{ meeting_id: meetingId, task }} disabled={empty}>{failed ? 'Retry' : children}</CommandButton></span>;
}

export function Citation({ source, number: citationNumber }: { source: RecordData; number?: number }) {
  const select = useInterface((state) => state.selectUtterance);
  const activeMeeting = useInterface((state) => state.activeMeeting);
  const meetingId = text(source.meeting_id) || activeMeeting || '';
  const utteranceId = text(source.utterance_id ?? source.source_utterance_id ?? source.start_utterance_id ?? source.source_id);
  if (source.type === 'human_note' || source.provenance === 'Human note') return <Badge>Human note</Badge>;
  if (!meetingId || !utteranceId) return <Badge>{citationNumber ? `[${citationNumber}] ` : ''}Source unavailable</Badge>;
  return <Link className={styles.citation} to="/meeting/$meetingId" params={{ meetingId }} onClick={() => select(utteranceId)} title={text(source.quote ?? source.quote_snapshot ?? source.excerpt, 'Open source')}><span>{citationNumber ? `[${citationNumber}] ` : ''}{text(source.meeting_title, 'Source')} at {clock(source.start_ms)}</span>{(source.stale === true || source.stale === 1) && <Badge tone="warning">Out of date</Badge>}</Link>;
}

export function CitedResult({ result }: { result: RecordData }) {
  const data = object(result.result ?? result.content ?? result.payload ?? result);
  const sections = array(data.sections);
  const groups = ['cues', 'questions', 'topics', 'decisions', 'risks', 'milestones', 'deliverables', 'follow_ups', 'candidates', 'actions'].filter((key) => array(data[key]).length > 0);
  const body = text(data.answer ?? data.summary ?? data.text ?? data.body ?? data.markdown);
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const meta = result.state === 'superseded' || Boolean(result.stale) || Boolean(result.updated_at);
  return <div className={styles.result}>
    {meta && <div className={styles.resultMeta}>
      {result.state === 'superseded' && <Badge>Older version</Badge>}
      {Boolean(result.stale) && <Badge tone="warning">Out of date</Badge>}
      {Boolean(result.updated_at) && <span>Generated {date(result.updated_at)}</span>}
    </div>}
    {sections.map((section, index) => {
      const source = text(section.source ?? section.provenance);
      const refs = array(section.source_refs ?? section.citations);
      return <section key={id(section) || index}>
        <h3>{text(section.heading ?? section.title)}</h3>
        <p className={styles.preWrap}>{text(section.body ?? section.text)}</p>
        {Boolean(section.human_note) && <p className={styles.preWrap}>Human note: {text(section.human_note)}</p>}
        {(refs.length > 0 || (source && source !== 'transcript')) && <Toolbar>
          {source && source !== 'transcript' && <Badge>{humanize(source)}</Badge>}
          {refs.map((ref, citationIndex) => <Citation key={id(ref) || citationIndex} source={ref} number={citationIndex + 1} />)}
        </Toolbar>}
      </section>;
    })}
    {!sections.length && body && <p className={styles.preWrap}>{body}</p>}
    {groups.map((key) => <section key={key}>
      {groups.length > 1 && <h3>{humanize(key)}</h3>}
      {array(data[key]).map((entry, index) => <article key={id(entry) || index} className={`${styles.entry} ${entry.parent_key ? styles.child : ''}`}>
        <strong>{text(entry.title ?? entry.description ?? entry.text ?? entry.question)}</strong>
        <div className={styles.entryMeta}>
          {Boolean(entry.kind) && <span>{humanize(entry.kind)}</span>}
          {Boolean(entry.sentiment) && <span>{humanize(entry.sentiment)}</span>}
          {entry.start_ms !== undefined && <span>{clock(entry.start_ms)} to {clock(entry.end_ms)}</span>}
          {array(entry.source_refs).map((source, citationIndex) => <Citation key={citationIndex} source={source} number={citationIndex + 1} />)}
        </div>
      </article>)}
    </section>)}
    {array(data.citations).length > 0 && <Toolbar>{array(data.citations).map((source, index) => <Citation key={id(source) || index} source={source} number={index + 1} />)}</Toolbar>}
    {warnings.length > 0 && <details className={styles.disclosure}><summary>Caveats ({warnings.length})</summary><ul>{warnings.map((warning, index) => <li key={index}>{typeof warning === 'string' ? warning : JSON.stringify(warning)}</li>)}</ul></details>}
    {!sections.length && !body && !groups.length && (Array.isArray(data.sections) ? <Empty title="Nothing in this version" /> : <Details title="Details" value={data} />)}
  </div>;
}

const running = (row: RecordData) => row.status === 'queued' || row.status === 'running';

/** Per-task job state under the panel title; nothing once the task is done. */
function TaskStatus({ meetingId, task }: { meetingId: string; task: string }) {
  const { row, generating, failed } = useTask(meetingId, task);
  if (generating) return <small role="status">Generating…</small>;
  return failed ? <FailureNote error={row?.error} code={row?.error_code} /> : null;
}

export function AnalysisPanel({ meetingId, task = 'summary' }: { meetingId: string; task?: string }) {
  const query = useResource('analysis.list', { meeting_id: meetingId, task });
  const { generating } = useTask(meetingId, task);
  return <><Panel title={task === 'summary' ? 'Summary' : 'Topics'} actions={<GenerateButton meetingId={meetingId} task={task}>{items(query.data).length > 0 ? 'Regenerate' : 'Generate'} {task}</GenerateButton>}><TaskStatus meetingId={meetingId} task={task} /><AsyncView query={query}>{(data) => items(data).length === 0 ? (generating ? null : <Empty title={`No ${task} yet`} />) : items(data).map((result) => <CitedResult key={id(result)} result={result} />)}</AsyncView></Panel>{task === 'topics' && <Artifacts meetingId={meetingId} />}</>;
}

function Artifacts({ meetingId }: { meetingId: string }) {
  const query = useResource('analysis.list', { meeting_id: meetingId, task: 'artifacts' });
  return <Panel title="Decisions and risks" actions={<GenerateButton meetingId={meetingId} task="artifacts">{items(query.data).length > 0 ? 'Regenerate' : 'Generate'}</GenerateButton>}><TaskStatus meetingId={meetingId} task="artifacts" /><AsyncView query={query} isEmpty={items(query.data).length === 0} empty={<Empty title="No decisions or risks yet" />}>{(data) => items(data).map((row) => <CitedResult key={id(row)} result={row} />)}</AsyncView></Panel>;
}

export function ActionsPanel({ meetingId }: { meetingId: string }) {
  const query = useResource('actions.list', { meeting_id: meetingId });
  const [open, setOpen] = useState(false);
  const [undo, setUndo] = useState<RecordData | null>(null);
  return <Panel title="Actions" actions={<><Modal open={open} onOpenChange={setOpen} trigger={<button>Add action</button>} title="Add action"><CommandForm operation="actions.create" fixed={{ meeting_id: meetingId }} fields={actionFields} submit="Add action" onSuccess={() => setOpen(false)} /></Modal><GenerateButton meetingId={meetingId} task="actions">Extract actions</GenerateButton></>}><TaskStatus meetingId={meetingId} task="actions" /><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No actions" /> : items(data).map((row) => <ActionRow key={id(row)} row={row} />)}</AsyncView>{(items(query.data).length > 0 || undo) && <Toolbar><ConfirmCommand trigger="Clear actions" title="Clear actions?" message="Removes all actions for this meeting. You can undo for ten minutes." operation="actions.clear" payload={{ meeting_id: meetingId }} onSuccess={(result) => setUndo(result)} />{undo && <CommandButton operation="undo.apply" payload={{ id: text(undo.undo_id ?? item(undo).id) }}>Undo clear</CommandButton>}</Toolbar>}</Panel>;
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
        {!done && status !== 'open' && <Badge>{humanize(status)}</Badge>}
        {(priority === 'high' || priority === 'urgent') && <Badge tone="warning">{humanize(priority)}</Badge>}
        <span>{text(row.assignee ?? row.assignee_text, 'Unassigned')}</span>
        <span>{row.due_date ? `Due ${text(row.due_date)}` : 'No deadline'}</span>
        {array(row.source_refs).map((source, index) => <Citation key={index} source={source} />)}
      </div>
      {Boolean(row.deadline_text) && <small className={styles.taskNote}>“{text(row.deadline_text)}”</small>}
      {update.error && <ErrorNotice error={update.error} />}
    </div>
    <ActionMenu label={`More actions for "${description}"`} actions={[
      { label: 'Edit…', onSelect: () => setDialog('edit') },
      { label: 'GitHub issue…', onSelect: () => setDialog('github') },
      ...(meetingId ? [{ label: 'Open meeting', onSelect: () => { void navigate({ to: '/meeting/$meetingId', params: { meetingId } }); } }] : []),
      { label: 'Delete…', danger: true, onSelect: () => setDialog('delete') },
    ]} />
    <EditResource operation="actions.update" row={row} fields={actionFields} label="Edit action" {...bind('edit')} />
    <GitHubHandoff action={row} {...bind('github')} />
    <ConfirmCommand trigger="Delete action" title="Delete action?" message="Removes the action here only; issues already sent stay." operation="actions.delete" payload={{ id: id(row), expected_revision: revision(row) }} {...bind('delete')} />
  </article>;
}

export function QuestionsPanel({ meetingId }: { meetingId: string }) {
  const questions = useResource('questions.list', { meeting_id: meetingId });
  const cues = useResource('analysis.list', { meeting_id: meetingId, task: 'cues' });
  return <Panel title="Cues & questions" actions={<GenerateButton meetingId={meetingId} task="cues">{items(cues.data).length > 0 ? 'Regenerate cues' : 'Generate cues'}</GenerateButton>}><TaskStatus meetingId={meetingId} task="cues" /><AsyncView query={cues}>{(data) => items(data).map((row) => <CitedResult key={id(row)} result={row} />)}</AsyncView><h3>Questions</h3><AsyncView query={questions}>{(data) => items(data).length === 0 ? <Empty title="No open questions" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><p>{text(row.text ?? row.question)}</p><Badge>{humanize(row.status ?? 'open')}</Badge>{array(row.source_refs).map((source, index) => <Citation key={index} source={source} />)}</div><Toolbar>{text(row.status, 'open') === 'open' ? <><CommandButton operation="questions.update" payload={{ id: id(row), expected_revision: revision(row), status: 'answered' }}>Mark answered</CommandButton><CommandButton operation="questions.update" payload={{ id: id(row), expected_revision: revision(row), status: 'dismissed' }}>Dismiss</CommandButton></> : <CommandButton operation="questions.update" payload={{ id: id(row), expected_revision: revision(row), status: 'open' }}>Reopen</CommandButton>}</Toolbar></article>)}</AsyncView></Panel>;
}

export function ImportantPanel({ meetingId }: { meetingId: string }) {
  const query = useResource('moments.list', { meeting_id: meetingId });
  return <Panel title="Important moments"><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No moments marked" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><blockquote>{text(row.source_quote ?? row.quote)}</blockquote><p>{text(row.note)}</p><Toolbar><Badge>{row.include_in_minutes === false ? 'Not in minutes' : 'In minutes'}</Badge><Citation source={{ ...row, meeting_id: meetingId }} /></Toolbar></div><Toolbar><EditResource operation="moments.update" row={row} label="Edit moment" fields={[{ name: 'note', label: 'Note', type: 'textarea' }, { name: 'include_in_minutes', label: 'Include in minutes', type: 'checkbox' }, { name: 'status', label: 'Status', type: 'select', options: ['open', 'done', 'dismissed'].map((value) => ({ value, label: humanize(value) })) }, { name: 'priority', label: 'Priority', type: 'select', options: ['low', 'normal', 'high', 'urgent'].map((value) => ({ value, label: humanize(value) })) }, { name: 'assignee', label: 'Assignee' }]} /><ConfirmCommand trigger="Delete moment" title="Delete moment?" message="The transcript is not changed." operation="moments.delete" payload={{ id: id(row), expected_revision: revision(row) }} /></Toolbar></article>)}</AsyncView></Panel>;
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
    { name: 'repository', label: 'Repository', type: 'select', required: true, options: [{ value: '', label: 'Choose repository' }, ...items(repositories.data).map((row) => ({ value: text(row.full_name ?? row.id), label: text(row.full_name ?? row.name) }))] },
    { name: 'title', label: 'Issue title', value: text(action.description ?? action.title), required: true },
    { name: 'body', label: 'Issue body', type: 'textarea', value: text(action.description) },
    { name: 'labels', label: 'Labels (JSON array)', type: 'json', value: [] },
    { name: 'assignees', label: 'GitHub assignees (JSON array)', type: 'json', value: [] },
    { name: 'create_distinct', label: 'Create another issue even if one exists', type: 'checkbox', value: false },
  ];
  const acceptPrepared = (result: RecordData): void => setPrepared(item(result));
  const sendPayload = { id: prepared ? id(prepared) : '', request_hash: prepared?.request_hash ?? '', reviewed: true };
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>GitHub issue…</button>} title="GitHub issue">
    {!prepared ? <>
      <AsyncView query={connections}>{(data) => <FieldInput field={{ name: 'connection_id', label: 'Connection', type: 'select', options: [{ value: '', label: 'Choose connection' }, ...items(data).map((row) => ({ value: id(row), label: text(row.api_origin, id(row)) }))] }} value={connectionId} onChange={(value) => setConnectionId(text(value))} />}</AsyncView>
      {connectionId ? <AsyncView query={repositories}>{() => <CommandForm operation="github.issue.prepare" fixed={{ connection_id: connectionId, action_id: id(action), meeting_id: action.meeting_id ?? '' }} fields={requestFields} submit="Preview" onSuccess={acceptPrepared} />}</AsyncView> : <Empty title="Choose a connection" />}
    </> : <>
      <Notice><strong>Destination: {text(prepared.repository)}</strong><h3>{text(reviewed.title)}</h3><p className={styles.preWrap}>{text(reviewed.body)}</p></Notice>
      <Details value={prepared} title="Details" />
      {prepared.state === 'unknown' && <Notice danger>This issue may already exist. Reconcile before sending again.</Notice>}
      <Toolbar>
        <CommandButton operation="github.issue.send" payload={sendPayload} onSuccess={acceptPrepared} disabled={prepared.state === 'unknown' || prepared.state === 'created' || prepared.state === 'sending'}>Send issue</CommandButton>
        <CommandButton operation="github.issue.reconcile" payload={{ id: id(prepared) }} onSuccess={acceptPrepared}>Reconcile</CommandButton>
        <CommandButton operation="github.issue.refresh" payload={{ id: id(prepared) }} onSuccess={acceptPrepared}>Refresh status</CommandButton>
        {Boolean(prepared.remote_url) && <button onClick={() => { void nativeRequest('external_open', { url: text(prepared.remote_url) }).catch(setExternalError); }}>Open created issue</button>}
        <button onClick={() => setPrepared(null)}>New request</button>
      </Toolbar>
      {prepared.state === 'unknown' && <Modal trigger={<button>Send anyway…</button>} title="Send anyway?"><CommandForm operation="github.issue.send" fixed={{ ...sendPayload, authorize_duplicate_risk: true }} fields={[]} submit="Send anyway" confirm="I checked the repository and accept a possible duplicate." onSuccess={acceptPrepared} /></Modal>}
    </>}
    {Boolean(externalError) && <ErrorNotice error={externalError} />}
  </Modal>;
}
