import {runPiDecision, parseDecision, VettedPiSession} from "../examples/pi_executor";
function ok(condition: boolean, message: string): void { if (!condition) throw new Error(message); }
async function main(): Promise<void> {
  let disposed = false;
  let finished = false;
  const session: VettedPiSession = {
    async prompt() { await Promise.resolve(); finished = true; },
    async abort() {}, async waitForIdle() {},
    getLastAssistantText() { return finished ? '{"decision":"silent","summary":"No change","proposals":[]}' : undefined; },
    getActiveToolNames() { return ["source_read"]; },
    dispose() { disposed = true; }
  };
  const result = await runPiDecision(async () => session, "test", new Set(["source_read"]));
  ok(result.decision === "silent" && disposed && finished, "finalization failed");
  let rejected = false;
  try { parseDecision('{"decision":"silent","summary":"x","proposals":[{}]}'); }
  catch { rejected = true; }
  ok(rejected, "silent proposal should fail");
  rejected = false;
  try { await runPiDecision(async () => session, "test", new Set()); } catch { rejected = true; }
  ok(rejected, "unapproved tool must fail");
  const controller = new AbortController(); controller.abort();
  rejected = false;
  try { await runPiDecision(async () => session, "test", new Set(["source_read"]), controller.signal); }
  catch { rejected = true; }
  ok(rejected, "pre-cancelled run must fail");
  console.log("4 structural Pi contract checks passed; no real Pi package loaded");
}
void main().catch(error => { console.error(error); throw error; });
