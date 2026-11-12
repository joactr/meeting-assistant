import { createContext, useContext, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Tabs from '@radix-ui/react-tabs';
import { X } from 'lucide-react';
import type { UseQueryResult } from '@tanstack/react-query';
import { ApplicationError, nativeAvailable } from '../lib/bridge';
import { useCommand, useNativeResource } from '../lib/queries';
import { updatePreferences } from '../lib/preferences';
import { object, pretty, safeParse, text, type Json, type Payload, type RecordData } from '../lib/data';
import styles from './ui.module.css';

/** Class names for plain `<button>`s: default buttons are secondary (ink outline); these give the primary, quiet or danger look. */
export const buttonClass = { primary: styles.primary, quiet: styles.quiet, danger: styles.dangerButton };
/** True inside a dialog: a form there owns the screen's one primary action; on a page its submit stays secondary. */
const InDialog = createContext<{ close: () => void } | null>(null);

/** The top nav already names the page, so the h1 is visually hidden (still read aloud) unless `showTitle`; actions sit in a slim right-aligned row. */
export function PageHeader({ title, children, showTitle = false }: { title: string; children?: ReactNode; showTitle?: boolean }) {
  return <header className={`${styles.pageHeader} ${showTitle ? '' : styles.compactHeader}`}><h1 className={showTitle ? undefined : 'sr-only'}>{title}</h1>{(showTitle || children) && <div className={styles.toolbar}>{children}</div>}</header>;
}

export function Panel({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return <section className={styles.panel}>{(title || actions) && <header className={styles.panelHeader}><h2>{title}</h2><div className={styles.toolbar}>{actions}</div></header>}{children}</section>;
}

export function Toolbar({ children }: { children: ReactNode }) { return <div className={styles.toolbar}>{children}</div>; }
export function Grid({ children }: { children: ReactNode }) { return <div className={styles.grid}>{children}</div>; }
export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'success' | 'warning' | 'danger' }) { return <span className={`${styles.badge} ${styles[tone]}`}>{children}</span>; }
export function Empty({ title = 'Nothing here yet', children }: { title?: string; children?: ReactNode }) { return <div className={styles.empty}><strong>{title}</strong>{children}</div>; }
export function Notice({ children, danger = false }: { children: ReactNode; danger?: boolean }) { return <div role={danger ? 'alert' : 'status'} className={`${styles.notice} ${danger ? styles.danger : ''}`}>{children}</div>; }

export function ErrorNotice({ error, retry, children }: { error: unknown; retry?: () => void; children?: ReactNode }) {
  const failure = error instanceof ApplicationError ? error : new ApplicationError(error);
  const conflict = failure.code.includes('conflict') || failure.code.includes('revision');
  const details = Object.entries(failure.details).filter(([, value]) => value !== null && value !== '');
  return <div role="alert" className={`${styles.notice} ${styles.danger}`}><strong>{conflict ? 'Changed elsewhere. Your changes have not been discarded.' : 'Something went wrong'}</strong><p>{failure.message}</p>{(details.length > 0 || failure.traceId) && <details className={styles.details}><summary>Technical details</summary>{details.map(([key, value]) => <small key={key}>{key.replaceAll('_', ' ')}: {typeof value === 'string' ? value : JSON.stringify(value)}</small>)}{failure.traceId && <small>Trace: {failure.traceId}</small>}</details>}{children}{retry && <button type="button" onClick={retry}>{conflict ? 'Reload saved version' : 'Try again'}</button>}</div>;
}

export function AsyncView({ query, children, empty, isEmpty = false }: { query: Pick<UseQueryResult<RecordData>, 'isPending' | 'isError' | 'error' | 'data' | 'isFetching'> & { refetch: () => unknown }; children: (data: RecordData) => ReactNode; empty?: ReactNode; isEmpty?: boolean }) {
  if (!nativeAvailable()) return <Empty title="Desktop connection unavailable" />;
  if (query.isPending) return <div className={styles.loading} role="status">Loading…</div>;
  if (query.isError) return <ErrorNotice error={query.error} retry={() => { void query.refetch(); }} />;
  if (isEmpty) return <>{empty ?? <Empty />}</>;
  return <div className={styles.view}>{children(query.data ?? {})}</div>;
}

/** What to refocus when a dialog closes: the opener, or the menu trigger when the opener was a menu item that unmounts. */
function openerOf(): HTMLElement | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || active === document.body) return null;
  const menu = active.closest('[role=menu]');
  if (!menu) return active;
  return (menu.id ? document.querySelector<HTMLElement>(`[aria-controls="${menu.id}"]`) : null) ?? document.querySelector<HTMLElement>('[aria-expanded=true]') ?? active;
}

export function Modal({ trigger, title, description, children, open, onOpenChange }: { trigger?: ReactNode; title: string; description?: string; children: ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const [own, setOwn] = useState(false);
  const isOpen = open ?? own;
  const setOpen = (value: boolean): void => { if (open === undefined) setOwn(value); onOpenChange?.(value); };
  const opener = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  if (isOpen && !wasOpen.current) opener.current = openerOf();
  wasOpen.current = isOpen;
  const restoreFocus = (event: Event): void => {
    const target = opener.current;
    if (target?.isConnected) { event.preventDefault(); target.focus(); }
  };
  return <Dialog.Root open={isOpen} onOpenChange={setOpen}>{trigger && <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>}<Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.modal} onCloseAutoFocus={restoreFocus} {...(description ? {} : { 'aria-describedby': undefined })}><div className={styles.modalBody}><Dialog.Title>{title}</Dialog.Title>{description && <Dialog.Description className={styles.description}>{description}</Dialog.Description>}<InDialog.Provider value={{ close: () => setOpen(false) }}>{children}</InDialog.Provider></div><Dialog.Close className={styles.close} aria-label="Close dialog"><X size={18} aria-hidden="true" /></Dialog.Close></Dialog.Content></Dialog.Portal></Dialog.Root>;
}

export function TabSet({ tabs, value, onValueChange, label = 'Workspace panels' }: { tabs: { label: string; content: ReactNode }[]; value?: string; onValueChange?: (value: string) => void; label?: string }) {
  const key = tabs.map((tab) => tab.label).join('|');
  const preferences = useNativeResource('native_preferences_get');
  const persisted = text(object(object(preferences.data?.preferences ?? preferences.data).selected_tabs)[key]);
  const [local, setLocal] = useState(value ?? tabs[0]?.label ?? '');
  const [saveError, setSaveError] = useState<unknown>();
  // Restore the saved tab once, before the user picks one. Later refetches (window focus, other saves) can carry an
  // older tab than the click still being saved; applying them bounced the user back to the previous tab.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || !persisted) return;
    restored.current = true;
    if (tabs.some((tab) => tab.label === persisted)) {
      setLocal(persisted);
      onValueChange?.(persisted);
    }
  }, [persisted]);
  const change = (next: string): void => {
    restored.current = true;
    const previous = value ?? local;
    setLocal(next);
    onValueChange?.(next);
    if (nativeAvailable()) void updatePreferences({ selected_tabs: { [key]: next } }).catch((error: unknown) => {
      setLocal(previous);
      onValueChange?.(previous);
      setSaveError(error);
    });
  };
  return <Tabs.Root className={styles.tabSet} value={value ?? local} onValueChange={change}>{Boolean(saveError) && <ErrorNotice error={saveError} />}<Tabs.List className={styles.tabs} aria-label={label}>{tabs.map((tab) => <Tabs.Trigger key={tab.label} value={tab.label}>{tab.label}</Tabs.Trigger>)}</Tabs.List>{tabs.map((tab) => <Tabs.Content className={styles.tabContent} tabIndex={-1} key={tab.label} value={tab.label}>{tab.content}</Tabs.Content>)}</Tabs.Root>;
}

export interface Field {
  name: string;
  label: string;
  type?: 'text' | 'textarea' | 'number' | 'select' | 'checkbox' | 'date' | 'datetime-local' | 'time' | 'json';
  value?: Json;
  required?: boolean;
  options?: { value: string; label: string }[];
  min?: number;
  max?: number;
  advanced?: boolean;
  /** Free-text suggestions shown as a native datalist. */
  suggestions?: string[];
  /** Grey the field out while the form's current values make it irrelevant (e.g. a toggle is off). */
  disabledWhen?: (values: Payload) => boolean;
}

export function FieldInput({ field, value, onChange, disabled = false }: { field: Field; value: Json; onChange: (value: Json) => void; disabled?: boolean }) {
  const inputId = useId();
  const props = { id: inputId, name: field.name, required: field.required, disabled, list: field.suggestions ? `${inputId}-list` : undefined };
  return <div className={styles.field}><label htmlFor={inputId}>{field.label}{field.required && <span aria-hidden="true"> *</span>}</label>{field.type === 'checkbox' ? <input {...props} type="checkbox" checked={value === true || value === 1} onChange={(event) => onChange(event.target.checked)} /> : field.type === 'select' ? <select {...props} value={text(value)} onChange={(event) => onChange(event.target.value)}>{field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select> : field.type === 'textarea' || field.type === 'json' ? <textarea {...props} rows={field.type === 'json' ? 7 : 3} spellCheck={field.type !== 'json'} value={text(value)} onChange={(event) => onChange(event.target.value)} /> : <input {...props} type={field.type ?? 'text'} step={field.type === 'number' ? 'any' : undefined} min={field.min} max={field.max} value={text(value)} onChange={(event) => onChange(field.type === 'number' && event.target.value !== '' ? Number(event.target.value) : event.target.value)} />}{field.suggestions && <datalist id={`${inputId}-list`}>{field.suggestions.map((name) => <option key={name} value={name} />)}</datalist>}</div>;
}

export function CommandForm({ operation, fields, fixed = {}, submit = 'Save changes', onSuccess, confirm, children, mapPayload, danger = false }: { operation: string; fields: Field[]; fixed?: Payload; submit?: string; onSuccess?: (result: RecordData) => void; confirm?: string; children?: ReactNode; mapPayload?: (payload: Payload) => Payload; danger?: boolean }) {
  const command = useCommand(operation);
  const dialog = useContext(InDialog);
  const [values, setValues] = useState<Payload>(() => Object.fromEntries(fields.map((field) => [field.name, field.type === 'json' ? pretty(field.value ?? {}) : field.value ?? (field.type === 'checkbox' ? false : '')])));
  const [localError, setLocalError] = useState<unknown>();
  const [confirmed, setConfirmed] = useState(false);
  const submitForm = (event: FormEvent): void => {
    event.preventDefault();
    setLocalError(undefined);
    try {
      const payload: Payload = { ...fixed };
      for (const field of fields) {
        const value = values[field.name] ?? null;
        payload[field.name] = field.type === 'json' ? safeParse(text(value)) : value === '' && !field.required ? null : value;
      }
      let outgoing = mapPayload ? mapPayload(payload) : payload;
      if (operation === 'entity.create' || operation === 'entity.update') {
        const { collection, id: entityId, expected_revision, ...data } = outgoing;
        outgoing = { collection: collection ?? '', ...(entityId ? { id: entityId } : {}), ...(expected_revision != null ? { expected_revision } : {}), data };
      }
      command.mutate(outgoing, { onSuccess: (result) => { if (onSuccess) onSuccess(result); else dialog?.close(); } });
    } catch (error) { setLocalError(error); }
  };
  const input = (field: Field) => <FieldInput key={field.name} field={field} value={values[field.name] ?? null} disabled={field.disabledWhen?.(values) ?? false} onChange={(value) => { setValues((current) => ({ ...current, [field.name]: value })); command.reset(); }} />;
  return <form className={styles.form} onSubmit={submitForm}>{fields.filter((field) => !field.advanced).map(input)}{fields.some((field) => field.advanced) && <details className={styles.details}><summary>More options</summary><div className={styles.form}>{fields.filter((field) => field.advanced).map(input)}</div></details>}{children}{confirm && <label className={styles.check}><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} required />{confirm}</label>}{Boolean(command.error || localError) && <ErrorNotice error={command.error ?? localError} />}{command.isSuccess && <Notice>Saved.</Notice>}<button className={danger ? styles.dangerButton : dialog ? styles.primary : undefined} disabled={command.isPending || !nativeAvailable() || (!!confirm && !confirmed)} type="submit">{command.isPending ? 'Working…' : submit}</button></form>;
}
export function CommandButton({ operation, payload = {}, children, danger = false, disabled = false, onSuccess }: { operation: string; payload?: Payload; children: ReactNode; danger?: boolean; disabled?: boolean; onSuccess?: (result: RecordData) => void }) {
  const command = useCommand(operation);
  return <span className={styles.command}><button type="button" className={danger ? styles.dangerButton : undefined} disabled={disabled || command.isPending || !nativeAvailable()} onClick={() => command.mutate(payload, { onSuccess })}>{command.isPending ? 'Working…' : children}</button>{command.error && <ErrorNotice error={command.error} />}</span>;
}

/** Open state that is either owned locally or driven by a parent (for example an overflow menu item). */
export function useOpenState(open?: boolean, onOpenChange?: (open: boolean) => void): { open: boolean; setOpen: (open: boolean) => void; controlled: boolean } {
  const [own, setOwn] = useState(false);
  const controlled = open !== undefined;
  return { open: controlled ? open : own, setOpen: (value) => { if (controlled) onOpenChange?.(value); else setOwn(value); }, controlled };
}

export function ConfirmCommand({ trigger, title, message, operation, payload, onSuccess, open: controlledOpen, onOpenChange }: { trigger: string; title: string; message: string; operation: string; payload: Payload; onSuccess?: (result: RecordData) => void; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const { open, setOpen, controlled } = useOpenState(controlledOpen, onOpenChange);
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button className={styles.dangerButton}>{trigger}</button>} title={title} description={message}><CommandForm operation={operation} fixed={{ ...payload, confirm: true }} fields={[]} submit={trigger.replace(/…$/, '')} danger confirm="I understand." onSuccess={(result) => { setOpen(false); onSuccess?.(result); }} /></Modal>;
}

export function Details({ value, title = 'Details' }: { value: unknown; title?: string }) {
  const data = object(value);
  if (Object.values(data).every((entry) => entry == null || entry === '' || (Array.isArray(entry) && entry.length === 0))) return null;
  return <details className={styles.details}><summary>{title}</summary><pre>{pretty(data)}</pre></details>;
}

export function EditResource({ operation, row, fields, fixed = {}, label = 'Edit', open: controlledOpen, onOpenChange, mapPayload }: { operation: string; row: RecordData; fields: Field[]; fixed?: Payload; label?: string; open?: boolean; onOpenChange?: (open: boolean) => void; mapPayload?: (payload: Payload) => Payload }) {
  const { open, setOpen, controlled } = useOpenState(controlledOpen, onOpenChange);
  return <Modal open={open} onOpenChange={setOpen} trigger={controlled ? undefined : <button>{label}</button>} title={label}><CommandForm operation={operation} fixed={{ id: row.id ?? '', expected_revision: row.revision ?? 1, ...fixed }} fields={fields.map((field) => ({ ...field, value: row[field.name] ?? field.value }))} mapPayload={mapPayload} onSuccess={() => setOpen(false)} /></Modal>;
}
