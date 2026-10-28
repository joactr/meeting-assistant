import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { useWindowVirtualizer } from '@tanstack/react-virtual';
import { id, type RecordData } from '../lib/data';

/** Where a window-scrolled list starts on the page. Lists scroll with the page: one scrollbar. */
export function useScrollMargin(ref: RefObject<HTMLElement | null>): number {
  const [margin, setMargin] = useState(0);
  useLayoutEffect(() => {
    const top = ref.current ? Math.round(ref.current.getBoundingClientRect().top + window.scrollY) : 0;
    if (top !== margin) setMargin(top);
  });
  return margin;
}

export function VirtualList({ rows, render, label, estimate = 95 }: { rows: RecordData[]; render: (row: RecordData, index: number) => ReactNode; label: string; estimate?: number }) {
  const parent = useRef<HTMLDivElement>(null);
  const scrollMargin = useScrollMargin(parent);
  const virtualizer = useWindowVirtualizer({
    count: rows.length,
    estimateSize: () => estimate,
    getItemKey: (index) => id(rows[index] ?? {}) || index,
    overscan: 8,
    scrollMargin,
  });
  return <div ref={parent} role="region" aria-label={label} style={{ overflowAnchor: 'none' }}><div role="list" aria-label={label} style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>{virtualizer.getVirtualItems().map((virtualRow) => {
    const row = rows[virtualRow.index];
    return row && <div role="listitem" key={virtualRow.key} ref={virtualizer.measureElement} data-index={virtualRow.index} style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${virtualRow.start - scrollMargin}px)` }}>{render(row, virtualRow.index)}</div>;
  })}</div></div>;
}
