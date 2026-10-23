type Flush = () => Promise<void>;
const flushers = new Set<Flush>();

export function registerDraft(flush: Flush): () => void {
  flushers.add(flush);
  return () => { flushers.delete(flush); };
}

export async function flushDrafts(): Promise<void> {
  await Promise.all([...flushers].map((flush) => flush()));
}
