import { useState, type ReactNode } from 'react';
import { Link, useNavigate } from '@tanstack/react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { date, id, item, items, label, object, text, when, type Payload } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, Details, Empty, ErrorNotice, FieldInput, Modal, Notice, PageHeader, Panel, TabSet, Toolbar } from '../components/ui';
import { VirtualList } from '../components/virtual-list';
import { PrepareEvent } from './connections';
import { GetStarted, useGetStarted } from './providers';
import styles from './workspace.module.css';

const LANGUAGES = [['en', 'English'], ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'], ['it', 'Italian'], ['pt', 'Portuguese'], ['nl', 'Dutch'], ['ca', 'Catalan'], ['pl', 'Polish'], ['ja', 'Japanese'], ['zh', 'Chinese'], ['ko', 'Korean'], ['ar', 'Arabic'], ['hi', 'Hindi'], ['ru', 'Russian']] as const;
export function NewMeeting({ trigger = 'New meeting' }: { trigger?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const projects = useResource('projects.list');
  const setup = useResource('setup.status');
  const languageSelectable = object(setup.data?.asr).language_selectable === true;
  return <Modal open={open} onOpenChange={setOpen} trigger={<button data-testid="new-meeting">{trigger}</button>} title="Prepare a meeting"><CommandForm operation="meetings.create" fields={[
    { name: 'title', label: 'Meeting name', required: true },
    { name: 'project_id', label: 'Project', type: 'select', options: [{ value: '', label: 'No project' }, ...items(projects.data).map((row) => ({ value: id(row), label: label(row) }))] },
    ...(languageSelectable ? [{ name: 'language', label: 'Language', type: 'select' as const, value: '', options: [{ value: '', label: 'Detect automatically' }, ...LANGUAGES.map(([value, name]) => ({ value, label: name }))] }] : []),
    { name: 'source', label: 'Capture sources', type: 'select', value: 'both', options: [{ value: 'both', label: 'Microphone + system audio' }, { value: 'mic', label: 'Microphone only' }, { value: 'system', label: 'System audio only' }] },
    { name: 'pre_briefing', label: 'Human agenda / preparation', type: 'textarea' },
  ]} mapPayload={(payload) => { const { pre_briefing, ...meeting } = payload; return { ...meeting, config: { human_agenda: pre_briefing ?? '' } }; }} submit="Create preparation" onSuccess={(result) => { const meeting = item(result); setOpen(false); void navigate({ to: '/meeting/$meetingId', params: { meetingId: id(meeting) } }); }} /></Modal>;
}

export function ImportMeeting() {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [retained, setRetained] = useState(false);
  const [consented, setConsented] = useState(false);
  const [profileId, setProfileId] = useState('');
  const profiles = useResource('entity.list', { collection: 'model_profiles' }, open);
  const client = useQueryClient();
  const navigate = useNavigate();
  const operation = useMutation({
    mutationFn: async () => {
      const grant = await nativeRequest('file_grant', { purpose: 'import_audio' });
      if (grant.cancelled === true) return {};
      return request('media.import', { file_grant: grant.grant_id ?? '', title, retain_audio: retained, temporary_decode_consent: consented, asr_policy: { model_profile_id: profileId }, idempotency_key: crypto.randomUUID() });
    },
    onSuccess: async (result) => { await client.invalidateQueries({ queryKey: ['engine'] }); const meetingId = text(item(result).meeting_id ?? item(result).id); if (meetingId) { setOpen(false); void navigate({ to: '/meeting/$meetingId', params: { meetingId } }); } },
  });
  return <Modal open={open} onOpenChange={setOpen} trigger={<button>Import audio</button>} title="Import audio"><form onSubmit={(event) => { event.preventDefault(); operation.mutate(); }}><FieldInput field={{ name: 'title', label: 'Meeting name', required: true }} value={title} onChange={(value) => setTitle(text(value))} /><FieldInput field={{ name: 'profile_id', label: 'Transcription profile', type: 'select', required: true, options: [{ value: '', label: 'Choose profile' }, ...items(profiles.data).filter((profile) => profile.role === 'asr').map((profile) => ({ value: id(profile), label: label(profile) }))] }} value={profileId} onChange={(value) => setProfileId(text(value))} />{profiles.error && <ErrorNotice error={profiles.error} />}<label className={styles.check}><input type="checkbox" checked={consented} onChange={(event) => setConsented(event.target.checked)} required />Allow temporary decoded audio (deleted afterwards)</label><label className={styles.check}><input type="checkbox" checked={retained} onChange={(event) => setRetained(event.target.checked)} />Keep the audio. I have permission from participants.</label><Notice>A remote transcription provider may process this audio.</Notice><button type="submit" disabled={!nativeAvailable() || operation.isPending || !consented || !profileId}>{operation.isPending ? 'Importing…' : 'Choose file and import'}</button>{operation.error && <ErrorNotice error={operation.error} />}</form></Modal>;
}

function AttentionQueue() {
  const query = useResource('jobs.attention');
  return <Panel title="Needs attention"><AsyncView query={query}>{(data) => <>{items(data).length === 0 && items({ items: data.meetings }).length === 0 && <Empty title="Nothing needs attention" />}{items({ items: data.meetings }).map((meeting) => <article className={styles.listRow} key={id(meeting)}><div><strong>{label(meeting)}</strong><p>{text(meeting.interrupted_reason ?? meeting.state)}</p></div><Link to="/meeting/$meetingId" params={{ meetingId: id(meeting) }}>Open meeting</Link></article>)}{items(data).map((row) => <article className={styles.listRow} key={id(row)}><div><strong>{label(row)}</strong><p>{text(row.message ?? row.state)}</p><Details value={row} /></div><Toolbar>{row.meeting_id && <Link to="/meeting/$meetingId" params={{ meetingId: text(row.meeting_id) }}>Open meeting</Link>}<CommandButton operation="jobs.retry" payload={{ job_id: id(row) }}>Retry</CommandButton></Toolbar></article>)}</>}</AsyncView></Panel>;
}

function Upcoming() {
  const query = useResource('calendar.events', { upcoming: true });
  return <Panel title="Upcoming"><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No upcoming events" /> : items(data).map((row) => <article className={styles.listRow} key={id(row)}><div><strong>{label(row)}</strong><p>{date(row.start)} · {text(row.original_timezone, 'Calendar timezone')}</p></div><PrepareEvent event={row} /></article>)}</AsyncView></Panel>;
}

export function LibraryPage() {
  const [project, setProject] = useState('');
  const [status, setStatus] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<(string | null)[]>([]);
  const projects = useResource('projects.list');
  const payload: Payload = { limit: 100, ...(project ? { project_id: project } : {}), ...(status ? { state: status } : {}), ...(fromDate ? { from_date: fromDate } : {}), ...(cursor ? { cursor } : {}) };
  const query = useResource('meetings.list', payload);
  const reset = (): void => { setCursor(null); setPrevious([]); };
  const setup = useResource('setup.status');
  const [started, setStarted] = useGetStarted();
  return <><PageHeader title="Library"><ImportMeeting /></PageHeader>{setup.data?.asr_ready === false && <Empty title="Transcription isn't set up yet"><button onClick={() => setStarted(true)}>Set up transcription</button></Empty>}<GetStarted open={started} onOpenChange={setStarted} /><TabSet tabs={[
    { label: 'Meetings', content: <div><div className={styles.filters}><FieldInput field={{ name: 'project', label: 'Project', type: 'select', options: [{ value: '', label: 'All projects' }, ...items(projects.data).map((row) => ({ value: id(row), label: label(row) }))] }} value={project} onChange={(value) => { setProject(text(value)); reset(); }} /><FieldInput field={{ name: 'status', label: 'Status', type: 'select', options: ['', 'draft', 'recording', 'paused', 'processing', 'complete', 'interrupted', 'attention_required'].map((value) => ({ value, label: value.replaceAll('_', ' ') || 'All statuses' })) }} value={status} onChange={(value) => { setStatus(text(value)); reset(); }} /><FieldInput field={{ name: 'from', label: 'From date', type: 'date' }} value={fromDate} onChange={(value) => { setFromDate(text(value)); reset(); }} /></div><AsyncView query={query}>{(data) => { const rows = items(data); return rows.length === 0 ? <Empty title="No meetings yet" /> : <><div className={styles.tableHeader}><span>Meeting</span><span>Last updated</span></div><VirtualList rows={rows} label="Meeting history" render={(row) => <article className={styles.listRow}><div><Link className={styles.meetingTitle} to="/meeting/$meetingId" params={{ meetingId: id(row) }}>{label(row)}</Link><p>{text(items(projects.data).find((p) => id(p) === row.project_id)?.name, 'No project')} · {when(row.started_at ?? row.created_at)}</p></div><div>{row.state !== 'complete' && <Badge tone={row.state === 'interrupted' || row.state === 'attention_required' ? 'warning' : row.state === 'recording' ? 'success' : 'neutral'}>{text(row.state ?? row.status, 'draft').replaceAll('_', ' ')}</Badge>}<p>{when(row.updated_at ?? row.created_at)}</p></div></article>} /><Toolbar><button disabled={previous.length === 0} onClick={() => { setCursor(previous[previous.length - 1] ?? null); setPrevious((current) => current.slice(0, -1)); }}>Previous page</button><button disabled={!data.next_cursor} onClick={() => { setPrevious((current) => [...current, cursor]); setCursor(text(data.next_cursor)); }}>Next page</button><small>{rows.length} meetings</small></Toolbar></>; }}</AsyncView></div> },
    { label: 'Upcoming', content: <Upcoming /> },
    { label: 'Attention', content: <AttentionQueue /> },
  ]} /></>;
}

