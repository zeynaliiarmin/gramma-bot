import type { IncomingMessage, ServerResponse } from "node:http";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

/**
 * Gramma AI Bridge — exposes `POST /api/ask` on the OpenClaw gateway.
 *
 * Contract (mirrors app/services/openclaw.py on the Gramma side):
 *   request : { "message": string, "context": object?, "task": string? }
 *   response: { "reply": string }
 *
 * The route runs with gateway auth, i.e. `Authorization: Bearer <gateway-token>`
 * is validated by OpenClaw itself. Each request runs one isolated agent turn
 * through `api.runtime.subagent.complete(...)` and returns the visible text.
 */

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk: Buffer) => {
      data += chunk.toString("utf8");
      if (data.length > 1_000_000) {
        reject(new Error("body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

export default definePluginEntry({
  id: "gramma-bridge",
  name: "Gramma AI Bridge",
  description:
    "Exposes POST /api/ask on the OpenClaw gateway so the Gramma bot can query the AI brain.",
  register(api) {
    api.registerHttpRoute({
      path: "/api/ask",
      match: "exact",
      auth: "gateway",
      handler: async (req: IncomingMessage, res: ServerResponse) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ error: "method not allowed" }));
          return true;
        }
        let parsed: any;
        try {
          parsed = JSON.parse((await readBody(req)) || "{}");
        } catch {
          res.statusCode = 400;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ error: "invalid JSON body" }));
          return true;
        }
        const message: string = String(
          parsed?.message ?? parsed?.prompt ?? ""
        ).trim();
        if (!message) {
          res.statusCode = 400;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ error: "message is required" }));
          return true;
        }
        try {
          const { text } = await api.runtime.subagent.complete({
            agentId: "gramma",
            message,
            timeoutMs: 30000,
          });
          res.statusCode = 200;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ reply: text }));
        } catch (e: any) {
          res.statusCode = 502;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ error: String(e?.message ?? e) }));
        }
        return true;
      },
    });
  },
});
