import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { array, clock, id, item, items, label, text, type Payload, type RecordData } from '../lib/data';
import { Bookmark } from 'lucide-react';
import { FilterDisclosure, MeetingPicker, Segmented } from '../components/controls';
import { AsyncView, Badge, CommandForm, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, PageHeader, Panel, Toolbar } from '../components/ui';
import { ActionRow, Citation, CitedResult } from './outcomes';
import { VirtualList } from '../components/virtual-list';
import styles from './workspace.module.css';

export function FollowupsPage() {
  const [filters, setFilters] = useState<Payload>({ status: 'open', priority: '', assignee: '', meeting_id: '', project_id: '', from_date: '', to_date: '' });
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<(string | null)[]>([]);
  const query = useResource('followups.list', { ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== '')), limit: 50, ...(cursor ? { cursor } : {}) });
  const projects = useResource('projects.list');
  const change = (key: string, value: Payload[string]): void => { setFilters((current) => ({ ...current, [key]: value })); setCursor(null); setPrevious([]); };
  const extra = ['priority', 'meeting_id', 'from_date', 'to_date'].filter((key) => filters[key] !== '').length;
  return <><PageHeader title="Follow-ups" /><Panel>
    <FilterDisclosure title="More filters" count={extra} bar={<>
      <Segmented legend="Status" value={text(filters.status)} onChange={(value) => change('status', value)} options={[{ value: 'open', label: 'Open' }, { value: 'in_progress', label: 'In progress' }, { value: 'done', label: 'Done' }, { value: '', label: 'All' }]} />
      <FieldInput field={{ name: 'project', label: 'Project', type: 'select', options: [{ value: '', label: 'All projects' }, ...items(projects.data).map((project) => ({ value: id(project), label: label(project) }))] }} value={filters.project_id ?? ''} onChange={(value) => change('project_id', value)} />
      <FieldInput field={{ name: 'assignee', label: 'Assignee' }} value={filters.assignee ?? ''} onChange={(value) => change('assignee', value)} />
    </>}>
      <FieldInput field={{ name: 'priority', label: 'Priority', type: 'select', options: ['', 'low', 'normal', 'high', 'urgent'].map((value) => ({ value, label: value || 'Any priority' })) }} value={filters.priority ?? ''} onChange={(value) => change('priority', value)} />
      <MeetingPicker title="Meeting" values={filters.meeting_id ? [text(filters.meeting_id)] : []} onChange={(values) => change('meeting_id', values[0] ?? '')} />
      <FieldInput field={{ name: 'from', label: 'From date', type: 'date' }} value={filters.from_date ?? ''} onChange={(value) => change('from_date', value)} />
      <FieldInput field={{ name: 'to', label: 'Through date', type: 'date' }} value={filters.to_date ?? ''} onChange={(value) => change('to_date', value)} />
    </FilterDisclosure>
    <AsyncView query={query}>{(data) => items(data).length === 0 ? <Empty title="No follow-ups match these filters" /> : <>
      <VirtualList rows={items(data)} label="Follow-up results" estimate={76} height="min(62vh, 720px)" render={(row) => row.kind === 'moment' || row.entity_type === 'important_moment' ? <ImportantFollowup row={row} /> : <ActionRow row={row} />} />
      <Toolbar><button disabled={!previous.length} onClick={() => { setCursor(previous[previous.length - 1] ?? null); setPrevious((current) => current.slice(0, -1)); }}>Previous page</button><button disabled={!data.next_cursor} onClick={() => { setPrevious((current) => [...current, cursor]); setCursor(text(data.next_cursor)); }}>Next page</button></Toolbar>
    </>}</AsyncView>
  </Panel></>;
}

function ImportantFollowup({ row }: { row: RecordData }) {
  return <article className={styles.taskRow}>
    <Bookmark size={18} aria-hidden="true" className={styles.taskMark} />
    <div className={styles.taskBody}>
      <strong>{text(row.note ?? row.source_quote ?? row.quote)}</strong>
      <div className={styles.taskMeta}><Badge>Important moment</Badge><span>{text(row.assignee, 'Unassigned')}</span><span>{text(row.priority, 'normal')}</span><span>{text(row.status, 'open')}</span><Citation source={row} /></div>
    </div>
    <EditResource operation="moments.update" row={row} label="Update follow-up" fields={[{ name: 'note', label: 'Note', type: 'textarea' }, { name: 'status', label: 'Status', type: 'select', options: ['open', 'done', 'dismissed'].map((value) => ({ value, label: value })) }, { name: 'assignee', label: 'Assignee' }, { name: 'priority', label: 'Priority', type: 'select', options: ['low', 'normal', 'high'].map((value) => ({ value, label: value })) }]} />
  </article>;
}

const escapeExpression = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Trims an excerpt around its first match so long passages show the relevant sentence, not a wall of text. */
function excerpt(source: string, terms: string[], limit = 320): string {
  if (source.length <= limit) return source;
  const hit = terms.length ? source.search(new RegExp(terms.map(escapeExpression).join('|'), 'i')) : -1;
  const start = Math.max(0, (hit < 0 ? 0 : hit) - 110);
  const end = Math.min(source.length, start + limit);
  return `${start > 0 ? '… ' : ''}${source.slice(start, end).trim()}${end < source.length ? ' …' : ''}`;
}

function Highlight({ value, terms }: { value: string; terms: string[] }) {
  if (!terms.length) return <>{value}</>;
  const parts = value.split(new RegExp(`(${terms.map(escapeExpression).join('|')})`, 'i'));
  return <>{parts.map((part, index) => index % 2 === 1 ? <mark key={index}>{part}</mark> : part)}</>;
}

export function SearchPage() {
  const [queryText, setQueryText] = useState('');
  const [mode, setMode] = useState('keyword');
  const [projectId, setProjectId] = useState('');
  const [meetingIds, setMeetingIds] = useState<string[]>([]);
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [historicalConsent, setHistoricalConsent] = useState(false);
  const [currentQuery, setCurrentQuery] = useState<Payload | null>(null);
  const [ask, setAsk] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<(string | null)[]>([]);
  const projects = useResource('projects.list');
  const results = useResource('search.query', { ...(currentQuery ?? {}), ...(cursor ? { cursor } : {}) }, !!currentQuery && !ask);
  const askRequest = useMutation({ mutationFn: (payload: Payload) => request('ask.query', payload), retry: false });
  const terms = text(currentQuery?.query).split(/\s+/).filter((term) => term.length > 1);
  const activeFilters = [mode !== 'keyword', projectId, meetingIds.length, fromDate, toDate].filter(Boolean).length;
  const submit = (askMode: boolean): void => {
    const scope: Payload = { query: queryText, mode, ...(projectId ? { project_id: projectId } : {}), ...(meetingIds.length ? { meeting_ids: meetingIds } : {}), ...(fromDate ? { from_date: fromDate } : {}), ...(toDate ? { to_date: toDate } : {}), limit: 50 };
    setAsk(askMode); setCurrentQuery(scope); setCursor(null); setPrevious([]);
    if (askMode) askRequest.mutate({ ...scope, historical_excerpt_consent: historicalConsent, idempotency_key: crypto.randomUUID() });
  };
  return <>
    <PageHeader title="Search & Ask" />
    <Panel>
    <form onSubmit={(event) => { event.preventDefault(); submit(false); }}>
      <label className={styles.searchInput}>Query<input name="query" value={queryText} onChange={(event) => setQueryText(event.target.value)} placeholder="A phrase, decision, or question…" required autoFocus /></label>
      <div className={styles.searchActions}>
        <button type="submit" className={styles.primaryAction} disabled={!queryText.trim()}>Search</button>
        <button type="button" disabled={!queryText.trim() || askRequest.isPending} onClick={() => submit(true)}>{askRequest.isPending ? 'Finding an answer…' : 'Ask'}</button>
        <label className={styles.check}><input type="checkbox" checked={historicalConsent} onChange={(event) => setHistoricalConsent(event.target.checked)} />Let Ask send matching excerpts to your AI provider</label>
      </div>
    </form>
    {/* Outside the form: portal dialogs bubble React submit events to their ancestor form. */}
      <FilterDisclosure title="Filters" count={activeFilters}>
        <FieldInput field={{ name: 'mode', label: 'Match by', type: 'select', options: [{ value: 'keyword', label: 'Exact words' }, { value: 'semantic', label: 'Meaning' }, { value: 'hybrid', label: 'Both' }] }} value={mode} onChange={(value) => setMode(text(value))} />
        <FieldInput field={{ name: 'project', label: 'Project', type: 'select', options: [{ value: '', label: 'All projects' }, ...items(projects.data).map((project) => ({ value: id(project), label: label(project) }))] }} value={projectId} onChange={(value) => setProjectId(text(value))} />
        <MeetingPicker title="Meetings" multiple values={meetingIds} onChange={setMeetingIds} projectId={projectId} placeholder="All meetings" />
        <FieldInput field={{ name: 'from', label: 'From date', type: 'date' }} value={fromDate} onChange={(value) => setFromDate(text(value))} />
        <FieldInput field={{ name: 'to', label: 'Through date', type: 'date' }} value={toDate} onChange={(value) => setToDate(text(value))} />
        <SemanticIndex meetingIds={meetingIds} />
      </FilterDisclosure>
    </Panel>
    {!currentQuery && <Empty title="Search across your meetings" />}
    {currentQuery && !ask && <Panel title="Results"><AsyncView query={results}>{(data) => <>
      {data.retrieval_mode === 'keyword' && mode !== 'keyword' && <Notice>Matching by meaning isn't available yet, so these are exact-word matches.</Notice>}
      {(Boolean(data.destination) || Boolean(data.fallback_reason ?? data.warning)) && <Toolbar>{Boolean(data.destination) && <Badge>Sent to: {text(data.destination)}</Badge>}{Boolean(data.fallback_reason ?? data.warning) && <Notice>{text(data.fallback_reason ?? data.warning)}</Notice>}</Toolbar>}
      {items(data).length === 0 ? <Empty title="No matches" /> : <VirtualList label="Search results" rows={items(data)} estimate={130} render={(row) => <article className={styles.searchResult}><h3>{text(row.meeting_title, 'Meeting passage')}</h3><p><Highlight value={excerpt(text(row.excerpt ?? row.text), terms)} terms={terms} /></p><Toolbar><Citation source={row} /><small>{clock(row.start_ms)}–{clock(row.end_ms)}</small></Toolbar></article>} />}
      <Toolbar><button disabled={!previous.length} onClick={() => { setCursor(previous[previous.length - 1] ?? null); setPrevious((values) => values.slice(0, -1)); }}>Previous results</button><button disabled={!data.next_cursor} onClick={() => { setPrevious((values) => [...values, cursor]); setCursor(text(data.next_cursor)); }}>Next results</button></Toolbar>
    </>}</AsyncView></Panel>}
    {ask && <Panel title="Answer">{askRequest.isPending && <Notice>Finding relevant passages and checking every citation…</Notice>}{askRequest.error && <ErrorNotice error={askRequest.error} retry={() => submit(true)} />}{askRequest.data && <AskAnswer answer={askRequest.data} />}</Panel>}
  </>;
}

function SemanticIndex({ meetingIds }: { meetingIds: string[] }) {
  return <Modal trigger={<button type="button">Build / rebuild semantic index…</button>} title="Explicit semantic index rebuild" description="The active index is replaced only after all embedding batches succeed.">
    <Notice>{meetingIds.length ? `Embedding scope: ${meetingIds.length} selected meeting${meetingIds.length === 1 ? '' : 's'}` : 'Embedding scope: all active meetings in this workspace.'} A remote embeddings destination requires separate historical-excerpt consent.</Notice>
    <CommandForm operation="search.reindex" fixed={{ ...(meetingIds.length ? { meeting_ids: meetingIds } : {}), idempotency_key: crypto.randomUUID() }} fields={[]} submit="Queue real semantic rebuild" confirm="I reviewed the exact embedding scope and configured provider destination/cost." />
  </Modal>;
}

function AskAnswer({ answer }: { answer: RecordData }) {
  const result: RecordData = { ...item(answer), citations: answer.citations ?? [] };
  const insufficient = result.insufficient_evidence === true || result.insufficient_evidence === 1;
  return <><Toolbar><Badge>Destination: {text(result.destination, 'Configured Ask task policy')}</Badge><Badge>Retrieval: {text(answer.retrieval_mode, 'Reported in provenance')}</Badge>{insufficient && <Badge tone="warning">Insufficient evidence</Badge>}</Toolbar><CitedResult result={result} />{array(result.citations).length === 0 && !insufficient && <Notice>No valid citations returned. Do not treat uncited text as a grounded meeting claim.</Notice>}<Details value={answer} title="Details" /></>;
}
