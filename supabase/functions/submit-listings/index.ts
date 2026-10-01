// Supabase Edge Function: POST {"listings": [...]} -> verified listings saved to the database.
// All logic lives in handler.js (tested in Node). Deploy: see README, section "Edge Function".
// @ts-ignore: plain JS module
import { handle } from "./handler.mjs";

declare const Deno: { env: { get(name: string): string | undefined }; serve(h: (req: Request) => Response | Promise<Response>): void };

Deno.serve((req: Request) =>
  handle(req, {
    fetch,
    supabaseUrl: Deno.env.get("SUPABASE_URL")!,
    serviceKey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    extraHosts: (Deno.env.get("ALLOWED_LISTING_HOSTS") || "").split(",").map((h) => h.trim().toLowerCase()).filter(Boolean),
  })
);
