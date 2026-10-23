import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { CommandForm, Modal } from '../src/components/ui';

const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));

function wrapper({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>{children}</QueryClientProvider>;
}

describe('accessible real-command forms', () => {
  it('leaves native submission disabled in browser development', () => {
    render(<CommandForm operation="meetings.create" fields={[{ name: 'title', label: 'Meeting name', required: true }]} submit="Create preparation" />, { wrapper });
    expect(screen.getByRole('textbox', { name: 'Meeting name' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Create preparation' })).toBeDisabled();
    expect(invoke).not.toHaveBeenCalled();
  });
  it('preserves a typed draft and surfaces CAS conflict recovery', async () => {
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {}, configurable: true });
    invoke.mockRejectedValue({ code: 'revision_conflict', message: 'Saved revision is now 3', details: { current_revision: 3 } });
    render(<CommandForm operation="actions.update" fixed={{ id: 'action', expected_revision: 2 }} fields={[{ name: 'description', label: 'Action', value: 'Before', required: true }]} />, { wrapper });
    const input = screen.getByRole('textbox', { name: 'Action' });
    fireEvent.change(input, { target: { value: 'My correction' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Your changes have not been discarded'));
    expect(input).toHaveValue('My correction');
    expect(invoke).toHaveBeenCalledWith('engine_request', { command: { operation_id: 'actions.update', payload: { id: 'action', expected_revision: 2, description: 'My correction' } } });
  });
  it('traps modal interaction and restores focus on Escape', async () => {
    const user = userEvent.setup();
    render(<Modal trigger={<button>Open review</button>} title="Review destination"><label>Destination<input /></label></Modal>, { wrapper });
    const trigger = screen.getByRole('button', { name: 'Open review' });
    await user.click(trigger);
    expect(screen.getByRole('dialog', { name: 'Review destination' })).toBeVisible();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });
  it('renders untrusted meeting text as text, never HTML', () => {
    render(<Modal open title="Untrusted title"><p>{'<img src=x onerror=alert(1)>'}</p></Modal>, { wrapper });
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeVisible();
    expect(document.querySelector('img')).toBeNull();
  });
});
