import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import * as Menu from '@radix-ui/react-dropdown-menu';
import { ChevronDown, MoreHorizontal, SlidersHorizontal, X } from 'lucide-react';
import { useResource } from '../lib/queries';
import { id, items, label, text, type RecordData } from '../lib/data';
import styles from './controls.module.css';

export interface MenuAction {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}

/**
 * Overflow menu for secondary actions. Radix supplies the roving focus, typeahead, Escape and
 * focus-return behaviour of the WAI-ARIA menu button pattern. Destructive actions sit after a separator.
 */
export function ActionMenu({ label: menuLabel, actions }: { label: string; actions: MenuAction[] }) {
  const safe = actions.filter((action) => !action.danger);
  const danger = actions.filter((action) => action.danger);
  return <Menu.Root>
    <Menu.Trigger asChild><button type="button" className={styles.iconButton} aria-label={menuLabel}><MoreHorizontal size={18} aria-hidden="true" /></button></Menu.Trigger>
    <Menu.Portal><Menu.Content className={styles.menu} align="end" sideOffset={6}>
      {safe.map((action) => <Menu.Item key={action.label} className={styles.menuItem} disabled={action.disabled} onSelect={action.onSelect}>{action.label}</Menu.Item>)}
      {safe.length > 0 && danger.length > 0 && <Menu.Separator className={styles.menuSeparator} />}
      {danger.map((action) => <Menu.Item key={action.label} className={`${styles.menuItem} ${styles.menuDanger}`} disabled={action.disabled} onSelect={action.onSelect}>{action.label}</Menu.Item>)}
    </Menu.Content></Menu.Portal>
  </Menu.Root>;
}

/** Primary `bar` filters share one row with the toggle; secondary `children` render on the row below. */
export function FilterDisclosure({ bar, children, count = 0, title = 'Filters' }: { bar?: ReactNode; children: ReactNode; count?: number; title?: string }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const toggle = <button type="button" className={styles.disclosureToggle} aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)}>
    <SlidersHorizontal size={15} aria-hidden="true" />{title}{count > 0 && <span className={styles.count}>{count}</span>}<ChevronDown size={15} aria-hidden="true" className={open ? styles.flipped : undefined} />
  </button>;
  return <div className={styles.disclosure}>
    {bar ? <div className={styles.disclosureBar}>{bar}{toggle}</div> : toggle}
    <div id={panelId} hidden={!open} className={styles.disclosurePanel}>{children}</div>
  </div>;
}

interface PickerProps {
  title: string;
  values: string[];
  onChange: (values: string[]) => void;
  multiple?: boolean;
  projectId?: string;
  placeholder?: string;
}

/**
 * Meeting combobox (WAI-ARIA 1.2 editable combobox with list autocomplete). Replaces free-text meeting-ID
 * fields: people search by title and the engine receives the real ID. `multiple` renders removable chips.
 */
export function MeetingPicker({ title, values, onChange, multiple = false, projectId, placeholder = 'Search meetings by title' }: PickerProps) {
  const inputId = useId();
  const listId = useId();
  const input = useRef<HTMLInputElement>(null);
  const meetings = useResource('meetings.list', { limit: 100, ...(projectId ? { project_id: projectId } : {}) });
  const rows = items(meetings.data);
  const byId = useMemo(() => new Map(rows.map((row) => [id(row), row])), [rows]);
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const needle = query.trim().toLowerCase();
  const choices = rows.filter((row) => !values.includes(id(row)) && (!needle || label(row).toLowerCase().includes(needle)));
  const single = !multiple && values.length === 1 ? byId.get(values[0] ?? '') : undefined;
  const shown = open ? query : single ? label(single) : '';
  const optionId = (index: number): string => `${listId}-${index}`;
  const pick = (row: RecordData): void => {
    onChange(multiple ? [...values, id(row)] : [id(row)]);
    setQuery(''); setOpen(false); setActive(0);
  };
  const keys = (event: KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); setActive((value) => Math.min(value + 1, choices.length - 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive((value) => Math.max(value - 1, 0)); }
    else if (event.key === 'Enter' && open && choices[active]) { event.preventDefault(); pick(choices[active]); }
    else if (event.key === 'Escape' && open) { event.stopPropagation(); setOpen(false); setQuery(''); }
    else if (event.key === 'Backspace' && !query && multiple && values.length) onChange(values.slice(0, -1));
  };
  return <div className={styles.picker}>
    <label htmlFor={inputId}>{title}</label>
    <div className={styles.pickerBox}>
      {multiple && values.map((value) => <span key={value} className={styles.chip}>{label(byId.get(value) ?? { id: value })}<button type="button" aria-label={`Remove ${label(byId.get(value) ?? { id: value })}`} onClick={() => onChange(values.filter((entry) => entry !== value))}><X size={12} aria-hidden="true" /></button></span>)}
      <input
        ref={input} id={inputId} role="combobox" aria-expanded={open} aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={open && choices[active] ? optionId(active) : undefined}
        autoComplete="off" value={shown} placeholder={values.length && multiple ? '' : placeholder}
        onChange={(event) => { setQuery(event.target.value); setOpen(true); setActive(0); }}
        onFocus={() => setOpen(true)} onBlur={() => { setOpen(false); setQuery(''); }} onKeyDown={keys}
      />
      {!multiple && values.length > 0 && <button type="button" className={styles.clear} aria-label={`Clear ${title}`} onClick={() => { onChange([]); input.current?.focus(); }}><X size={14} aria-hidden="true" /></button>}
    </div>
    <ul id={listId} role="listbox" aria-label={title} hidden={!open} className={styles.options}>
      {meetings.isPending ? <li className={styles.optionNote}>Loading meetings…</li> : choices.length === 0 ? <li className={styles.optionNote}>No matching meetings</li> : choices.slice(0, 50).map((row, index) =>
        <li key={id(row)} id={optionId(index)} role="option" aria-selected={index === active} className={index === active ? styles.optionActive : undefined} onMouseDown={(event) => { event.preventDefault(); pick(row); }} onMouseEnter={() => setActive(index)}>
          <strong>{label(row)}</strong><small>{text(row.project_name)}</small>
        </li>)}
    </ul>
  </div>;
}

/** Single-choice filter rendered as a segmented control. Native radios keep arrow-key navigation and screen-reader semantics. */
export function Segmented({ legend, value, options, onChange }: { legend: string; value: string; options: { value: string; label: string }[]; onChange: (value: string) => void }) {
  const name = useId();
  return <fieldset className={styles.segmented}>
    <legend className={styles.srOnly}>{legend}</legend>
    {options.map((option) => <label key={option.value} className={value === option.value ? styles.segmentActive : undefined}>
      <input type="radio" name={name} value={option.value} checked={value === option.value} onChange={() => onChange(option.value)} />{option.label}
    </label>)}
  </fieldset>;
}
