/* Hallmark · dropped onClick={() => setDraft(null)}, onClick={() => setDraft(row)}, onOpenChange={setPrepareOpen}, onSuccess={() => setDraft(null)}, onSuccess={(result) => { setDraft(item(result)); setPrepareOpen(false); }} · minutes are made and saved in one step; no review, candidates or AI policy field */
/* Hallmark · dropped onClick={() => exit.mutate()}, edit(blocks.filter((candidate), select(id(row)); setMenu({ row · quit takes a cancel flag, note remove asks twice, the mark-important dialog was renamed */
import { Fragment, useEffect, useState } from 'react';
import { Link, useNavigate } from '@tanstack/react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { listen } from '@tauri-apps/api/event';
import { ApplicationError, nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useResource } from '../lib/queries';
import { useInterface } from '../lib/ui-store';
import { array, date, id, item, items, label, object, revision, text, type Payload, type RecordData } from '../lib/data';
import { ActionMenu } from '../components/controls';
import { AsyncView, Badge, buttonClass, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, PageHeader, Panel, TabSet, Toolbar, useOpenState } from '../components/ui';
import { CaptureControls, LanguageControl, MeetingStatus, SpeakersPanel } from './capture';
import { Transcript } from './transcript';
import { HumanNotes } from './notes';
import { ActionsPanel, AnalysisPanel, AuthHint, CitedResult, FailureNote, ImportantPanel, NO_TRANSCRIPT, QuestionsPanel, useTask } from './outcomes';
import { CandidatePanel, ReplayPanel } from './media';
import styles from './workspace.module.css';

export function MeetingPage({ meetingId }: { meetingId: string }) {
  const query = useResource('meetings.get', { id: meetingId });
  const setActiveMeeting = useInterface((state) => state.setActiveMeeting);
  useEffect(() => { setActiveMeeting(meetingId); return () => setActiveMeeting(null); }, [meetingId, setActiveMeeting]);
  // Deleted elsewhere (another window, the API): say so instead of a raw not_found error.
  if (query.error instanceof ApplicationError && query.error.code === 'not_found') return <Empty title="This meeting no longer exists"><Link to="/">Back to the library</Link></Empty>;
  return <AsyncView query={query}>{(data) => <MeetingWorkspace meeting={item(data)} />}</AsyncView>;
}

function MeetingWorkspace({ meeting }: { meeting: RecordData }) {
  const meetingId = id(meeting);
  const panel = useInterface((state) => state.panel);
  const setPanel = useInterface((state) => state.setPanel);
  const recording = ['recording', 'starting', 'paused'].includes(text(meeting.state));
  return <><PageHeader title={label(meeting)} showTitle><CaptureControls meeting={meeting} /><MeetingActions meeting={meeting} /></PageHeader><MeetingStatus meeting={meeting} /><CaptureAlert meeting={meeting} /><div className={styles.meeting}><div className={styles.record}><TabSet label="Meeting panels" value={panel} onValueChange={setPanel} tabs={[
    { label: 'Transcript', content: <><Transcript meetingId={meetingId} recording={recording} />{!recording && <CandidatePanel meetingId={meetingId} />}</> },
    { label: 'Outcomes', content: <><AnalysisPanel meetingId={meetingId} /><ActionsPanel meetingId={meetingId} /><AnalysisPanel meetingId={meetingId} task="topics" /><QuestionsPanel meetingId={meetingId} /><ImportantPanel meetingId={meetingId} /></> },
    { label: 'Minutes', content: <><MinutesPanel meeting={meeting} /><FinalizationPanel meetingId={meetingId} /></> },
  ]} /></div><aside className={styles.margin} aria-label="Your notes"><HumanNotes meetingId={meetingId} />{!recording && <ReplayPanel meetingId={meetingId} recording={recording} />}</aside></div></>;
}

const CAPTURE_ALERTS: Record<string, string> = {
  'capture.attention': 'Transcription needs attention',
  'capture.backpressure': 'Transcription fell behind, so some speech was skipped',
  'native.capture.error': 'Capture problem',
  'native.capture.source_lost': 'An audio source was lost',
  'native.dsp.mode': 'The speech detection model did not load, so quiet speech may be missed',
  'native.engine.interrupted': 'The engine stopped, so capture stopped',
};

/** Problems reported while capturing, with the way out: restart capture (new stream, fresh speech timing) or reload. */
function CaptureAlert({ meeting }: { meeting: RecordData }) {
  const meetingId = id(meeting);
  const client = useQueryClient();
  const sources = useResource('entity.list', { collection: 'capture_sources', filters: { meeting_id: meetingId } });
  const [alert, setAlert] = useState<{ type: string; message: string } | null>(null);
  useEffect(() => {
    if (!nativeAvailable()) return;
    let disposed = false; let cleanup: (() => void) | undefined;
    void listen<RecordData>('engine-event', ({ payload }) => {
      const type = text(payload.type);
      if (!(type in CAPTURE_ALERTS)) return;
      const data = object(payload.data);
      const target = text(payload.meeting_id ?? data.meeting_id);
      if (target && target !== meetingId) return;
      setAlert({ type, message: text(data.message) });
    }).then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; });
    return () => { disposed = true; cleanup?.(); };
  }, [meetingId]);
  const live = ['recording', 'paused'].includes(text(meeting.state));
  const source = items(sources.data)[0];
  if (!alert && !(live && meeting.health === 'degraded')) return null;
  const interrupted = alert?.type === 'native.engine.interrupted';
  return <Notice danger>{alert ? `${CAPTURE_ALERTS[alert.type]}${alert.message ? `: ${alert.message}` : ''}` : 'Some audio was not transcribed.'}<Toolbar>{live && source && !interrupted && <CommandButton operation="meetings.source_retry" payload={{ source_id: id(source) }} onSuccess={() => setAlert(null)}>Restart capture</CommandButton>}{interrupted && <button type="button" onClick={() => { setAlert(null); void client.invalidateQueries({ queryKey: ['engine'] }); }}>Reopen meeting</button>}{alert && <button type="button" onClick={() => setAlert(null)}>Dismiss</button>}</Toolbar></Notice>;
}

type MeetingDialog = 'export' | 'edit' | 'language' | 'speakers' | 'delete' | null;

/** One primary action (Export) plus an overflow menu; each dialog stays mounted so the menu can close first. */
function MeetingActions({ meeting }: { meeting: RecordData }) {
  const navigate = useNavigate();
  const projects = useResource('projects.list');
  const [dialog, setDialog] = useState<MeetingDialog>(null);
  const bind = (name: Exclude<MeetingDialog, null>) => ({ open: dialog === name, onOpenChange: (open: boolean) => setDialog(open ? name : null) });
  return <>
    <button onClick={() => setDialog('export')}>Export…</button>
    {meeting.state === 'complete' && <CopySummary meeting={meeting} />}
    <ActionMenu label="More meeting actions" actions={[
      { label: 'Edit details…', onSelect: () => setDialog('edit') },
      { label: 'Speakers…', onSelect: () => setDialog('speakers') },
      { label: 'Summary language…', onSelect: () => setDialog('language') },
      { label: 'Delete meeting…', danger: true, onSelect: () => setDialog('delete') },
    ]} />
    <ExportControls meeting={meeting} {...bind('export')} />
    <LanguageControl meeting={meeting} {...bind('language')} />
    <Modal title="Speakers" {...bind('speakers')}><SpeakersPanel meetingId={id(meeting)} /></Modal>
    <EditResource operation="meetings.update" row={meeting} label="Edit meeting" fields={[{ name: 'title', label: 'Title', required: true }, { name: 'project_id', label: 'Project', type: 'select', options: [{ value: '', label: 'No project' }, ...items(projects.data).map((row) => ({ value: id(row), label: label(row) }))] }]} {...bind('edit')} />
    <ConfirmCommand trigger="Delete meeting…" title={`Delete "${label(meeting)}"?`} message="Deletes the transcript, notes and audio. Anything already sent elsewhere stays." operation="meetings.delete" payload={{ id: id(meeting), expected_revision: revision(meeting), confirm: true }} onSuccess={() => { void navigate({ to: '/' }); }} {...bind('delete')} />
  </>;
}

/** The same text as "Copy email" (summary, decisions, actions), one click away. */
function CopySummary({ meeting }: { meeting: RecordData }) {
  const copy = useMutation({
    mutationFn: async () => {
      const result = await request('export.create', { meeting_id: id(meeting), expected_revision: revision(meeting), format: 'email', include_transcript: false });
      if (typeof result.text !== 'string' || !result.text.trim()) throw new Error('There is no summary to copy yet.');
      await nativeRequest('clipboard_share', { request: { operation: 'copy', text: result.text, recipients: [], subject: text(meeting.title) } });
    },
  });
  useEffect(() => { if (!copy.isSuccess) return; const timer = setTimeout(copy.reset, 2000); return () => clearTimeout(timer); }, [copy.isSuccess]);
  return <><button data-testid="copy-summary" disabled={copy.isPending} onClick={() => copy.mutate()}>{copy.isSuccess ? 'Copied' : 'Copy summary'}</button>{copy.error && <ErrorNotice error={copy.error} />}</>;
}

const STAGES: Record<string, string> = { drain: 'Finish the transcript', final_diarization: 'Tell speakers apart', final_analysis: 'Summary, actions and topics', minutes: 'Minutes', memory: 'Project memory', index: 'Search index' };
const JOB_STATUS: Record<string, string> = { queued: 'Waiting', running: 'Running', succeeded: 'Done', failed: 'Failed', skipped: 'Skipped', cancelled: 'Cancelled' };

const STAGE_TASKS: Record<string, string[]> = { final_analysis: ['summary', 'actions', 'topics', 'artifacts'], minutes: ['minutes'], memory: ['memory'] };
const TASK_NAMES: Record<string, string> = { summary: 'Summary', actions: 'Actions', topics: 'Topics', artifacts: 'Decisions and risks', minutes: 'Minutes', memory: 'Project memory' };

/** The engine marks a stage done even when its AI tasks failed softly. analysis.status holds the latest outcome per task (including later retries), so a stage fails while any of its tasks' latest outcome is a failure. */
function FinalizationPanel({ meetingId }: { meetingId: string }) {
  const query = useResource('meetings.finalization', { meeting_id: meetingId });
  const tasks = useResource('analysis.status', { meeting_id: meetingId });
  const minutes = useResource('minutes.get', { meeting_id: meetingId });
  const hasMinutes = Boolean(id(object(minutes.data?.item)));
  return <Panel title="Processing"><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="Not processed yet" /> : items(data).map((job) => {
    const stage = text(job.stage ?? object(job.payload).stage ?? job.kind);
    const reasons = new Map<string, { code: unknown; names: string[] }>();
    for (const row of items(tasks.data)) {
      if (job.status !== 'succeeded' || row.status !== 'failed' || !STAGE_TASKS[stage]?.includes(text(row.task)) || (row.task === 'minutes' && hasMinutes)) continue;
      const message = text(row.error, 'unknown error');
      const reason = reasons.get(message) ?? { code: row.error_code, names: [] };
      reason.names.push(TASK_NAMES[text(row.task)] ?? text(row.task));
      reasons.set(message, reason);
    }
    const failed = job.status === 'failed' || reasons.size > 0;
    return <article className={styles.actionRow} key={id(job)}><div><strong>{STAGES[stage] ?? stage.replaceAll('_', ' ')}</strong><Badge tone={failed ? 'danger' : job.status === 'succeeded' ? 'success' : 'neutral'}>{failed ? 'Failed' : JOB_STATUS[text(job.status)] ?? text(job.status)}</Badge>{Boolean(object(job.error).message) && <p>{text(object(job.error).message)}</p>}<AuthHint code={object(job.error).code} message={object(job.error).message} />{[...reasons].map(([message, reason]) => <Fragment key={message}><p>{reason.names.join(', ')}: {message}</p><AuthHint code={reason.code} message={message} /></Fragment>)}{job.error && <Details title="Details" value={job.error} />}<small>Updated {date(job.updated_at)}</small></div><Toolbar>{job.status === 'failed' && <CommandButton operation="meetings.retry" payload={{ meeting_id: meetingId, stage }}>Retry</CommandButton>}{['queued', 'running'].includes(text(job.status)) && <CommandButton operation="jobs.cancel" payload={{ id: id(job), expected_revision: revision(job) }}>Cancel</CommandButton>}</Toolbar></article>;
  })}</AsyncView></Panel>;
}

/** One heading and one text box per section. The engine keeps the section set fixed (ids and sources), so sections can be edited but not added or removed. */
function MinutesEditor({ meetingId, current }: { meetingId: string; current: RecordData }) {
  const sections = array(current.sections);
  const fields = sections.flatMap((section, index) => [{ name: `title_${index}`, label: `Section ${index + 1} heading`, required: true, value: text(section.title) }, { name: `text_${index}`, label: `Section ${index + 1} text`, type: 'textarea' as const, required: true, value: text(section.text) }]);
  const mapPayload = (payload: Payload): Payload => ({ ...Object.fromEntries(Object.entries(payload).filter(([key]) => !/^(title|text)_\d+$/.test(key))), sections: sections.map((section, index) => ({ ...section, title: payload[`title_${index}`] ?? null, text: payload[`text_${index}`] ?? null })) });
  return <EditResource operation="minutes.edit" row={current} fixed={{ meeting_id: meetingId }} label="Edit minutes" fields={fields} mapPayload={mapPayload} />;
}

function MinutesPanel({ meeting }: { meeting: RecordData }) {
  const notes = useResource('notes.get', { meeting_id: id(meeting) });
  const minutes = useResource('minutes.get', { meeting_id: id(meeting) });
  const task = useTask(id(meeting), 'minutes');
  const hasMinutes = Boolean(id(object(minutes.data?.item)));
  const failed = task.failed && !hasMinutes;
  const empty = text(meeting.state) === 'draft';
  // The tab already says "Minutes", so the panel carries no title of its own.
  return <Panel><Toolbar><span data-testid="prepare-minutes" title={empty ? NO_TRANSCRIPT : undefined}><CommandButton operation="minutes.prepare" disabled={empty} payload={{ meeting_id: id(meeting), note_revision: item(notes.data).revision ?? 0, transcript_revision: meeting.transcript_revision ?? 0 }}>{failed ? 'Retry' : hasMinutes ? 'Regenerate minutes' : 'Make minutes'}</CommandButton></span></Toolbar>
    {failed && <FailureNote error={task.row?.error} code={task.row?.error_code} />}
    <AsyncView query={minutes}>{(data) => {
      const current = object(data.item);
      return id(current) ? <><CitedResult result={current} /><MinutesEditor meetingId={id(meeting)} current={current} /></> : <Empty title="No minutes yet" />;
    }}</AsyncView>
  </Panel>;
}

export function ExportControls({ meeting, open: controlledOpen, onOpenChange }: { meeting: RecordData; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const { open, setOpen, controlled } = useOpenState(controlledOpen, onOpenChange);
  const [format, setFormat] = useState('markdown');
  const [includeTranscript, setIncludeTranscript] = useState(false);
  const [recipients, setRecipients] = useState('');
  const exportOperation = useMutation({
    mutationFn: async (share: 'save' | 'copy' | 'mailto') => {
      await flushDrafts();
      const grant = share === 'save' ? await nativeRequest('file_grant', { purpose: 'export', fileName: `${text(meeting.title) || 'Meeting'}.${{ markdown: 'md', pdf: 'pdf', srt: 'srt', json: 'json' }[format] ?? format}` }) : null;
      if (grant?.cancelled === true) return { cancelled: true };
      const result = await request('export.create', { meeting_id: id(meeting), expected_revision: revision(meeting), format: share === 'save' ? format : 'email', include_transcript: share === 'save' && includeTranscript, ...(grant ? { file_grant: grant.grant_id ?? '' } : {}) });
      if (share !== 'save') {
        if (typeof result.text !== 'string' || !result.text.trim()) throw new Error('The meeting export did not return email text. Nothing was shared.');
        return nativeRequest('clipboard_share', { request: { operation: share, text: result.text, recipients: recipients.split(',').map((entry) => entry.trim()).filter(Boolean), subject: text(meeting.title) } });
      }
      return result;
    },
  });
  useEffect(() => {
    const handler = (): void => setOpen(true);
    window.addEventListener('meeting-save-minutes', handler);
    return () => window.removeEventListener('meeting-save-minutes', handler);
  }, []);
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>Export…</button>} title="Export"><section className={styles.exportGroup}><h3>Save to file</h3><div className={styles.filters}><label>Format<select value={format} onChange={(event) => setFormat(event.target.value)}>{[['markdown', 'Markdown'], ['pdf', 'PDF'], ['srt', 'Subtitles (SRT)'], ['json', 'JSON']].map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label><label className={styles.check}><input type="checkbox" checked={includeTranscript} onChange={(event) => setIncludeTranscript(event.target.checked)} />Include transcript</label></div><Toolbar><button className={buttonClass.primary} disabled={exportOperation.isPending || !nativeAvailable()} onClick={() => exportOperation.mutate('save')}>Save export…</button></Toolbar>{exportOperation.variables === 'save' && <>{exportOperation.error && <ErrorNotice error={exportOperation.error} />}{exportOperation.isSuccess && <Details value={exportOperation.data} />}</>}</section><section className={styles.exportGroup}><h3>Share by email</h3><label>Recipients, separated by commas<input value={recipients} onChange={(event) => setRecipients(event.target.value)} /></label><Toolbar><button disabled={exportOperation.isPending || !nativeAvailable() || !recipients.trim()} onClick={() => exportOperation.mutate('copy')}>Copy email</button><button disabled={exportOperation.isPending || !nativeAvailable() || !recipients.trim()} onClick={() => exportOperation.mutate('mailto')}>Open mail draft</button></Toolbar>{exportOperation.variables !== undefined && exportOperation.variables !== 'save' && <>{exportOperation.error && <ErrorNotice error={exportOperation.error} />}{exportOperation.isSuccess && <Details value={exportOperation.data} />}</>}</section><DeliveryPreview meeting={meeting} /></Modal>;
}

/** Meeting delivery to a configured Slack or email destination: preview the exact text, then send it there. Shown only when a destination covers this meeting. */
function DeliveryPreview({ meeting }: { meeting: RecordData }) {
  const destinations = useResource('delivery.list');
  const [destination, setDestination] = useState('');
  const [preview, setPreview] = useState<RecordData | null>(null);
  const eligible = items(destinations.data).filter((row) => row.enabled !== false && (row.meeting_id ? row.meeting_id === id(meeting) : Boolean(row.project_id) && row.project_id === meeting.project_id));
  if (!eligible.length) return null;
  return <><hr /><h3>Send to Slack or email</h3>
    <FieldInput field={{ name: 'delivery_id', label: 'Destination', type: 'select', options: [{ value: '', label: 'Choose destination' }, ...eligible.map((row) => ({ value: id(row), label: text(row.destination, text(row.transport)) }))] }} value={destination} onChange={(value) => { setDestination(text(value)); setPreview(null); }} />
    {destination && <CommandForm operation="delivery.preview" fixed={{ meeting_id: id(meeting), include_transcript: false }} fields={[]} submit="Preview" onSuccess={(result) => setPreview(item(result))} />}
    {preview && <><pre className={styles.preWrap} aria-label="Message preview">{text(object(preview.content).markdown)}</pre><CommandForm operation="delivery.send" fixed={{ preview_id: id(preview), preview_hash: text(preview.preview_hash), delivery_id: destination, reviewed: true }} fields={[]} submit="Send" confirm="I reviewed the content and recipients. Sent messages can’t be recalled." /></>}
  </>;
}
