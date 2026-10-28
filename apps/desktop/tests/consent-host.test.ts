import { describe, expect, it } from 'vitest';
import { consentHost } from '../src/features/providers';

describe('consentHost', () => {
  it('asks for consent only when the endpoint is not this device', () => {
    expect(consentHost('ollama', '')).toBe('');
    expect(consentHost('openai-compatible', 'localhost:8080')).toBe('');
    expect(consentHost('openai-compatible', 'http://127.0.0.1:1234/v1')).toBe('');
    expect(consentHost('openai-compatible', '192.168.1.9:8080')).toBe('192.168.1.9');
    expect(consentHost('openai-compatible', 'http://127.example.com')).toBe('127.example.com');
    expect(consentHost('openrouter', '')).toBe('openrouter.ai');
  });
});
