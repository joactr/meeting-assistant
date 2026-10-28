import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { SecureSecret } from './providers';
import { id, items, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandForm, ConfirmCommand, Details, Empty, ErrorNotice, Modal, Panel, Toolbar } from '../components/ui';
import styles from './workspace.module.css';

export function PrivacySettings() {
  const consents = useResource('consents.list');
  const [diagnostics, setDiagnostics] = useState<RecordData | null>(null);
  return <><Panel title="Diagnostics"><Toolbar><CommandForm operation="diagnostics.get" fields={[]} submit="View diagnostics" onSuccess={setDiagnostics} /><Modal trigger={<button>Export support bundle…</button>} title="Export support bundle"><DiagnosticsExport /></Modal></Toolbar>{diagnostics && <Details value={diagnostics} title="Details" />}</Panel><Panel title="Outbound permissions"><AsyncView query={consents}>{(data) => items(data).length === 0 ? <Empty title="No permissions granted" /> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><strong>{text(row.purpose)}</strong><p>{text(row.destination_fingerprint)}</p><Badge>{row.revoked_at ? 'Revoked' : 'Active'}</Badge><Details value={row} title="Details" /></div><ConfirmCommand trigger="Revoke consent" title="Revoke permission?" message="Queued network work is cancelled. Data already sent can't be recalled." operation="consents.revoke" payload={{ id: id(row), expected_revision: revision(row) }} /></article>)}</AsyncView><Modal trigger={<button>Grant permission…</button>} title="Grant permission"><CommandForm operation="consents.grant" fields={[{ name: 'purpose', label: 'Purpose', required: true }, { name: 'destination_fingerprint', label: 'Destination fingerprint', required: true }, { name: 'data_classes', label: 'Data classes (JSON)', type: 'json', value: [], required: true }, { name: 'scope', label: 'Scope (JSON)', type: 'json', value: {}, required: true }, { name: 'policy_version', label: 'Policy version', value: '1', required: true }]} confirm="I reviewed this destination, data classes and scope." /></Modal></Panel><BackupAndRestore /><LegacyImport /><RetentionPurge /></>;
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
        <SecureSecret label="Backup passphrase" onStored={setPassphraseRef} />
        <small>Keep it safe. You need it to restore.</small>
        {passphraseRef && <Badge>Backup will be encrypted</Badge>}
        <label className={styles.check}><input type="checkbox" checked={includeMedia} onChange={(event) => setIncludeMedia(event.target.checked)} />Include retained audio</label>
        <label className={styles.check}><input type="checkbox" checked={plaintext} disabled={!!passphraseRef} onChange={(event) => setPlaintext(event.target.checked)} />Skip encryption (anyone with the file can read it)</label>
        <button disabled={!nativeAvailable() || backup.isPending || (!passphraseRef && !plaintext)} onClick={() => backup.mutate()}>{backup.isPending ? 'Saving…' : 'Choose destination and back up'}</button>
        {backup.error && <ErrorNotice error={backup.error} />}
        {backup.isSuccess && <Details title="Details" value={backup.data} />}
      </section>
      <section><h3>Restore backup</h3>
        <SecureSecret label="Passphrase, if the backup has one" onStored={setRestoreRef} />
        {restoreRef && <Badge>Passphrase stored</Badge>}
        <label className={styles.check}><input type="checkbox" checked={restoreConfirmed} onChange={(event) => setRestoreConfirmed(event.target.checked)} />I understand restoring replaces the current data (a rollback copy is kept).</label>
        <button disabled={!nativeAvailable() || restore.isPending || !restoreConfirmed} onClick={() => restore.mutate()}>Choose backup and restore</button>
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
      <CommandForm operation="retention.preview" fields={[{ name: 'category', label: 'Category', type: 'select', value: 'expired_media', options: ['expired_media', 'candidates', 'diagnostics', 'events', 'cache', 'voice_profiles'].map((value) => ({ value, label: value.replaceAll('_', ' ') })) }]} mapPayload={(payload) => ({ categories: [payload.category ?? 'expired_media'] })} submit="Preview" onSuccess={setPlan} />
      {plan && <><Details title="Details" value={plan} /><CommandForm operation="retention.purge" fixed={{ categories: plan.categories ?? [], confirm: true }} fields={[]} submit="Purge" confirm="I understand this cannot be undone." onSuccess={setPlan} /></>}
    </Modal>
  </Panel>;
}
