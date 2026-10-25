import { Component, StrictMode, type ErrorInfo, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from '@tanstack/react-router';
import { router } from './app/router';
import '@fontsource-variable/inter/wght.css';
import '@fontsource-variable/newsreader/wght.css';
import '@fontsource-variable/newsreader/wght-italic.css';
import './styles/global.css';

class RenderBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error): { error: Error } { return { error }; }
  componentDidCatch(_error: Error, _info: ErrorInfo): void { /* No automatic telemetry or meeting-content logging. */ }
  render() {
    if (this.state.error) return <main style={{ maxWidth: '50rem', margin: '4rem auto', padding: '2rem' }}><h1>Something went wrong</h1><p role="alert">Your meeting data is safe. Reload to continue.</p><button onClick={() => window.location.reload()}>Reload</button><details><summary>Details</summary><pre>{this.state.error.message}</pre></details></main>;
    return this.props.children;
  }
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: true, gcTime: 300_000 }, mutations: { retry: false } } });
const root = document.getElementById('root');
if (!root) throw new Error('Desktop root element is missing.');
createRoot(root).render(<StrictMode><RenderBoundary><QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider></RenderBoundary></StrictMode>);
