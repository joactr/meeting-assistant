import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { SecureSecret, destinationOf } from './providers';
import { id, items, label, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandButton, CommandForm, ConfirmCommand, Details, Empty, ErrorNotice, Modal, Panel, Toolbar } from '../components/ui';
import styles from './workspace.module.css';

/** What each retention.preview category removes (see the engine's purge). */
const purgeCategories: Record<string, string> = { expired_media: 'Expired audio recordings', candidates: 'Unreviewed AI suggestions older than 30 days', diagnostics: 'Diagnostic logs', events: 'Queued app events', cache: 'Cached files', voice_profiles: 'Voice profiles' };

export function PrivacySettings() {
  const consents = useResource('consents.list');
  const [diagnostics, setDiagnostics] = useState<RecordData | null>(null);
  return <><Panel title="Diagnostics"><Toolbar><CommandButton operation="diagnostics.get" onSuccess={setDiagnostics}>View diagnostics</CommandButton><Modal trigger={<button>Export support bundle…</button>} title="Export support bundle"><DiagnosticsExport /></Modal></Toolbar>{diagnostics && <Details value={diagnostics} title="Details" />}</Panel><Panel title="Outbound permissions"><AsyncView query={consents}>{(data) => items(data).length === 0 ? <Empty title="No permissions granted" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><strong>{text(row.purpose)}</strong><p>{text(row.destination_fingerprint)}</p><Badge>{row.revoked_at ? 'Revoked' : 'Active'}</Badge><Details value={row} title="Details" /></div><ConfirmCommand trigger="Revoke consent" title="Revoke permission?" message="Queued network work is cancelled. Data already sent can't be recalled." operation="consents.revoke" payload={{ id: id(row), expected_revision: revision(row) }} /></article>)}</AsyncView><GrantPermission /></Panel><BackupAndRestore /><LegacyImport /><RetentionPurge /></>;
}

/** What a permission can cover, in the engine's data-class names. */
const dataChoices = [{ label: 'Meeting transcripts', classes: ['meeting_text'] }, { label: 'Your notes', classes: ['human_notes'] }, { label: 'Past meetings, for Ask', classes: ['historical_text', 'historical_excerpts'] }, { label: 'Live audio, for cloud transcription', classes: ['live_audio'] }];

/** Allow sending data to one of your connections. Matches what AI tasks check: the connection's destination, workspace scope. */
function GrantPermission() {
  const connections = items(useResource('entity.list', { collection: 'provider_connections' }).data).filter((connection) => connection.declared_execution !== 'local');
  const [picked, setPicked] = useState<string[]>(['Meeting transcripts', 'Your notes']);
  return <Modal trigger={<button>Grant permission…</button>} title="Grant permission"><CommandForm operation="consents.grant" fields={[{ name: 'purpose', label: 'Purpose', required: true, value: 'AI features' }, { name: 'destination_fingerprint', label: 'Destination', type: 'select', required: true, options: [{ value: '', label: 'Choose a connection' }, ...connections.map((connection) => ({ value: destinationOf(connection), label: `${label(connection)} (${text(connection.endpoint)})` }))] }]} mapPayload={(payload) => {
    const classes = dataChoices.filter((choice) => picked.includes(choice.label)).flatMap((choice) => choice.classes);
    if (classes.length === 0) throw new Error('Choose what may be sent.');
    return { ...payload, data_classes: classes, scope: 'workspace', policy_version: '1' };
  }} confirm="I reviewed this destination and the data it will receive.">
    <fieldset><legend>Data to send</legend>{dataChoices.map((choice) => <label key={choice.label} className={styles.check}><input type="checkbox" checked={picked.includes(choice.label)} onChange={(event) => setPicked(event.target.checked ? [...picked, choice.label] : picked.filter((name) => name !== choice.label))} />{choice.label}</label>)}</fieldset>
  </CommandForm></Modal>;
}

function DiagnosticsExport() {
  const command = useMutation({ mutationFn: async () => { const grant = await nativeRequest('file_grant', { purpose: 'export' }); if (grant.cancelled) return {}; return request('diagnostics.export', { file_grant: grant.grant_id ?? '', confirm: true }); } });
  return <><button disabled={!nativeAvailable() || command.isPending} onClick={() => command.mutate()}>Choose destination and export</button>{command.error && <ErrorNotice error={command.error} />}{command.isSuccess && <Details value={command.data} title="Details" />}</>;
}

function BackupAndRestore() {
  const [includeMedia, setIncludeMedia] = useState(false);
  const [plaintext, setPlaintext] = useState(false);
  const [passphraseRef, setPassphraseRef] = useState('');
  const [restoreRef, setRestoreRef] = useState('');
  const [restoreConfirmed, setRestoreConfirmed] = useState(false);
  const [report, setReport] = useState<RecordData | null>(null);
  const client = useQueryClient();
  const backup = useMutation({ mutationFn: async () => {
    const grant = await nativeRequest('file_grant', { purpose: 'backup' });
    if (grant.cancelled) return {};
    return request('backup.create', { file_grant: grant.grant_id ?? '', include_media: includeMedia, plain_warning_accepted: plaintext, ...(passphraseRef ? { passphrase_secret_ref: passphraseRef } : {}) });
  } });
  const restore = useMutation({ mutationFn: async () => {
    const grant = await nativeRequest('file_grant', { purpose: 'restore' });
    if (grant.cancelled) return {};
    return request('backup.restore', { file_grant: grant.grant_id ?? '', confirm: restoreConfirmed, ...(restoreRef ? { passphrase_secret_ref: restoreRef } : {}) });
  }, onSuccess: async (data) => { setReport(data); await client.invalidateQueries({ queryKey: ['engine'] }); } });
  return <Panel title="Backup & restore">
    <div className={styles.settingsColumns}>
      <section><h3>Create backup</h3>
        <SecureSecret label="Backup passphrase" action="Set passphrase…" onStored={setPassphraseRef} />
        <small>Keep it safe. You need it to restore.</small>
        {passphraseRef && <Badge>Backup will be encrypted</Badge>}
        <label className={styles.check}><input type="checkbox" checked={includeMedia} onChange={(event) => setIncludeMedia(event.target.checked)} />Include retained audio</label>
        <label className={styles.check}><input type="checkbox" checked={plaintext} disabled={!!passphraseRef} onChange={(event) => setPlaintext(event.target.checked)} />Skip encryption (anyone with the file can read it)</label>
        <button disabled={!nativeAvailable() || backup.isPending || (!passphraseRef && !plaintext)} title={passphraseRef || plaintext ? undefined : 'Set a passphrase or skip encryption first'} onClick={() => backup.mutate()}>{backup.isPending ? 'Saving…' : 'Choose destination and back up'}</button>
        {backup.error && <ErrorNotice error={backup.error} />}
        {backup.isSuccess && <Details title="Details" value={backup.data} />}
      </section>
      <section><h3>Restore backup</h3>
        <SecureSecret label="Passphrase, if the backup has one" action="Enter passphrase…" onStored={setRestoreRef} />
        {restoreRef && <Badge>Passphrase stored</Badge>}
        <label className={styles.check}><input type="checkbox" checked={restoreConfirmed} onChange={(event) => setRestoreConfirmed(event.target.checked)} />I understand restoring replaces the current data (a rollback copy is kept).</label>
        <button disabled={!nativeAvailable() || restore.isPending || !restoreConfirmed} title={restoreConfirmed ? undefined : 'Confirm that restoring replaces the current data first'} onClick={() => restore.mutate()}>Choose backup and restore</button>
        {restore.error && <ErrorNotice error={restore.error} />}
        {report && <Details title="Details" value={report} />}
      </section>
    </div>
  </Panel>;
}

function LegacyImport() {
  const [grant, setGrant] = useState<RecordData | null>(null);
  const [report, setReport] = useState<RecordData | null>(null);
  const select = useMutation({ mutationFn: async () => { const selected = await nativeRequest('file_grant', { purpose: 'legacy_import' }); if (selected.cancelled) return {}; setGrant(selected); return request('legacy.preview', { file_grant: selected.grant_id ?? '' }); }, onSuccess: setReport });
  return <Panel title="Import old database"><button disabled={!nativeAvailable() || select.isPending} onClick={() => select.mutate()}>Choose database and inspect</button>{select.error && <ErrorNotice error={select.error} />}{report && <><Details value={report} title="Details" /><CommandForm operation="legacy.import" fixed={{ file_grant: grant?.grant_id ?? '', source_fingerprint: report.source_fingerprint ?? '', confirm: true }} fields={[{ name: 'source_stopped', label: 'The old app is closed', type: 'checkbox', value: false, required: true }]} submit="Import" confirm="I reviewed the report. The original database stays unchanged." onSuccess={setReport} /></>}</Panel>;
}

function RetentionPurge() {
  const [plan, setPlan] = useState<RecordData | null>(null);
  return <Panel title="Purge data">
    <Modal trigger={<button>Choose data to purge…</button>} title="Purge data">
      <CommandForm operation="retention.preview" fields={[{ name: 'category', label: 'Category', type: 'select', value: 'expired_media', options: Object.entries(purgeCategories).map(([value, label]) => ({ value, label })) }]} mapPayload={(payload) => ({ categories: [payload.category ?? 'expired_media'] })} submit="Preview" onSuccess={setPlan} />
      {plan && <><Details title="Details" value={plan} /><CommandForm operation="retention.purge" fixed={{ categories: plan.categories ?? [], confirm: true }} fields={[]} submit="Purge" danger confirm="I understand this cannot be undone." onSuccess={setPlan} /></>}
    </Modal>
  </Panel>;
}
