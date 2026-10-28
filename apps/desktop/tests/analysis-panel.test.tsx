import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AnalysisPanel } from '../src/features/outcomes';

const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
vi.mock('@tanstack/react-router', () => ({ Link: ({ children }: { children: unknown }) => children, useNavigate: () => vi.fn() }));

function show(status: Record<string, unknown>) {
  invoke.mockImplementation(async (_command: string, args: { command?: { operation_id?: string } }) => args?.command?.operation_id === 'analysis.status' ? { items: [status] } : { items: [] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><AnalysisPanel meetingId="m1" /></QueryClientProvider>);
}

describe('analysis panel', () => {
  beforeEach(() => {
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {}, configurable: true });
    invoke.mockReset();
  });

  it('shows a running task as generating', async () => {
    show({ task: 'summary', status: 'running', job_id: 'j1' });
    expect(await screen.findByText('Generating…')).toBeInTheDocument();
  });

  it('shows a failed task with its error and a retry', async () => {
    show({ task: 'summary', status: 'failed', error: 'no provider', job_id: 'j1' });
    expect(await screen.findByText('Failed: no provider')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
  });
});
