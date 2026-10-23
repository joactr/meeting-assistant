import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => { cleanup(); delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__; });
