import { useState, type ReactNode } from 'react';
import { Link, useNavigate } from '@tanstack/react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { date, id, item, items, label, text, when, type Payload } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, Details, Empty, ErrorNotice, FieldInput, Modal, Notice, PageHeader, Panel, TabSet, Toolbar } from '../components/ui';
import { VirtualList } from '../components/virtual-list';
import { PrepareEvent } from './connections';
import styles from './workspace.module.css';

export function NewMeeting({ trigger = 'New meeting' }: { trigger?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const projects = useResource('projects.list');
  const templates = useResource('templates.list');
  const packages = useResource('entity.list', { collection: 'work_packages' }, open);
  return <Modal open={open} onOpenChange={setOpen} trigger={<button data-testid="new-meeting">{trigger}</button>} title="Prepare a meeting" description="Nothing records until you explicitly press Start after device and provider checks."><CommandForm operation="meetings.create" fields={[
    { name: 'title', label: 'Meeting name', required: true },
    { name: 'project_id', label: 'Project', type: 'select', options: [{ value: '', label: 'No project — meeting-only context' }, ...items(projects.data).map((row) => ({ value: id(row), label: label(row) }))] },
    { name: 'work_package_id', label: 'Work package (optional)', type: 'select', options: [{ value: '', label: 'None' }, ...items(packages.data).map((row) => ({ value: id(row), label: label(row) }))] },
    { name: 'template_id', label: 'Template', type: 'select', options: [{ value: '', label: 'General' }, ...items(templates.data).map((row) => ({ value: id(row), label: label(row) }))] },
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
  return <Modal open={open} onOpenChange={setOpen} trigger={<button>Import audio</button>} title="Import a recording" description="Choose WAV, FLAC, MP3, Ogg/Opus or M4A. MP4 extracts audio only. Maximum default: 2 GiB / 8 hours."><form onSubmit={(event) => { event.preventDefault(); operation.mutate(); }}><FieldInput field={{ name: 'title', label: 'Meeting name', required: true }} value={title} onChange={(value) => setTitle(text(value))} /><FieldInput field={{ name: 'profile_id', label: 'Explicit ASR model profile', type: 'select', required: true, options: [{ value: '', label: 'Choose an acquired / authorized ASR profile' }, ...items(profiles.data).filter((profile) => profile.role === 'asr').map((profile) => ({ value: id(profile), label: label(profile) }))] }} value={profileId} onChange={(value) => setProfileId(text(value))} />{profiles.error && <ErrorNotice error={profiles.error} />}<label className={styles.check}><input type="checkbox" checked={consented} onChange={(event) => setConsented(event.target.checked)} required />Allow private temporary decoded audio for this file, removed on completion/cancel.</label><label className={styles.check}><input type="checkbox" checked={retained} onChange={(event) => setRetained(event.target.checked)} />Retain imported audio under workspace retention policy. I checked participant permission.</label><Notice>The chosen ASR destination may process this audio remotely when explicitly configured and consented. Original source bytes are never silently modified.</Notice><button type="submit" disabled={!nativeAvailable() || operation.isPending || !consented || !profileId}>{operation.isPending ? 'Opening and importing…' : 'Choose file and import'}</button>{operation.error && <ErrorNotice error={operation.error} />}</form></Modal>;
}

function AttentionQueue() {
  const query = useResource('jobs.attention');
  return <Panel title="Needs attention"><AsyncView query={query}>{(data) => <>{items(data).length === 0 && items({ items: data.meetings }).length === 0 && <Empty title="No attention items">Failures and recovery actions stay visible here, including interrupted capture and finalization.</Empty>}{items({ items: data.meetings }).map((meeting) => <article className={styles.listRow} key={id(meeting)}><div><strong>{label(meeting)}</strong><p>{text(meeting.interrupted_reason ?? meeting.state)}</p></div><Link to="/meeting/$meetingId" params={{ meetingId: id(meeting) }}>Recover preserved meeting</Link></article>)}{items(data).map((row) => <article className={styles.listRow} key={id(row)}><div><strong>{label(row)}</strong><p>{text(row.message ?? row.state)}</p><Details value={row} /></div><Toolbar>{row.meeting_id && <Link to="/meeting/$meetingId" params={{ meetingId: text(row.meeting_id) }}>Recover meeting</Link>}<CommandButton operation="jobs.retry" payload={{ job_id: id(row) }}>Retry stage</CommandButton></Toolbar></article>)}</>}</AsyncView></Panel>;
}

function Upcoming() {
  const query = useResource('calendar.events', { upcoming: true });
  return <Panel title="Upcoming preparation"><AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No upcoming events">Connect a read-only calendar in Settings, or prepare a meeting manually. Calendar events never start recording.</Empty> : items(data).map((row) => <article className={styles.listRow} key={id(row)}><div><strong>{label(row)}</strong><p>{date(row.start)} · {text(row.original_timezone, 'Calendar timezone')}</p></div><PrepareEvent event={row} /></article>)}</AsyncView></Panel>;
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
  return <><PageHeader title="Meeting library"><ImportMeeting /></PageHeader><TabSet tabs={[
    { label: 'Meetings', content: <div><div className={styles.filters}><FieldInput field={{ name: 'project', label: 'Project', type: 'select', options: [{ value: '', label: 'All projects' }, ...items(projects.data).map((row) => ({ value: id(row), label: label(row) }))] }} value={project} onChange={(value) => { setProject(text(value)); reset(); }} /><FieldInput field={{ name: 'status', label: 'Status', type: 'select', options: ['', 'draft', 'recording', 'paused', 'processing', 'complete', 'interrupted', 'attention_required'].map((value) => ({ value, label: value.replaceAll('_', ' ') || 'All statuses' })) }} value={status} onChange={(value) => { setStatus(text(value)); reset(); }} /><FieldInput field={{ name: 'from', label: 'From date', type: 'date' }} value={fromDate} onChange={(value) => { setFromDate(text(value)); reset(); }} /></div><AsyncView query={query}>{(data) => { const rows = items(data); return rows.length === 0 ? <Empty title="Your next conversation starts here">Prepare a meeting, choose your sources and press Start when ready. Nothing is recorded by opening this app.</Empty> : <><div className={styles.tableHeader}><span>Meeting</span><span>Last updated</span></div><VirtualList rows={rows} label="Meeting history" render={(row) => <article className={styles.listRow}><div><Link className={styles.meetingTitle} to="/meeting/$meetingId" params={{ meetingId: id(row) }}>{label(row)}</Link><p>{text(items(projects.data).find((p) => id(p) === row.project_id)?.name, 'No project')} · {when(row.started_at ?? row.created_at)}</p></div><div>{row.state !== 'complete' && <Badge tone={row.state === 'interrupted' || row.state === 'attention_required' ? 'warning' : row.state === 'recording' ? 'success' : 'neutral'}>{text(row.state ?? row.status, 'draft').replaceAll('_', ' ')}</Badge>}<p>{when(row.updated_at ?? row.created_at)}</p></div></article>} /><Toolbar><button disabled={previous.length === 0} onClick={() => { setCursor(previous[previous.length - 1] ?? null); setPrevious((current) => current.slice(0, -1)); }}>Previous page</button><button disabled={!data.next_cursor} onClick={() => { setPrevious((current) => [...current, cursor]); setCursor(text(data.next_cursor)); }}>Next page</button><small>{rows.length} meetings</small></Toolbar></>; }}</AsyncView></div> },
    { label: 'Upcoming', content: <Upcoming /> },
    { label: 'Attention', content: <AttentionQueue /> },
  ]} /></>;
}

