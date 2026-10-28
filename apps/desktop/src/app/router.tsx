import { useCallback, useEffect, useState, type MouseEvent } from 'react';
import { listen } from '@tauri-apps/api/event';
import { Link, Outlet, createRootRoute, createRoute, createRouter, useNavigate, useRouterState } from '@tanstack/react-router';
import { useQueryClient } from '@tanstack/react-query';
import { FolderKanban, Keyboard, Library, ListChecks, Plus, Search, Settings } from 'lucide-react';
import { nativeAvailable, nativeRequest, subscribeEngine } from '../lib/bridge';
import { flushDrafts } from '../lib/drafts';
import { useNativeResource, useResource } from '../lib/queries';
import { useInterface } from '../lib/ui-store';
import { item, number, object, text, type RecordData } from '../lib/data';
import { Details, ErrorNotice, Modal, Notice, Toolbar } from '../components/ui';
import { LibraryPage, NewMeeting } from '../features/library';
import { MeetingPage } from '../features/meeting';
import { ProjectsPage } from '../features/projects';
import { FollowupsPage, SearchPage } from '../features/knowledge';
import { SettingsPage } from '../features/settings';
import styles from './shell.module.css';

type QuitState = { phase: 'working' } | { phase: 'blocked'; result: RecordData } | { phase: 'error'; error: unknown } | null;

/** Handles the native close request: flush drafts, reserve preservation, and either exit or explain what blocks it. */
function QuitGuard() {
  const [state, setState] = useState<QuitState>(null);
  const attempt = useCallback(async (): Promise<void> => {
    setState({ phase: 'working' });
    try {
      await flushDrafts();
      // With no blockers the native host exits the process here; a returned value means something blocks it.
      setState({ phase: 'blocked', result: await nativeRequest<RecordData>('prepare_exit', { cancel: false }) });
    } catch (error) { setState({ phase: 'error', error }); }
  }, []);
  useEffect(() => {
    if (!nativeAvailable()) return undefined;
    let disposed = false; let cleanup: (() => void) | undefined;
    void listen<{ type?: string }>('engine-event', ({ payload }) => { if (payload.type === 'native.shutdown.requested') void attempt(); })
      .then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; });
    return () => { disposed = true; cleanup?.(); };
  }, [attempt]);
  const keepWorking = (): void => { setState(null); void nativeRequest('prepare_exit', { cancel: true }).catch(() => undefined); };
  const busy = state?.phase === 'working';
  return <Modal open={state !== null} onOpenChange={(open) => { if (!open) keepWorking(); }} title="Quit Meeting Assistant">
    {busy && <Notice>Checking…</Notice>}
    {state?.phase === 'blocked' && <><Notice danger>Stop the active meeting, then try again.</Notice><Details value={state.result} title="Blockers" /></>}
    {state?.phase === 'error' && <><ErrorNotice error={state.error} /><Notice danger>Quitting now can lose active recording audio.</Notice></>}
    <Toolbar>
      <button disabled={busy} onClick={keepWorking}>Keep working</button>
      <button disabled={busy} onClick={() => { void attempt(); }}>Try again</button>
      {state?.phase === 'error' && <button onClick={() => { void nativeRequest('force_exit').catch(() => undefined); }}>Quit anyway</button>}
    </Toolbar>
  </Modal>;
}

/** Asks once per version when the native host's automatic check finds a signed GitHub release. */
function UpdatePrompt() {
  const [offer, setOffer] = useState<RecordData | null>(null);
  const [dismissed, setDismissed] = useState('');
  const [state, setState] = useState<{ phase: 'working' } | { phase: 'blocked'; result: RecordData } | { phase: 'error'; error: unknown } | null>(null);
  const [progress, setProgress] = useState({ received: 0, total: 0 });
  useEffect(() => {
    if (!nativeAvailable()) return undefined;
    let disposed = false; let cleanup: (() => void) | undefined;
    void listen<{ type?: string; data?: RecordData }>('engine-event', ({ payload }) => {
      if (payload.type === 'native.update' && payload.data?.state === 'available') setOffer(payload.data);
      if (payload.type === 'native.update.progress') setProgress((current) => ({ received: current.received + number(payload.data?.chunk_bytes), total: number(payload.data?.total_bytes, current.total) }));
    }).then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; });
    return () => { disposed = true; cleanup?.(); };
  }, []);
  const version = text(offer?.version);
  const later = (): void => { setDismissed(version); setState(null); };
  const install = async (): Promise<void> => {
    setProgress({ received: 0, total: 0 });
    setState({ phase: 'working' });
    try {
      await flushDrafts();
      // On success the installer replaces and restarts the app; any other returned value means it did not.
      const result = await nativeRequest<RecordData>('update_install');
      if (result.state === 'blocked') setState({ phase: 'blocked', result });
      else if (result.state !== 'installing') setState({ phase: 'error', error: new Error('The update did not install.') });
    } catch (error) { setState({ phase: 'error', error }); }
  };
  const busy = state?.phase === 'working';
  // Installing… never outlives a minute without progress.
  useEffect(() => {
    if (!busy) return undefined;
    const timer = window.setTimeout(() => setState({ phase: 'error', error: new Error('The update stopped making progress.') }), 60_000);
    return () => window.clearTimeout(timer);
  }, [busy, progress.received]);
  const percent = progress.total ? Math.round(progress.received / progress.total * 100) : 0;
  return <Modal open={!!offer && version !== dismissed} onOpenChange={(open) => { if (!open && !busy) later(); }} title={`Update available: ${version}`}>
    {busy && <Notice>{percent > 0 && percent < 100 ? `Downloading… ${percent}%` : 'Installing…'}</Notice>}
    {state?.phase === 'blocked' && <><Notice danger>Stop the active meeting first.</Notice><Details value={state.result} title="Blockers" /></>}
    {state?.phase === 'error' && <ErrorNotice error={state.error} />}
    <Toolbar>
      <button disabled={busy} onClick={later}>Later</button>
      <button disabled={busy} onClick={() => { void install(); }}>{state?.phase === 'error' ? 'Retry' : 'Install and restart'}</button>
    </Toolbar>
  </Modal>;
}

function Shell() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const shortcutsOpen = useInterface((state) => state.shortcutsOpen);
  const setShortcutsOpen = useInterface((state) => state.setShortcutsOpen);
  const [error, setError] = useState<unknown>();
  const workspace = useResource('workspace.status');
  const preferences = useNativeResource('native_preferences_get');
  const savedPreferences = object(preferences.data?.preferences ?? preferences.data);
  const collapsed = savedPreferences.sidebar_collapsed === true;
  useEffect(() => {
    document.documentElement.dataset.theme = text(savedPreferences.theme, 'system');
  }, [savedPreferences.theme]);
  useEffect(() => {
    let disposed = false; let cleanup: (() => void) | undefined;
    void subscribeEngine(client).then((unlisten) => { if (disposed) unlisten(); else cleanup = unlisten; }).catch(setError);
    return () => { disposed = true; cleanup?.(); };
  }, [client]);
  useEffect(() => {
    const shortcuts = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;
      const editable = !!target?.closest('input,textarea,select,[contenteditable=true],[role=textbox]');
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.key.toLowerCase() === 'k') { event.preventDefault(); void flushDrafts().then(() => navigate({ to: '/search' })).catch(setError); }
      else if (modifier && event.key === ',') { event.preventDefault(); void flushDrafts().then(() => navigate({ to: '/settings' })).catch(setError); }
      else if (modifier && event.shiftKey && event.key.toLowerCase() === 's') { event.preventDefault(); window.dispatchEvent(new Event('meeting-save-minutes')); }
      else if (!editable && modifier && event.shiftKey && event.key.toLowerCase() === 'm') { event.preventDefault(); const button = document.querySelector<HTMLButtonElement>('[data-testid="stop-meeting"], [data-testid="start-meeting"]'); if (button) button.click(); else document.querySelector<HTMLButtonElement>('[data-testid="new-meeting"]')?.click(); }
      else if (!editable && event.key === '?') { event.preventDefault(); setShortcutsOpen(true); }
    };
    window.addEventListener('keydown', shortcuts);
    return () => window.removeEventListener('keydown', shortcuts);
  }, [navigate, setShortcutsOpen]);
  const navigation = [
    { to: '/' as const, label: 'Library', icon: Library },
    { to: '/projects' as const, label: 'Projects', icon: FolderKanban },
    { to: '/followups' as const, label: 'Follow-ups', icon: ListChecks },
    { to: '/search' as const, label: 'Search & Ask', icon: Search },
    { to: '/settings' as const, label: 'Settings', icon: Settings },
  ];
  const followLink = (event: MouseEvent<HTMLAnchorElement>, to: '/' | '/projects' | '/followups' | '/search' | '/settings'): void => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    void flushDrafts().then(() => navigate({ to })).catch(setError);
  };
  const active = object(item(workspace.data).capabilities);
  const mod = navigator.platform.startsWith('Mac') ? '⌘' : 'Ctrl+';
  return <><a className="skip-link" href="#workspace-content">Skip to workspace</a><div className={`${styles.shell} ${collapsed ? styles.collapsed : ''}`}><aside className={styles.sidebar} aria-label="Application navigation"><Link className={styles.brand} to="/" onClick={(event) => followLink(event, '/')}><span className={styles.brandMark} aria-hidden="true"><i /><i /><i /></span><span className={styles.brandText}>Meeting<strong>Assistant</strong></span></Link><nav aria-label="Main navigation">{navigation.map((entry) => <Link key={entry.to} to={entry.to} onClick={(event) => followLink(event, entry.to)} className={`${styles.navLink} ${(entry.to === '/' ? pathname === '/' || pathname.startsWith('/meeting/') : pathname === entry.to || pathname.startsWith(entry.to + '/')) ? styles.active : ''}`} aria-current={(entry.to === '/' ? pathname === '/' || pathname.startsWith('/meeting/') : pathname === entry.to || pathname.startsWith(entry.to + '/')) ? 'page' : undefined}><entry.icon className={styles.navIcon} size={18} aria-hidden="true" /><span className={styles.navLabel}>{entry.label}</span>{entry.to === '/search' && <kbd>{mod}K</kbd>}</Link>)}</nav><div className={styles.sidebarAction}><NewMeeting trigger={<><Plus size={16} aria-hidden="true" /><span className={styles.actionLabel}>New meeting</span></>} /></div><div className={styles.sidebarBottom}><div className={styles.localStatus}><span aria-hidden="true" />{nativeAvailable() ? 'Local engine' : 'Browser preview'}</div><button onClick={() => setShortcutsOpen(true)}><Keyboard size={15} aria-hidden="true" /><span className={styles.navLabel}>Keyboard shortcuts</span><kbd>?</kbd></button><span className={styles.version}>1.0.0 beta{active.capture === true ? '' : ' · no capture host'}</span></div></aside><div className={styles.mainFrame}>{!nativeAvailable() && <div className={styles.offlineBanner} role="status"><strong>Native desktop features are unavailable in this browser.</strong> Recording, providers, files and playback are disabled.</div>}{workspace.isError && <ErrorNotice error={workspace.error} retry={() => { void workspace.refetch(); }} />}{Boolean(error) && <Notice danger><ErrorNotice error={error} /><button onClick={() => setError(undefined)}>Dismiss</button></Notice>}<main id="workspace-content" className={styles.content} tabIndex={-1}><Outlet /></main></div></div><QuitGuard /><UpdatePrompt /><Modal open={shortcutsOpen} onOpenChange={setShortcutsOpen} title="Keyboard shortcuts"><dl><dt>Search</dt><dd><kbd>{mod}K</kbd></dd><dt>Start / stop</dt><dd><kbd>{mod}Shift+M</kbd></dd><dt>Save minutes</dt><dd><kbd>{mod}Shift+S</kbd></dd><dt>Settings</dt><dd><kbd>{mod},</kbd></dd><dt>This list</dt><dd><kbd>?</kbd></dd><dt>Edit utterance</dt><dd>Double-click or Enter · Shift+Enter for newline</dd></dl></Modal></>;
}

const rootRoute = createRootRoute({ component: Shell });
const libraryRoute = createRoute({ getParentRoute: () => rootRoute, path: '/', component: LibraryPage });
const meetingRoute = createRoute({ getParentRoute: () => rootRoute, path: '/meeting/$meetingId', component: () => { const { meetingId } = meetingRoute.useParams(); return <MeetingPage meetingId={meetingId} />; } });
const projectsRoute = createRoute({ getParentRoute: () => rootRoute, path: '/projects', component: ProjectsPage });
const followupsRoute = createRoute({ getParentRoute: () => rootRoute, path: '/followups', component: FollowupsPage });
const searchRoute = createRoute({ getParentRoute: () => rootRoute, path: '/search', component: SearchPage });
const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: '/settings', component: SettingsPage });
export const router = createRouter({ routeTree: rootRoute.addChildren([libraryRoute, meetingRoute, projectsRoute, followupsRoute, searchRoute, settingsRoute]), defaultPreload: 'intent' });
declare module '@tanstack/react-router' { interface Register { router: typeof router; } }
