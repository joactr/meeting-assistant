import { useEffect, useState } from 'react';
import { useNavigate } from '@tanstack/react-router';
import { useMutation } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useResource } from '../lib/queries';
import { useInterface } from '../lib/ui-store';
import { date, id, item, items, label, object, revision, text, type RecordData } from '../lib/data';
import { ActionMenu } from '../components/controls';
import { AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, Modal, Notice, PageHeader, Panel, TabSet, Toolbar, useOpenState } from '../components/ui';
import { CaptureControls, CaptureStatus, LanguageControl, SpeakersPanel } from './capture';
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
  return <><PageHeader title={label(meeting)}><MeetingActions meeting={meeting} /></PageHeader><CaptureStatus meeting={meeting}><div className={styles.captureActions}><CaptureControls meeting={meeting} /></div></CaptureStatus>{meeting.health === 'degraded' && <Notice danger>Capture is degraded. Review individual source status; retry the missing source, pause, or explicitly continue with remaining audio. The app does not claim full capture.</Notice>}<TabSet value={panel} onValueChange={setPanel} tabs={[
    { label: 'Transcript', content: <><Transcript meetingId={meetingId} recording={recording} />{!recording && <ReplayPanel meetingId={meetingId} recording={recording} />}{!recording && <CandidatePanel meetingId={meetingId} />}</> },
    { label: 'Notes', content: <HumanNotes meetingId={meetingId} /> },
    { label: 'Summary', content: <><AnalysisPanel meetingId={meetingId} /><AnalysisPanel meetingId={meetingId} task="topics" /></> },
    { label: 'Actions', content: <ActionsPanel meetingId={meetingId} /> },
    { label: 'Questions', content: <><QuestionsPanel meetingId={meetingId} /><ImportantPanel meetingId={meetingId} /></> },
    { label: 'Speakers', content: <SpeakersPanel meetingId={meetingId} /> },
    { label: 'Minutes', content: <><MinutesPanel meeting={meeting} /><FinalizationPanel meetingId={meetingId} /></> },
  ]} /></>;
}

type MeetingDialog = 'export' | 'edit' | 'language' | 'delete' | null;

/** One primary action (Export) plus an overflow menu; each dialog stays mounted so the menu can close first. */
function MeetingActions({ meeting }: { meeting: RecordData }) {
  const navigate = useNavigate();
  const projects = useResource('projects.list');
  const packages = useResource('entity.list', { collection: 'work_packages' });
  const [dialog, setDialog] = useState<MeetingDialog>(null);
  const bind = (name: Exclude<MeetingDialog, null>) => ({ open: dialog === name, onOpenChange: (open: boolean) => setDialog(open ? name : null) });
  return <>
    <button onClick={() => setDialog('export')}>Export / share…</button>
    <ActionMenu label="More meeting actions" actions={[
      { label: 'Edit details…', onSelect: () => setDialog('edit') },
      { label: 'Output language…', onSelect: () => setDialog('language') },
      { label: 'Delete meeting…', danger: true, onSelect: () => setDialog('delete') },
    ]} />
    <ExportControls meeting={meeting} {...bind('export')} />
    <LanguageControl meeting={meeting} {...bind('language')} />
    <EditResource operation="meetings.update" row={meeting} label="Edit meeting details" fields={[{ name: 'title', label: 'Meeting title', required: true }, { name: 'project_id', label: 'Project', type: 'select', options: [{ value: '', label: 'No project' }, ...items(projects.data).map((row) => ({ value: id(row), label: label(row) }))] }, { name: 'work_package_id', label: 'Work package', type: 'select', options: [{ value: '', label: 'None' }, ...items(packages.data).map((row) => ({ value: id(row), label: label(row) }))] }]} {...bind('edit')} />
    <ConfirmCommand trigger="Delete meeting…" title="Delete meeting and local derived evidence?" message="Default deletion removes transcript, notes, derived text, search and media references. Already sent GitHub issues, Slack and email cannot be erased. Physical media purge can remain pending on Windows. This is not secure erasure on SSDs or backups." operation="meetings.delete" payload={{ id: id(meeting), expected_revision: revision(meeting), confirm: true }} onSuccess={() => { void navigate({ to: '/' }); }} {...bind('delete')} />
  </>;
}

function FinalizationPanel({ meetingId }: { meetingId: string }) {
  const query = useResource('meetings.finalization', { meeting_id: meetingId });
  return <Panel title="Preservation & finalization"><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No finalization stages">Stopping capture queues durable finalization jobs. Failures remain actionable here.</Empty> : items(data).map((job) => <article className={styles.actionRow} key={id(job)}><div><strong>{text(job.stage ?? object(job.payload).stage ?? job.kind)}</strong><Badge tone={job.status === 'failed' ? 'danger' : job.status === 'succeeded' ? 'success' : 'neutral'}>{text(job.status)}</Badge>{job.error && <Details title="Safe failure details" value={job.error} />}<small>Updated {date(job.updated_at)}</small></div><Toolbar>{job.status === 'failed' && <CommandButton operation="meetings.retry" payload={{ meeting_id: meetingId, stage: job.stage ?? object(job.payload).stage ?? '' }}>Retry this stage</CommandButton>}{['queued', 'running'].includes(text(job.status)) && <CommandButton operation="jobs.cancel" payload={{ id: id(job), expected_revision: revision(job) }}>Cancel job</CommandButton>}</Toolbar></article>)}</AsyncView></Panel>;
}

function MinutesPanel({ meeting }: { meeting: RecordData }) {
  const notes = useResource('notes.get', { meeting_id: id(meeting) });
  const minutes = useResource('minutes.get', { meeting_id: id(meeting) });
  const [draft, setDraft] = useState<RecordData | null>(null);
  const [prepareOpen, setPrepareOpen] = useState(false);
  return <Panel title="Human-guided minutes">
    <Modal open={prepareOpen} onOpenChange={setPrepareOpen} trigger={<button data-testid="prepare-minutes">Prepare minutes candidate</button>} title="Prepare minutes from a saved snapshot" description="First save human notes. This request records the exact note and active transcript revisions.">
      <CommandForm operation="minutes.prepare" fixed={{ meeting_id: id(meeting), note_revision: item(notes.data).revision ?? 0, transcript_revision: meeting.transcript_revision ?? 0 }} fields={[{ name: 'policy', label: 'Optional selected AI policy (JSON; empty object uses deterministic notes)', type: 'json', value: {} }]} submit="Prepare candidate" onSuccess={(result) => { setDraft(item(result)); setPrepareOpen(false); }} />
    </Modal>
    {draft && <>
      <h3>Reviewed candidate</h3><CitedResult result={draft} />
      <Details title="Candidate diff, conflicts, exact source revisions and provenance" value={draft} />
      <CommandForm operation="minutes.approve" fixed={{ id: id(draft), expected_revision: draft.approved_base_revision ?? null }} fields={[]} submit="Approve reviewed candidate" confirm="I reviewed this candidate, its source conflicts and changes against the approved revision." onSuccess={() => setDraft(null)} />
      <button onClick={() => setDraft(null)}>Close candidate review</button>
    </>}
    <AsyncView query={minutes}>{(data) => {
      const approved = object(data.item);
      const candidates = items({ items: data.candidates ?? [] });
      return <>
        <h3>Approved minutes</h3>
        {id(approved) ? <><CitedResult result={approved} /><EditResource operation="minutes.edit" row={approved} fixed={{ meeting_id: id(meeting) }} label="Edit approved sections" fields={[{ name: 'sections', label: 'Sections with stable IDs and preserved source references (JSON)', type: 'json', value: approved.sections ?? [] }]} /></> : <Empty title="No approved minutes yet">Prepare and review a candidate above. Deterministic export does not require an LLM.</Empty>}
        <h3>Preserved candidates</h3>
        {candidates.length === 0 ? <Empty title="No minutes candidates" /> : candidates.map((row) => <article key={id(row)}><Badge>{text(row.state)}</Badge>{Boolean(row.stale) && <Badge tone="warning">Stale evidence — regenerate before approval</Badge>}<small>Created {date(row.created_at)}</small><button onClick={() => setDraft(row)}>Review candidate revision</button></article>)}
      </>;
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
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>Export / share…</button>} title="Export saved meeting outcomes" description="Exports use committed revisions and persisted edits. Summary, actions and important moments are included by default; transcript and audio are never silently shared."><div className={styles.filters}><label>Format<select value={format} onChange={(event) => setFormat(event.target.value)}>{['markdown', 'pdf', 'srt', 'json'].map((value) => <option key={value} value={value}>{value.toUpperCase()}</option>)}</select></label><label className={styles.check}><input type="checkbox" checked={includeTranscript} onChange={(event) => setIncludeTranscript(event.target.checked)} />Include full transcript in supported formats</label></div><label>Email recipients (comma-separated)<input value={recipients} onChange={(event) => setRecipients(event.target.value)} /></label><Toolbar><button disabled={exportOperation.isPending || !nativeAvailable()} onClick={() => exportOperation.mutate('save')}>Save export…</button><button disabled={exportOperation.isPending || !nativeAvailable()} onClick={() => exportOperation.mutate('copy')}>Copy email</button><button disabled={exportOperation.isPending || !nativeAvailable()} onClick={() => exportOperation.mutate('mailto')}>Open shortened mail draft</button></Toolbar>{exportOperation.error && <ErrorNotice error={exportOperation.error} />}{exportOperation.isSuccess && <Details value={exportOperation.data} title="Export / native share receipt" />}<DeliveryPreview meetingId={id(meeting)} /></Modal>;
}

function DeliveryPreview({ meetingId }: { meetingId: string }) {
  const [preview, setPreview] = useState<RecordData | null>(null);
  return <><hr /><h3>Explicit Slack / SMTP delivery</h3><CommandForm operation="delivery.preview" fixed={{ meeting_id: meetingId, include_transcript: false }} fields={[{ name: 'connection_id', label: 'Configured destination connection ID', required: true }]} submit="Preview exact delivery" onSuccess={(result) => setPreview(item(result))} />{preview && <><Details value={preview} title="Reviewed destination and content" /><CommandForm operation="delivery.send" fixed={{ id: id(preview), expected_revision: revision(preview), reviewed: true }} fields={[]} submit="Send reviewed content" confirm="I reviewed the fixed recipients/destination and content. Remote sends cannot be recalled; an unknown result must not be blindly resent." /></>}</>;
}
