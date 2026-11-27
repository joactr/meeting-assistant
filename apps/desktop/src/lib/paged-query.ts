import { useEffect } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { nativeAvailable, request } from './bridge';
import { items, text, type Payload, type RecordData } from './data';

export function useCursorResource(operationId: string, payload: Payload) {
  return useInfiniteQuery({
    // Own key: an infinite query must never share cache data with a plain useResource of the same payload.
    queryKey: ['engine', operationId, payload, 'pages'],
    initialPageParam: '',
    queryFn: ({ pageParam }) => request(operationId, { ...payload, ...(pageParam ? { cursor: pageParam } : {}) }),
    getNextPageParam: (lastPage) => text(lastPage.next_cursor) || undefined,
    enabled: nativeAvailable(),
    staleTime: 10_000,
  });
}

/** Every meeting, fetched page by page, so pickers and title lookups never stop at the first page. */
export function useAllMeetings(payload: Payload = {}): { rows: RecordData[]; isPending: boolean } {
  const query = useCursorResource('meetings.list', { limit: 100, ...payload });
  useEffect(() => { if (query.hasNextPage && !query.isFetchingNextPage) void query.fetchNextPage(); }, [query.hasNextPage, query.isFetchingNextPage, query.data?.pages.length]);
  return { rows: query.data?.pages.flatMap((page) => items(page)) ?? [], isPending: query.isPending || query.hasNextPage };
}
