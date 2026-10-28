import { useState } from 'react';
import { useNavigate } from '@tanstack/react-router';
import { useMutation } from '@tanstack/react-query';
import { nativeRequest, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { date, id, item, items, label, object, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, EditResource, Empty, ErrorNotice, FieldInput, Modal, Notice, Panel, TabSet, Toolbar } from '../components/ui';
import { SecureSecret } from './providers';
import styles from './workspace.module.css';

export function CalendarSettings() {
  const connections = useResource('calendar.connection.list');
  const events = useResource('calendar.events');
  const [authorized, setAuthorized] = useState<RecordData | null>(null);
  const [provider, setProvider] = useState('google');
  return <><Panel title="Calendar"><Modal trigger={<button>Connect calendar…</button>} title="Connect calendar"><FieldInput field={{ name: 'provider', label: 'Provider', type: 'select', options: [{ value: 'google', label: 'Google Calendar' }, { value: 'microsoft', label: 'Microsoft Calendar' }] }} value={provider} onChange={(value) => setProvider(text(value))} /><CommandForm operation="calendar.connection.authorize" fixed={{ provider }} fields={[{ name: 'client_id', label: 'Client ID', required: true }, ...(provider === 'microsoft' ? [{ name: 'tenant', label: 'Tenant', value: 'common' }] : [])]} submit="Authorize in browser" onSuccess={setAuthorized} />{authorized && <><Notice>Authorized. Choose calendars below.</Notice><CommandForm operation="calendar.connection.save" fixed={{ provider, secret_ref: authorized.secret_ref ?? item(authorized).secret_ref ?? '', client_id: authorized.client_id ?? '' }} fields={[{ name: 'client_id', label: 'Client ID', required: true }, { name: 'calendar_ids', label: 'Calendar IDs (JSON array)', type: 'json', value: [], required: true }, { name: 'timezone', label: 'Timezone', value: Intl.DateTimeFormat().resolvedOptions().timeZone, required: true }, { name: 'reminders_enabled', label: 'Reminders', type: 'checkbox', value: false }, { name: 'reminder_minutes', label: 'Minutes before event', type: 'number', value: 5, min: 0, max: 1440 }, { name: 'project_mapping', label: 'Project mapping (JSON)', type: 'json', value: {} }]} /></>}<button onClick={() => { void nativeRequest('oauth_cancel').catch(() => undefined); }}>Cancel authorization</button></Modal><AsyncView query={connections}>{(data) => items(data).length === 0 ? <Empty title="No calendars connected" /> : items(data).map((row) => <CalendarConnection key={id(row)} row={row} />)}</AsyncView></Panel><Panel title="Events" actions={<Modal trigger={<button>Add event</button>} title="Add event"><CommandForm operation="calendar.event.save" fields={[{ name: 'title', label: 'Title', required: true }, { name: 'start', label: 'Start (ISO date-time)', required: true }, { name: 'end', label: 'End (ISO date-time)', required: true }, { name: 'timezone', label: 'Timezone', value: Intl.DateTimeFormat().resolvedOptions().timeZone, required: true }, { name: 'attendees', label: 'Attendees (JSON array)', type: 'json', value: [] }, { name: 'series_id', label: 'Series ID (optional)' }]} submit="Save" /></Modal>}><AsyncView query={events}>{(data) => items(data).length === 0 ? <Empty title="No events" /> : items(data).map((event) => <article className={styles.listRow} key={id(event)}><div><strong>{label(event)}</strong><p>{date(event.start)}, {text(event.original_timezone)}</p>{event.cancelled === true && <Badge>Cancelled</Badge>}</div><PrepareEvent event={event} /></article>)}</AsyncView></Panel></>;
}

function CalendarConnection({ row }: { row: RecordData }) {
  const [calendars, setCalendars] = useState<RecordData | null>(null);
  return <article className={styles.actionRow}><div><strong>{text(row.provider)} calendar</strong><p>{row.enabled === false ? 'Disconnected' : 'Connected'}, {text(row.timezone)}</p><Details value={row} title="Details" /></div><Toolbar><CommandButton operation="calendar.calendars" payload={{ connection_id: id(row) }} onSuccess={setCalendars}>List calendars</CommandButton><CommandButton operation="calendar.sync" payload={{ connection_id: id(row) }}>Sync</CommandButton><EditResource operation="calendar.connection.save" row={row} label="Edit" fields={[{ name: 'provider', label: 'Provider', value: row.provider, required: true }, { name: 'client_id', label: 'Client ID', value: row.client_id }, { name: 'calendar_ids', label: 'Calendar IDs (JSON array)', type: 'json' }, { name: 'timezone', label: 'Timezone' }, { name: 'reminders_enabled', label: 'Reminders', type: 'checkbox' }, { name: 'reminder_minutes', label: 'Minutes before event', type: 'number', min: 0, max: 1440 }, { name: 'project_mapping', label: 'Project mapping (JSON)', type: 'json' }]} /><DisconnectConnection row={row} operation="calendar.connection.disconnect" /></Toolbar>{calendars && <Details value={calendars} title="Details" />}</article>;
}

export function PrepareEvent({ event }: { event: RecordData }) {
  const [open, setOpen] = useState(false);
  const [preparation, setPreparation] = useState<RecordData | null>(null);
  const navigate = useNavigate();
  const select = useMutation({ mutationFn: () => request('calendar.select', { id: id(event) }), onSuccess: setPreparation });
  return <Modal open={open} onOpenChange={setOpen} trigger={<button disabled={event.cancelled === true} onClick={() => select.mutate()}>Prepare meeting</button>} title="Prepare meeting">{select.isPending && <Notice>Loading…</Notice>}{select.error && <ErrorNotice error={select.error} retry={() => select.mutate()} />}{preparation && <><Details value={preparation} title="Details" /><CommandForm operation="meetings.create" fixed={{ ...object(preparation.draft), source: 'both' }} fields={[{ name: 'title', label: 'Meeting title', value: object(preparation.draft).title ?? '', required: true }, { name: 'project_id', label: 'Project ID', value: object(preparation.draft).project_id ?? '' }]} submit="Create" onSuccess={(result) => { setOpen(false); void navigate({ to: '/meeting/$meetingId', params: { meetingId: id(item(result)) } }); }} /></>}</Modal>;
}

export function IntegrationSettings() {
  return <TabSet tabs={[{ label: 'GitHub', content: <GitHubSettings /> }, { label: 'Slack / SMTP', content: <DeliverySettings /> }]} />;
}

function GitHubSettings() {
  const connections = useResource('github.connection.list');
  const [auth, setAuth] = useState<RecordData | null>(null);
  const [secretRef, setSecretRef] = useState('');
  const [device, setDevice] = useState<RecordData | null>(null);
  return <Panel title="GitHub"><Modal trigger={<button>Connect GitHub…</button>} title="Connect GitHub"><CommandForm operation="github.auth.begin" fields={[{ name: 'client_id', label: 'Client ID', required: true }]} submit="Start" onSuccess={(result) => { setDevice(item(result)); if (result.secret_ref || item(result).secret_ref) setAuth(item(result)); }} />{device && <><Details title="Details" value={device} /><CommandButton operation="github.auth.poll" payload={{ flow_id: device.flow_id ?? device.id ?? '' }} onSuccess={(result) => { setDevice(item(result)); if (item(result).secret_ref) setAuth(item(result)); }}>Check authorization</CommandButton><CommandButton operation="github.auth.cancel" payload={{ flow_id: device.flow_id ?? device.id ?? '' }}>Cancel</CommandButton></>}{auth && <CommandForm operation="github.connection.save" fixed={{ secret_ref: auth.secret_ref ?? '', credential_kind: 'device' }} fields={[{ name: 'client_id', label: 'Client ID', required: true }, { name: 'repositories', label: 'Repositories (JSON array of owner/name)', type: 'json', value: [], required: true }]} submit="Save" />}</Modal><Modal trigger={<button>Use access token…</button>} title="Use access token"><SecureSecret label="GitHub token" onStored={setSecretRef} /><CommandForm operation="github.connection.save" fixed={{ secret_ref: secretRef, credential_kind: 'pat' }} fields={[{ name: 'api_origin', label: 'API URL', value: 'https://api.github.com', required: true }, { name: 'repositories', label: 'Repositories (JSON array of owner/name)', type: 'json', value: [], required: true }]} submit="Save" /></Modal><AsyncView query={connections}>{(data) => items(data).length === 0 ? <Empty title="GitHub not connected" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{text(row.api_origin, 'https://api.github.com')}</strong><p>{Array.isArray(row.repositories) ? row.repositories.map((repo) => text(repo)).join(', ') : ''}</p><Details value={row} title="Details" /></div><><CommandButton operation="github.repositories" payload={{ connection_id: id(row) }} onSuccess={(result) => setDevice(result)}>List repositories</CommandButton><DisconnectConnection row={row} operation="github.connection.disconnect" /></></article>)}</AsyncView>{device && <Details title="Details" value={device} />}</Panel>;
}

function DeliverySettings() {
  const [secretRef, setSecretRef] = useState('');
  const [receipt, setReceipt] = useState<RecordData | null>(null);
  const destinations = useResource('delivery.list');
  return <Panel title="Delivery"><SecureSecret label="Slack webhook URL or SMTP credentials (JSON)" onStored={setSecretRef} /><CommandForm operation="delivery.configure" fixed={{ secret_ref: secretRef, automatic: false }} fields={[{ name: 'transport', label: 'Type', type: 'select', value: 'slack', options: [{ value: 'slack', label: 'Slack' }, { value: 'smtp', label: 'Email (SMTP)' }] }, { name: 'project_id', label: 'Project ID', required: true }, { name: 'destination', label: 'Slack channel or SMTP host', required: true }, { name: 'recipients', label: 'Recipients (JSON email array, SMTP only)', type: 'json', value: [] }, { name: 'enabled', label: 'Enabled', type: 'checkbox', value: true }]} submit="Save" /><AsyncView query={destinations}>{(data) => items(data).length === 0 ? <Empty title="No destinations" /> : items(data).map((row) => <article key={id(row)} className={styles.actionRow}><div><strong>{text(row.destination)}</strong><Badge>{text(row.transport)}</Badge><Details value={row} title="Details" /></div><CommandButton operation="delivery.test" payload={{ id: id(row), reviewed: true }} onSuccess={setReceipt}>Send test</CommandButton></article>)}</AsyncView>{receipt && <Details title="Details" value={receipt} />}</Panel>;
}

function DisconnectConnection({ row, operation }: { row: RecordData; operation: string }) {
  const [receipt, setReceipt] = useState<RecordData | null>(null);
  const [error, setError] = useState<unknown>();
  const revocation = object(receipt?.revocation);
  return <div>
    <ConfirmCommand trigger="Disconnect" title="Disconnect?" message="Deletes the saved credentials on this device. Calendar events and issues stay." operation={operation} payload={{ id: id(row), expected_revision: revision(row) }} onSuccess={setReceipt} />
    {receipt && <><Notice>Local credentials removed. Provider access: {text(revocation.provider_revocation, 'not confirmed')}.</Notice><Details title="Details" value={receipt} />{Boolean(revocation.manage_url) && <button onClick={() => { void nativeRequest('external_open', { url: text(revocation.manage_url) }).catch(setError); }}>Manage access</button>}</>}
    {Boolean(error) && <ErrorNotice error={error} />}
  </div>;
}
