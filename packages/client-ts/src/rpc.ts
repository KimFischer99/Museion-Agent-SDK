/** PAS JSON-RPC 2.0 client (SPEC §14.2).
 *
 * Transport-agnostic: the caller supplies an `Endpoint` that sends one
 * JSON-RPC frame (as text) and returns the reply frame. Loopback HTTP and
 * Unix-socket endpoints live in the deployment; this file never opens a
 * socket itself. Notifications carry no id and are therefore unreliable
 * by definition — durable writes always go through `call`.
 *
 * Method set is frozen by SPEC §14.2 and mirrored from
 * proactive_sdk.rpc.PROACTIVE_RPC_METHODS; `hello()` negotiates the
 * protocol version and caches the server's method list, and every other
 * call fails closed until it has run.
 */

import type {
  ActionRecord,
  ContextPack,
  Decision,
  DeliveryAttempt,
  JobSpec,
  PasError,
  RunHandle,
  RunRequest,
  Usage,
  WakeEvent,
} from "./schema_types";

export const PAS_PROTOCOL_VERSION = "1.0";

/** Hard frame budget shared with the Python side (rpc.MAX_MESSAGE_BYTES). */
export const MAX_MESSAGE_BYTES = 1024 * 1024;

export const PROACTIVE_RPC_METHODS = [
  "system.hello",
  "system.capabilities",
  "system.health",
  "jobs.create",
  "jobs.update",
  "jobs.list",
  "jobs.pause",
  "jobs.resume",
  "jobs.delete",
  "runs.get",
  "runs.list",
  "runs.cancel",
  "runs.events",
  "skills.audit",
  "skills.import",
  "skills.explain",
  "approvals.get",
  "approvals.resolve",
  "notifications.list",
  "notifications.feedback",
] as const;

export type ProactiveRpcMethod = (typeof PROACTIVE_RPC_METHODS)[number];

export interface Endpoint {
  /** Send one request frame, return the reply frame text. */
  send(frame: string): Promise<string>;
}

export class PasRpcError extends Error {
  readonly wireCode: number;
  /** PAS error code from schemas/v1/error.json, when the server sent one. */
  readonly pasCode: string | undefined;

  constructor(wireCode: number, message: string, pasCode?: string | undefined) {
    super(message);
    this.name = "PasRpcError";
    this.wireCode = wireCode;
    this.pasCode = pasCode;
  }
}

interface RpcReply {
  jsonrpc?: string;
  id?: number | string | null;
  result?: unknown;
  error?: { code: number; message: string; data?: { code?: string } };
}

const textEncoder = new TextEncoder();

export class PasRpcClient {
  private nextId = 0;
  private negotiatedMethods: readonly string[] | null = null;

  constructor(
    private readonly endpoint: Endpoint,
    private readonly options: { clientName?: string } = {},
  ) {}

  get negotiated(): boolean {
    return this.negotiatedMethods !== null;
  }

  /** Version negotiation (SPEC §14.2). Fails closed on major mismatch. */
  async hello(): Promise<readonly string[]> {
    const result = (await this.call("system.hello", {
      protocol_version: PAS_PROTOCOL_VERSION,
      client: this.options.clientName ?? "client-ts",
    })) as { protocol_version: string; methods: string[] };
    if (result.protocol_version.split(".")[0] !== PAS_PROTOCOL_VERSION.split(".")[0]) {
      throw new PasRpcError(
        -32000,
        `protocol major mismatch: server ${result.protocol_version}`,
        "unsupported_capability",
      );
    }
    this.negotiatedMethods = Object.freeze(result.methods);
    return this.negotiatedMethods;
  }

  async capabilities(): Promise<{ protocol_version: string; methods: string[] }> {
    return (await this.call("system.capabilities", {})) as {
      protocol_version: string;
      methods: string[];
    };
  }

  async health(): Promise<{ ok: boolean; server: string }> {
    return (await this.call("system.health", {})) as { ok: boolean; server: string };
  }

  async call(method: string, params?: Record<string, unknown>): Promise<unknown> {
    if (method !== "system.hello" && !this.negotiated) {
      throw new PasRpcError(-32000, "session not negotiated; call hello() first", "auth_required");
    }
    const id = ++this.nextId;
    const frame = JSON.stringify({ jsonrpc: "2.0", id, method, params: params ?? {} });
    if (textEncoder.encode(frame).length > MAX_MESSAGE_BYTES) {
      throw new PasRpcError(-32600, "request exceeds MAX_MESSAGE_BYTES");
    }
    let reply: RpcReply;
    try {
      reply = JSON.parse(await this.endpoint.send(frame)) as RpcReply;
    } catch (error) {
      throw new PasRpcError(
        -32700,
        `invalid reply frame: ${error instanceof Error ? error.message : "unknown"}`,
      );
    }
    if (reply.error) {
      throw new PasRpcError(
        reply.error.code,
        String(reply.error.message),
        reply.error.data?.code,
      );
    }
    if (!("result" in reply)) {
      throw new PasRpcError(-32603, "reply carries neither result nor error");
    }
    return reply.result;
  }

  /** Notifications are fire-and-forget; never use them for durable writes. */
  notify(method: string, params?: Record<string, unknown>): void {
    void this.endpoint
      .send(JSON.stringify({ jsonrpc: "2.0", method, params: params ?? {} }))
      .then(() => undefined)
      .catch(() => undefined);
  }

  // -- typed §14.2 convenience calls; result shapes beyond the v1 schemas
  // are bound by the facade (P7) and stay `unknown` until then. --------

  createJob(spec: JobSpec): Promise<unknown> {
    return this.call("jobs.create", { job: spec });
  }
  pauseJob(jobId: string): Promise<unknown> {
    return this.call("jobs.pause", { job_id: jobId });
  }
  resumeJob(jobId: string): Promise<unknown> {
    return this.call("jobs.resume", { job_id: jobId });
  }
  getRun(runId: string): Promise<unknown> {
    return this.call("runs.get", { run_id: runId });
  }
  cancelRun(runId: string): Promise<unknown> {
    return this.call("runs.cancel", { run_id: runId });
  }
  listNotifications(): Promise<unknown> {
    return this.call("notifications.list", {});
  }
}

// Schema types are re-exported so a single import serves the whole wire.
export type {
  ActionRecord,
  ContextPack,
  Decision,
  DeliveryAttempt,
  JobSpec,
  PasError,
  RunHandle,
  RunRequest,
  Usage,
  WakeEvent,
} from "./schema_types";
