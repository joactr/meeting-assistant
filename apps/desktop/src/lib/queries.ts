import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { nativeAvailable, nativeRequest, request } from './bridge';
import { items, type Payload, type RecordData } from './data';

export function useResource(operationId: string, payload: Payload = {}, enabled = true) {
  return useQuery({
    queryKey: ['engine', operationId, payload],
    queryFn: () => request(operationId, payload),
    enabled: enabled && nativeAvailable(),
    staleTime: 10_000,
    retry: false,
  });
}

export function useCommand(operationId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Payload) => request(operationId, payload),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['engine'] }); },
    retry: false,
  });
}

export function useNativeResource(operation: string, args: Payload = {}, enabled = true) {
  return useQuery({
    queryKey: ['native', operation, args],
    queryFn: () => nativeRequest<RecordData>(operation, args),
    enabled: enabled && nativeAvailable(),
    staleTime: 15_000,
    retry: false,
  });
}

export function useNativeCommand(operation: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: Payload) => nativeRequest<RecordData>(operation, args),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['native'] }); },
    retry: false,
  });
}

export function useMeetingMedia(meetingId: string, enabled = true) {
  return useQuery({
    queryKey: ['engine', 'media.catalog', { meeting_id: meetingId }],
    queryFn: async (): Promise<RecordData> => {
      const segments: RecordData[] = [];
      for (let offset = 0; ; offset += 200) {
        const page = items(await request('entity.list', { collection: 'media_segments', filters: { meeting_id: meetingId }, limit: 200, offset }));
        segments.push(...page);
        if (page.length < 200) return { items: segments };
      }
    },
    enabled: enabled && nativeAvailable(),
    staleTime: 10_000,
    retry: false,
  });
}
