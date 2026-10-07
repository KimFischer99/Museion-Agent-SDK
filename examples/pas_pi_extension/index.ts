/** PAS proactive extension for Pi (SPEC §12.2 方式 B; P6).
 *
 * Registers the proactive.* tool family through Pi's official extension
 * entry (registerTool). Every operation lands in the PAS control plane
 * via JSON-RPC 2.0 (PAS_RPC_URL / PAS_RPC_TOKEN from the environment);
 * the extension stores nothing and decides nothing, mirroring the Hermes
 * plugin (examples/pas_hermes_plugin). Tools exposed to the model are the
 * restricted §14.3 subset — never grants.create/approvals.resolve.
 *
 * InlineExtension shape: { name, factory }. The factory keeps the same
 * signature as Pi's ExtensionAPI.registerTool so the extension can be
 * loaded from .pi/extensions or passed as an inline extension by a vetted
 * composition root (SPEC §12.2: 禁止默认发现未知项目 extension — this file
 * is a source the deployment must explicitly trust).
 *
 * Configure via PAS_RPC_URL (https or literal loopback http) and
 * PAS_RPC_TOKEN before activation; unconfigured → every tool answers
 * { ok: false } with a safe message (fail closed), registration still
 * succeeds so the user can see and disable the extension.
 */

// Minimal ambient Node surface: this file runs inside the Pi worker host
// (Node ≥ 22) and the repo's zero-dependency typecheck provides no
// @types/node. Only process.env is consumed.
declare const process: { env: Record<string, string | undefined> };

interface PasToolArgs {
  [key: string]: unknown;
}

interface PasTool {
  name: string;
  label: string;
  description: string;
  parameters: Record<string, unknown>;
  execute(args: PasToolArgs): Promise<ToolReply>;
}

const PROTOCOL_VERSION = "1.0";
const MAX_REPLY_BYTES = 1024 * 1024;

class PasRpcError extends Error {
  constructor(
    message: string,
    readonly pasCode?: string,
  ) {
    super(message);
    this.name = "PasRpcError";
  }
}

let negotiated = false;
let nextId = 0;

function rpcConfig(): { url: string; token: string } {
  const url = process.env.PAS_RPC_URL ?? "";
  const token = process.env.PAS_RPC_TOKEN ?? "";
  if (!url || !token) {
    throw new PasRpcError(
      "PAS is not configured: set PAS_RPC_URL and PAS_RPC_TOKEN before using proactive tools",
    );
  }
  const parsed = new URL(url);
  const loopback =
    parsed.hostname === "127.0.0.1" ||
    parsed.hostname === "::1" ||
    parsed.hostname === "localhost";
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && loopback)) {
    throw new PasRpcError("PAS_RPC_URL must be https or loopback http");
  }
  if (parsed.username || parsed.password || parsed.search) {
    throw new PasRpcError("PAS_RPC_URL must not carry credentials or query");
  }
  return { url: url.replace(/\/$/, ""), token };
}

async function rpcCall(method: string, params?: Record<string, unknown>): Promise<unknown> {
  const { url, token } = rpcConfig();
  if (method !== "system.hello" && !negotiated) {
    await rpcCall("system.hello", { protocol_version: PROTOCOL_VERSION, client: "pi-pas-extension" });
  }
  const frame = { jsonrpc: "2.0", id: ++nextId, method, params: params ?? {} };
  const reply = (await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(frame),
  })) as Response & { json(): Promise<unknown> };
  if (reply.status !== 200) {
    throw new PasRpcError(`PAS RPC HTTP ${reply.status}`);
  }
  const body = (await reply.json()) as {
    result?: unknown;
    error?: { code: number; message: string; data?: { code?: string } };
  };
  if (body.error) {
    throw new PasRpcError(String(body.error.message), body.error.data?.code);
  }
  if (method === "system.hello") negotiated = true;
  return body.result;
}

interface ToolReply {
  ok: boolean;
  [key: string]: unknown;
}

async function guard(fn: () => Promise<Record<string, unknown>>): Promise<ToolReply> {
  try {
    return { ok: true, ...(await fn()) };
  } catch (error) {
    const message = error instanceof PasRpcError ? error.message : `PAS call failed: ${error instanceof Error ? error.constructor.name : "unknown"}`;
    return { ok: false, error: message };
  }
}

function str(description: string): Record<string, unknown> {
  return { type: "string", description };
}

function schema(properties: Record<string, unknown>, required: string[]): Record<string, unknown> {
  return { type: "object", properties, required, additionalProperties: false };
}

function tools(): PasTool[] {
  return [
    {
      name: "proactive_schedule",
      label: "PAS: schedule plan",
      description:
        "Propose one persisted proactive plan. Landed in PAS; delivery still obeys PAS policy. Accepted is NOT notified.",
      parameters: schema(
        {
          job_id: str("Stable plan id, 3..128 chars [a-z0-9._-]"),
          mode: { type: "string", enum: ["heartbeat", "task", "watch"] },
          instruction: str("What the agent should analyse on each wake"),
        },
        ["job_id", "mode", "instruction"],
      ),
      execute: async (args) =>
        guard(async () => {
          const job = {
            job_id: String(args.job_id ?? ""),
            mode: String(args.mode ?? ""),
            schedule: args.schedule as unknown,
            task: { instruction: String(args.instruction ?? "") },
            enabled: true,
          };
          const result = (await rpcCall("jobs.create", { job })) as Record<string, unknown>;
          return { job: result };
        }),
    },
    {
      name: "proactive_status",
      label: "PAS: plan status",
      description: "Read the user's persisted PAS plans and their next due times.",
      parameters: schema({}, []),
      execute: async () =>
        guard(async () => ({ jobs: await rpcCall("jobs.list", {}) })),
    },
    {
      name: "proactive_pause",
      label: "PAS: pause plan",
      description: "Pause one persisted PAS plan (missed occurrences follow its misfire policy).",
      parameters: schema({ job_id: str("Plan id to pause") }, ["job_id"]),
      execute: async (args) =>
        guard(async () => ({
          job: await rpcCall("jobs.pause", { job_id: String(args.job_id ?? "") }),
        })),
    },
    {
      name: "proactive_resume",
      label: "PAS: resume plan",
      description: "Resume one paused PAS plan.",
      parameters: schema({ job_id: str("Plan id to resume") }, ["job_id"]),
      execute: async (args) =>
        guard(async () => ({
          job: await rpcCall("jobs.resume", { job_id: String(args.job_id ?? "") }),
        })),
    },
    {
      name: "proactive_skills_inspect",
      label: "PAS: skills inspect",
      description: "Look up a Skill's audit/compatibility status inside PAS (read-only).",
      parameters: schema({ skill_ref: str("Skill reference; omit for the summary") }, []),
      execute: async (args) =>
        guard(async () => ({
          skill: await rpcCall("skills.explain", {
            skill_ref: args.skill_ref === undefined ? undefined : String(args.skill_ref),
          }),
        })),
    },
  ];
}

interface PiLikeTool {
  name: string;
  label: string;
  description: string;
  parameters: Record<string, unknown>;
  execute(
    toolCallId: string,
    params: PasToolArgs,
    signal: AbortSignal | undefined,
    onUpdate: undefined,
    ctx: unknown,
  ): Promise<{ output: string }>;
}

interface PiLike {
  registerTool(tool: PiLikeTool): void;
}

export const pasExtension = {
  name: "pas-proactive",
  factory: (pi: PiLike) => {
    for (const tool of tools()) {
      pi.registerTool({
        name: tool.name,
        label: tool.label,
        description: tool.description,
        parameters: tool.parameters,
        async execute(_toolCallId, params) {
          const result = await tool.execute(params);
          return { output: JSON.stringify(result) };
        },
      });
    }
  },
};

export default pasExtension;
