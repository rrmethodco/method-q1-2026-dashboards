import { WebClient } from "@slack/web-api";

let _client: WebClient | null = null;

function slack(): WebClient {
  if (_client) return _client;
  const token = Deno.env.get("SLACK_BOT_TOKEN");
  if (!token) throw new Error("SLACK_BOT_TOKEN secret not set");
  _client = new WebClient(token);
  return _client;
}

export async function postAlert(
  channel: string,
  text: string,
  blocks?: unknown[],
): Promise<void> {
  // deno-lint-ignore no-explicit-any
  await slack().chat.postMessage({ channel, text, blocks } as any);
}
