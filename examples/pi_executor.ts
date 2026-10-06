/** Original structural adapter, not a copied Pi SDK implementation.
 * Checked against the documented AgentSession methods on 2026-10-06.
 * No package import here: a trusted composition root supplies a vetted session.
 * Compile with tsc --strict --target ES2022 --module commonjs --lib ES2022,DOM.
 */
export interface VettedPiSession {
  prompt(text: string): Promise<void>;
  abort(): Promise<void>;
  waitForIdle(): Promise<void>;
  getLastAssistantText(): string | undefined;
  getActiveToolNames(): string[];
  dispose(): void;
}

export interface DecisionEnvelope {
  decision: "silent" | "propose";
  summary: string;
  proposals: unknown[]; // Production JSON Schema validator refines each action.
}

export function parseDecision(text: string): DecisionEnvelope {
  if (text.length > 65536) throw new Error("Decision too large");
  const x: unknown = JSON.parse(text);
  if (x === null || typeof x !== "object" || Array.isArray(x)) {
    throw new Error("Decision must be an object");
  }
  const o = x as Record<string, unknown>;
  if (Object.keys(o).some(k => !["decision", "summary", "proposals"].includes(k)) ||
      (o.decision !== "silent" && o.decision !== "propose") ||
      typeof o.summary !== "string" || o.summary.length > 2048 ||
      !Array.isArray(o.proposals) || o.proposals.length > 8 ||
      (o.decision === "silent" && o.proposals.length !== 0)) {
    throw new Error("Invalid decision envelope");
  }
  return o as unknown as DecisionEnvelope;
}

export async function runPiDecision(
  factory: () => Promise<VettedPiSession>,
  prompt: string,
  approvedReadToolNames: ReadonlySet<string>,
  signal?: AbortSignal,
): Promise<DecisionEnvelope> {
  if (signal?.aborted) throw new Error("Cancelled before creating session");
  const session = await factory();
  let abortPromise: Promise<void> | undefined;
  let abortError: unknown;
  const requestAbort = (): void => {
    if (!abortPromise) {
      // Keep rejection observed even if prompt() is still pending.
      abortPromise = session.abort().catch(error => { abortError = error; });
    }
  };
  try {
    const unexpected = session.getActiveToolNames().filter(n => !approvedReadToolNames.has(n));
    if (unexpected.length) throw new Error("Session contains unapproved tools");
    // Names are an extra check, NOT proof of read-only behavior. The factory must
    // disable unsafe extensions, use isolated storage, and route tools via a broker.
    signal?.addEventListener("abort", requestAbort, { once: true });
    if (signal?.aborted) requestAbort();
    if (!signal?.aborted) await session.prompt(prompt);
    await session.waitForIdle();
    if (abortPromise) await abortPromise;
    if (abortError) throw abortError;
    if (signal?.aborted) throw new Error("Cancelled");
    const text = session.getLastAssistantText();
    if (text === undefined) throw new Error("No final assistant message");
    return parseDecision(text);
  } finally {
    signal?.removeEventListener("abort", requestAbort);
    session.dispose();
    if (abortPromise) await abortPromise;
  }
}
