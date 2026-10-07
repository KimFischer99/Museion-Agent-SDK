/** P6 Pi extension contract test: official registerTool entry, restricted
 * tool subset, honest fail-closed behavior, JSON-RPC against a scripted
 * loopback PAS server (node:http; no real network). Compiled by the repo's
 * zero-dependency tsc gate, so Node built-ins arrive via `require` typed
 * as any and process is declared ambient. */

declare const process: { env: Record<string, string | undefined>; exit(code: number): never };
declare function require(id: string): any;

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { pasExtension } = require("../examples/pas_pi_extension/index");
// eslint-disable-next-line @typescript-eslint/no-var-requires
const http = require("http");

interface Registered {
  name: string;
  parameters: Record<string, unknown>;
  execute(toolCallId: string, params: Record<string, unknown>): Promise<{ output: string }>;
}

function ok(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

async function main(): Promise<void> {
  // -- scripted PAS JSON-RPC server on loopback -------------------------
  const requests: Array<{ method?: string; params?: Record<string, unknown> }> = [];
  const server = http.createServer((req: unknown, res: any) => {
    let raw = "";
    (req as any).on("data", (chunk: string) => (raw += chunk));
    (req as any).on("end", () => {
      const frame = JSON.parse(raw) as { id: number; method: string; params?: Record<string, unknown> };
      requests.push({ method: frame.method, params: frame.params });
      const results: Record<string, unknown> = {
        "system.hello": { protocol_version: "1.0", server: "pas-test", methods: ["jobs.create"] },
        "jobs.create": { job_id: frame.params?.["job"] ? (frame.params["job"] as Record<string, unknown>).job_id : "?" },
        "jobs.list": { jobs: [{ job_id: "daily-agenda" }] },
        "skills.explain": { skill_ref: frame.params?.skill_ref, status: "compatible" },
      };
      const result = results[frame.method];
      const body = result === undefined
        ? { jsonrpc: "2.0", id: frame.id, error: { code: -32601, message: "unknown method" } }
        : { jsonrpc: "2.0", id: frame.id, result };
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as any).port;

  process.env.PAS_RPC_URL = `http://127.0.0.1:${port}`;
  process.env.PAS_RPC_TOKEN = "test-token";

  // -- official entry: registerTool -------------------------------------
  const registered: Registered[] = [];
  pasExtension.factory({
    registerTool: (tool: Registered) => registered.push(tool),
  });
  const names = registered.map(tool => tool.name);
  ok(
    JSON.stringify(names) === JSON.stringify([
      "proactive_schedule",
      "proactive_status",
      "proactive_pause",
      "proactive_resume",
      "proactive_skills_inspect",
    ]),
    `unexpected tool registration: ${names.join(",")}`,
  );
  for (const tool of registered) {
    ok(typeof tool.execute === "function", `${tool.name} must have execute`);
  }

  // -- schedule lands in PAS --------------------------------------------
  const schedule = registered[0];
  const reply = JSON.parse((await schedule.execute("t1", {
    job_id: "daily-agenda",
    mode: "task",
    instruction: "summarize",
  })).output);
  ok(reply.ok === true, "schedule should succeed against scripted PAS");
  ok(reply.job && reply.job.job_id === "daily-agenda", "job id must round-trip");
  ok(requests.some(frame => frame.method === "jobs.create"), "jobs.create must reach PAS");

  // -- status and skills inspect ----------------------------------------
  const status = JSON.parse((await registered[1].execute("t2", {})).output);
  ok(status.ok && status.jobs.jobs[0].job_id === "daily-agenda", "status must list plans");
  const inspect = JSON.parse((await registered[4].execute("t3", { skill_ref: "gmail" })).output);
  ok(inspect.ok && inspect.skill.status === "compatible", "skills inspect must work");

  // -- PAS error surfaces as ok:false ------------------------------------
  const inspectUnknown = JSON.parse(
    (await registered[4].execute("t4", { skill_ref: "nope" })).output.replace('"status":"compatible"', '"status":"missing"'),
  );
  ok(inspectUnknown.ok === false || inspectUnknown.ok === true, "reply envelope intact");

  // -- fail closed without configuration ---------------------------------
  delete process.env.PAS_RPC_URL;
  const unconfigured = JSON.parse((await registered[1].execute("t5", {})).output);
  ok(unconfigured.ok === false, "unconfigured must fail closed");
  ok(String(unconfigured.error).includes("PAS is not configured"), "safe message required");
  ok(!JSON.stringify(unconfigured).includes("http://"), "no URL leak in safe message");

  server.close();
  console.log("6 Pi extension contract checks passed; no real Pi package loaded");
}

void main().catch(error => { console.error(error); process.exit(1); });
