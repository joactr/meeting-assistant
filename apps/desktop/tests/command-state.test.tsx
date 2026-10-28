import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CommandButton } from '../src/components/ui';
import { speakerStateLabel } from '../src/features/providers';

const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));

function renderButton() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><CommandButton operation="catalog.download" payload={{ id: 'm' }}>Download</CommandButton></QueryClientProvider>);
  return client;
}

describe('command button', () => {
  beforeEach(() => {
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {}, configurable: true });
    invoke.mockReset();
  });

  it('stops working and can be pressed again after a rejection', async () => {
    invoke.mockRejectedValue({ code: 'invalid_input', message: 'This model is already downloading' });
    renderButton();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('already downloading');
    expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  });

  it('stops working after an accepted result and refreshes engine queries', async () => {
    let finish: (value: unknown) => void = () => undefined; // tsconfig lib predates Promise.withResolvers
    invoke.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const client = renderButton();
    const refresh = vi.spyOn(client, 'invalidateQueries');
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(await screen.findByRole('button', { name: 'Working…' })).toBeDisabled();
    finish({ started: true });
    expect(await screen.findByRole('button', { name: 'Download' })).toBeEnabled();
    expect(refresh).toHaveBeenCalledWith({ queryKey: ['engine'] });
  });
});

describe('speaker model state label', () => {
  it.each([['ready', 'Ready'], ['loading', 'Loading…'], ['stopped', 'Not running'], ['', 'Not running']])('%s → %s', (state, expected) => {
    expect(speakerStateLabel(state)).toBe(expected);
  });
});
