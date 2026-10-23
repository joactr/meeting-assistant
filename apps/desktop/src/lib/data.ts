export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type RecordData = { [key: string]: Json };
export type Payload = RecordData;

export function object(value: unknown): RecordData {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as RecordData)
    : {};
}

export function array(value: unknown): RecordData[] {
  return Array.isArray(value) ? value.map(object) : [];
}

export function items(value: unknown): RecordData[] {
  const data = object(value);
  return array(data.items ?? data.results ?? data.devices ?? (Array.isArray(value) ? value : []));
}

export function item(value: unknown): RecordData {
  const data = object(value);
  return object(data.item ?? data);
}

export function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : fallback;
}

export function number(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

export function id(row: RecordData): string {
  return text(row.id ?? row.entity_id ?? row.speaker_id ?? row.utterance_id ?? row.start_utterance_id ?? row.meeting_id);
}

export function revision(row: RecordData): number {
  return number(row.revision, 1);
}

export function label(row: RecordData): string {
  return text(row.title ?? row.display_name ?? row.name ?? row.description ?? row.spelling ?? row.heading ?? row.id, 'Untitled');
}

export function clock(milliseconds: unknown): string {
  const seconds = Math.max(0, Math.floor(number(milliseconds) / 1000));
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
}

export function date(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'Not set';
  const parsed = new Date(typeof value === 'number' ? value : text(value));
  return Number.isNaN(parsed.valueOf()) ? text(value) : parsed.toLocaleString();
}

export function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? '';
}

export function safeParse(value: string): Json {
  return JSON.parse(value) as Json;
}

/** Compact timestamp for lists: "Today 14:55", "Yesterday 09:10", "Tue 11:30", else "3 Oct" (year only when different). */
export function when(value: unknown, now: Date = new Date()): string {
  if (value === null || value === undefined || value === '') return 'Not set';
  const parsed = new Date(typeof value === 'number' ? value : text(value));
  if (Number.isNaN(parsed.valueOf())) return text(value);
  const day = (stamp: Date): number => new Date(stamp.getFullYear(), stamp.getMonth(), stamp.getDate()).valueOf();
  const age = Math.round((day(now) - day(parsed)) / 86_400_000);
  const time = parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (age === 0) return `Today ${time}`;
  if (age === 1) return `Yesterday ${time}`;
  if (age > 1 && age < 7) return `${parsed.toLocaleDateString([], { weekday: 'short' })} ${time}`;
  const sameYear = parsed.getFullYear() === now.getFullYear();
  return `${parsed.toLocaleDateString([], { day: 'numeric', month: 'short', ...(sameYear ? {} : { year: 'numeric' }) })} ${time}`;
}
