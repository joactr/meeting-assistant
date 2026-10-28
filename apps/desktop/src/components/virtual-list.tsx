import { useRef, type ReactNode } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { id, type RecordData } from '../lib/data';

export function VirtualList({ rows, render, label, height = 580, estimate = 95 }: { rows: RecordData[]; render: (row: RecordData, index: number) => ReactNode; label: string; height?: number | string; estimate?: number }) {
  const parent = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parent.current,
    estimateSize: () => estimate,
    getItemKey: (index) => id(rows[index] ?? {}) || index,
    overscan: 8,
  });
  return <div ref={parent} role="region" aria-label={label} tabIndex={0} style={{ maxHeight: height, overflow: 'hidden auto', position: 'relative', overflowAnchor: 'none' }}><div role="list" aria-label={label} style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>{virtualizer.getVirtualItems().map((virtualRow) => {
    const row = rows[virtualRow.index];
    return row && <div role="listitem" key={virtualRow.key} ref={virtualizer.measureElement} data-index={virtualRow.index} style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${virtualRow.start}px)` }}>{render(row, virtualRow.index)}</div>;
  })}</div></div>;
}
