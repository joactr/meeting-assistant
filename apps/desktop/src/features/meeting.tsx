/* Hallmark · dropped onClick={() => setDraft(null)}, onClick={() => setDraft(row)}, onOpenChange={setPrepareOpen}, onSuccess={() => setDraft(null)}, onSuccess={(result) => { setDraft(item(result)); setPrepareOpen(false); }} · minutes are made and saved in one step; no review, candidates or AI policy field */
import { useEffect, useState } from 'react';
import { useNavigate } from '@tanstack/react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { listen } from '@tauri-apps/api/event';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useResource } from '../lib/queries';
import { useInterface } from '../lib/ui-store';
import { date, id, item, items, label, object, revision, text, type RecordData } from '../lib/data';
import { ActionMenu } from '../components/controls';
import { AsyncView, Badge, buttonClass, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, PageHeader, Panel, TabSet, Toolbar, useOpenState } from '../components/ui';
import { CaptureControls, LanguageControl, MeetingStatus, SpeakersPanel } from './capture';
import { Transcript } from './transcript';
import { HumanNotes } from './notes';
import { ActionsPanel, AnalysisPanel, CitedResult, ImportantPanel, QuestionsPanel } from './outcomes';
import { CandidatePanel, ReplayPanel } from './media';
import styles from './workspace.module.css';

export function MeetingPage({ meetingId }: { meetingId: string }) {
  const query = useResource('meetings.get', { id: meetingId });
  const setActiveMeeting = useInterface((state) => state.setActiveMeeting);
  useEffect(() => { setActiveMeeting(meetingId); return () => setActiveMeeting(null); }, [meetingId, setActiveMeeting]);
  return <AsyncView query={query}>{(data) => <MeetingWorkspace meeting={item(data)} />}</AsyncView>;
}

function MeetingWorkspace({ meeting }: { meeting: RecordData }) {
  const meetingId = id(meeting);
  const panel = useInterface((state) => state.panel);
  const setPanel = useInterface((state) => state.setPanel);
  const recording = ['recording', 'starting', 'paused'].includes(text(meeting.state));
  return <><PageHeader title={label(meeting)}><CaptureControls meeting={meeting} /><MeetingActions meeting={meeting} /></PageHeader><MeetingStatus meeting={meeting} /><CaptureAlert meeting={meeting} /><div className={styles.meeting}><div className={styles.record}><TabSet value={panel} onValueChange={setPanel} tabs={[
    { label: 'Transcript', content: <><Transcript meetingId={meetingId} recording={recording} />{!recording && <CandidatePanel meetingId={meetingId} />}</> },
    { label: 'Outcomes', content: <><AnalysisPanel meetingId={meetingId} /><ActionsPanel meetingId={meetingId} /><AnalysisPanel meetingId={meetingId} task="topics" /><QuestionsPanel meetingId={meetingId} /><ImportantPanel meetingId={meetingId} /></> },
    { label: 'Minutes', content: <><MinutesPanel meeting={meeting} /><FinalizationPanel meetingId={meetingId} /></> },
  ]} /></div><aside className={styles.margin} aria-label="Your notes"><HumanNotes meetingId={meetingId} />{!recording && <ReplayPanel meetingId={meetingId} recording={recording} />}</aside></div></>;
}

const CAPTURE_ALERTS: Record<string, string> = {
  'capture.attention': 'Transcription needs attention',
  'capture.backpressure': 'Transcription fell behind, so capture paused',
  'native.capture.error': 'Capture problem',
  'native.capture.source_lost': 'An audio source was lost',
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
    <ConfirmCommand trigger="Delete meeting…" title="Delete meeting?" message="Deletes the transcript, notes and audio. Anything already sent elsewhere stays." operation="meetings.delete" payload={{ id: id(meeting), expected_revision: revision(meeting), confirm: true }} onSuccess={() => { void navigate({ to: '/' }); }} {...bind('delete')} />
  </>;
}

const STAGES: Record<string, string> = { drain: 'Finish the transcript', final_diarization: 'Tell speakers apart', final_analysis: 'Summary, actions and topics', minutes: 'Minutes', memory: 'Project memory', index: 'Search index' };
const JOB_STATUS: Record<string, string> = { queued: 'Waiting', running: 'Running', succeeded: 'Done', failed: 'Failed', skipped: 'Skipped', cancelled: 'Cancelled' };

function FinalizationPanel({ meetingId }: { meetingId: string }) {
  const query = useResource('meetings.finalization', { meeting_id: meetingId });
  return <Panel title="Processing"><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="Not processed yet" /> : items(data).map((job) => { const stage = text(job.stage ?? object(job.payload).stage ?? job.kind); return <article className={styles.actionRow} key={id(job)}><div><strong>{STAGES[stage] ?? stage.replaceAll('_', ' ')}</strong><Badge tone={job.status === 'failed' ? 'danger' : job.status === 'succeeded' ? 'success' : 'neutral'}>{JOB_STATUS[text(job.status)] ?? text(job.status)}</Badge>{Boolean(object(job.error).message) && <p>{text(object(job.error).message)}</p>}{job.error && <Details title="Details" value={job.error} />}<small>Updated {date(job.updated_at)}</small></div><Toolbar>{job.status === 'failed' && <CommandButton operation="meetings.retry" payload={{ meeting_id: meetingId, stage }}>Retry</CommandButton>}{['queued', 'running'].includes(text(job.status)) && <CommandButton operation="jobs.cancel" payload={{ id: id(job), expected_revision: revision(job) }}>Cancel</CommandButton>}</Toolbar></article>; })}</AsyncView></Panel>;
}

function MinutesPanel({ meeting }: { meeting: RecordData }) {
  const notes = useResource('notes.get', { meeting_id: id(meeting) });
  const minutes = useResource('minutes.get', { meeting_id: id(meeting) });
  return <Panel title="Minutes" actions={<span data-testid="prepare-minutes"><CommandButton operation="minutes.prepare" payload={{ meeting_id: id(meeting), note_revision: item(notes.data).revision ?? 0, transcript_revision: meeting.transcript_revision ?? 0 }}>Make minutes</CommandButton></span>}>
    <AsyncView query={minutes}>{(data) => {
      const current = object(data.item);
      return id(current) ? <><CitedResult result={current} /><EditResource operation="minutes.edit" row={current} fixed={{ meeting_id: id(meeting) }} label="Edit minutes" fields={[{ name: 'sections', label: 'Sections (JSON)', type: 'json', value: current.sections ?? [] }]} /></> : <Empty title="No minutes yet" />;
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
      const grant = share === 'save' ? await nativeRequest('file_grant', { purpose: 'export' }) : null;
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
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>Export…</button>} title="Export"><div className={styles.filters}><label>Format<select value={format} onChange={(event) => setFormat(event.target.value)}>{[['markdown', 'Markdown'], ['pdf', 'PDF'], ['srt', 'Subtitles (SRT)'], ['json', 'JSON']].map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label><label className={styles.check}><input type="checkbox" checked={includeTranscript} onChange={(event) => setIncludeTranscript(event.target.checked)} />Include transcript</label></div><label>Email recipients, separated by commas<input value={recipients} onChange={(event) => setRecipients(event.target.value)} /></label><Toolbar><button className={buttonClass.primary} disabled={exportOperation.isPending || !nativeAvailable()} onClick={() => exportOperation.mutate('save')}>Save export…</button><button disabled={exportOperation.isPending || !nativeAvailable()} onClick={() => exportOperation.mutate('copy')}>Copy email</button><button disabled={exportOperation.isPending || !nativeAvailable()} onClick={() => exportOperation.mutate('mailto')}>Open mail draft</button></Toolbar>{exportOperation.error && <ErrorNotice error={exportOperation.error} />}{exportOperation.isSuccess && <Details value={exportOperation.data} />}<DeliveryPreview meeting={meeting} /></Modal>;
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
