import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from '../lib/bridge';
import { useResource } from '../lib/queries';
import { SecureSecret } from './providers';
import { id, items, revision, text, type RecordData } from '../lib/data';
import { AsyncView, Badge, CommandForm, ConfirmCommand, Details, Empty, ErrorNotice, Modal, Notice, Panel, Toolbar } from '../components/ui';
import styles from './workspace.module.css';

export function PrivacySettings() {
  const consents = useResource('consents.list');
  const [diagnostics, setDiagnostics] = useState<RecordData | null>(null);
  return <><Panel title="Your data stays under your control"><Notice>No analytics. Diagnostics stay on device unless you explicitly export a support bundle. The local database is not encrypted by this app: OS account/filesystem/full-disk protection applies. People with access to your account can read local notes and media. Credentials use the OS vault.</Notice><Toolbar><CommandForm operation="diagnostics.get" fields={[]} submit="Inspect safe diagnostics" onSuccess={setDiagnostics} /><Modal trigger={<button>Export support bundle…</button>} title="Explicit diagnostics export" description="Review the redacted local diagnostics before choosing a destination. No recordings, provider credentials or raw request bodies are included by default."><DiagnosticsExport /></Modal></Toolbar>{diagnostics && <Details value={diagnostics} title="Redacted local diagnostics" />}</Panel><Panel title="Outbound processing consent"><AsyncView query={consents}>{(data) => items(data).length === 0 ? <Empty title="No outbound consent granted">Cloud processing and remote workflow delivery stay disconnected until explicitly configured.</Empty> : items(data).map((row) => <article className={styles.actionRow} key={id(row)}><div><strong>{text(row.purpose)}</strong><p>{text(row.destination_fingerprint)}</p><Badge>{row.revoked_at ? 'Revoked' : 'Active'}</Badge><Details value={row} title="Data classes, scope and policy version" /></div><ConfirmCommand trigger="Revoke consent" title="Revoke this outbound permission?" message="Queued/active dependent network work is cancelled. Remote processing that already occurred cannot be undone." operation="consents.revoke" payload={{ id: id(row), expected_revision: revision(row) }} /></article>)}</AsyncView><Modal trigger={<button>Grant reviewed destination permission…</button>} title="Explicit outbound permission"><CommandForm operation="consents.grant" fields={[{ name: 'purpose', label: 'Purpose', required: true }, { name: 'destination_fingerprint', label: 'Exact configured destination fingerprint', required: true }, { name: 'data_classes', label: 'Authorized data classes (JSON)', type: 'json', value: [], required: true }, { name: 'scope', label: 'Selected scope (JSON; exact connection/project/meeting)', type: 'json', value: {}, required: true }, { name: 'policy_version', label: 'Privacy policy version', value: '1', required: true }]} confirm="I reviewed this destination, data classes and exact scope. This does not authorize unrelated cloud processing." /></Modal></Panel><BackupAndRestore /><LegacyImport /><RetentionPurge /></>;
}

function DiagnosticsExport() {
  const command = useMutation({ mutationFn: async () => { const grant = await nativeRequest('file_grant', { purpose: 'export' }); if (grant.cancelled) return {}; return request('diagnostics.export', { file_grant: grant.grant_id ?? '', confirm: true }); } });
  return <><button disabled={!nativeAvailable() || command.isPending} onClick={() => command.mutate()}>Choose destination and export redacted bundle</button>{command.error && <ErrorNotice error={command.error} />}{command.isSuccess && <Details value={command.data} title="Explicit export receipt" />}</>;
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
  return <Panel title="Portable backup & safe restore">
    <div className={styles.settingsColumns}>
      <section><h3>Create backup</h3>
        <SecureSecret label="New age backup passphrase (retain it independently for restore)" onStored={setPassphraseRef} />
        {passphraseRef && <Badge>Native passphrase stored — backup will be encrypted</Badge>}
        <label className={styles.check}><input type="checkbox" checked={includeMedia} onChange={(event) => setIncludeMedia(event.target.checked)} />Include retained audio</label>
        <label className={styles.check}><input type="checkbox" checked={plaintext} disabled={!!passphraseRef} onChange={(event) => setPlaintext(event.target.checked)} />Without encryption, I explicitly accept that anyone reading this backup can read meeting text and audio.</label>
        <button disabled={!nativeAvailable() || backup.isPending || (!passphraseRef && !plaintext)} onClick={() => backup.mutate()}>{backup.isPending ? 'Snapshotting and saving…' : 'Choose destination & create backup'}</button>
        {backup.error && <ErrorNotice error={backup.error} />}
        {backup.isSuccess && <Details title="Backup manifest, hashes and saved receipt" value={backup.data} />}
      </section>
      <section><h3>Validate & restore backup</h3>
        <SecureSecret label="Passphrase for an encrypted age backup (omit for plaintext)" onStored={setRestoreRef} />
        {restoreRef && <Badge>Native restore passphrase stored</Badge>}
        <label className={styles.check}><input type="checkbox" checked={restoreConfirmed} onChange={(event) => setRestoreConfirmed(event.target.checked)} />I understand restore requires exclusive access and switches to separately validated data with a rollback workspace retained.</label>
        <button disabled={!nativeAvailable() || restore.isPending || !restoreConfirmed} onClick={() => restore.mutate()}>Choose backup to validate and restore</button>
        {restore.error && <ErrorNotice error={restore.error} />}
        {report && <Details title="Restore integrity, activation and rollback report" value={report} />}
      </section>
    </div>
  </Panel>;
}

function LegacyImport() {
  const [grant, setGrant] = useState<RecordData | null>(null);
  const [report, setReport] = useState<RecordData | null>(null);
  const select = useMutation({ mutationFn: async () => { const selected = await nativeRequest('file_grant', { purpose: 'legacy_import' }); if (selected.cancelled) return {}; setGrant(selected); return request('legacy.preview', { file_grant: selected.grant_id ?? '' }); }, onSuccess: setReport });
  return <Panel title="Explicit legacy 0.1.x import"><button disabled={!nativeAvailable() || select.isPending} onClick={() => select.mutate()}>Choose legacy database and inspect</button>{select.error && <ErrorNotice error={select.error} />}{report && <><Details value={report} title="Entity counts, rejected rows, conflicts and source fingerprint" /><CommandForm operation="legacy.import" fixed={{ file_grant: grant?.grant_id ?? '', source_fingerprint: report.source_fingerprint ?? '', confirm: true }} fields={[{ name: 'source_stopped', label: 'The source application is stopped for final cutover', type: 'checkbox', value: false, required: true }]} submit="Import reviewed source" confirm="I reviewed the source fingerprint and import report. The original database must remain unchanged." onSuccess={setReport} /></>}</Panel>;
}

function RetentionPurge() {
  const [plan, setPlan] = useState<RecordData | null>(null);
  return <Panel title="Retention & category purge">
    <Notice>Automatic audio retention never removes transcripts. SSD unlink/vacuum is not secure erasure. Purging cannot erase remote recipients or independent backups.</Notice>
    <Modal trigger={<button>Review category purge…</button>} title="Review local deletion scope">
      <CommandForm operation="retention.preview" fields={[{ name: 'category', label: 'Category', type: 'select', value: 'expired_media', options: ['expired_media', 'candidates', 'diagnostics', 'events', 'cache', 'voice_profiles'].map((value) => ({ value, label: value.replaceAll('_', ' ') })) }]} mapPayload={(payload) => ({ categories: [payload.category ?? 'expired_media'] })} submit="Preview affected data" onSuccess={setPlan} />
      {plan && <><Details title="Reviewed category, counts, bytes and deletion warning" value={plan} /><CommandForm operation="retention.purge" fixed={{ categories: plan.categories ?? [], confirm: true }} fields={[]} submit="Purge reviewed local category" confirm="I reviewed these counts and understand this cannot be undone or erase external copies." onSuccess={setPlan} /></>}
    </Modal>
  </Panel>;
}
