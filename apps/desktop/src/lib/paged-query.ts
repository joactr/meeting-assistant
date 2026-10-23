import { useInfiniteQuery } from '@tanstack/react-query';
import { nativeAvailable, request } from './bridge';
import { text, type Payload } from './data';

export function useCursorResource(operationId: string, payload: Payload) {
  return useInfiniteQuery({
    queryKey: ['engine', operationId, payload],
    initialPageParam: '',
    queryFn: ({ pageParam }) => request(operationId, { ...payload, ...(pageParam ? { cursor: pageParam } : {}) }),
    getNextPageParam: (lastPage) => text(lastPage.next_cursor) || undefined,
    enabled: nativeAvailable(),
    staleTime: 10_000,
    retry: false,
  });
}
