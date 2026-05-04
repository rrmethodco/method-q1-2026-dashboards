import Anthropic from "@anthropic-ai/sdk";

let _client: Anthropic | null = null;

export function anthropic(): Anthropic {
  if (_client) return _client;
  const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
  if (!apiKey) throw new Error("ANTHROPIC_API_KEY not set");
  _client = new Anthropic({ apiKey });
  return _client;
}

export async function classifyDrift(opts: {
  source: string;
  storedSchemaKeys: string[];
  observedRowSamples: Record<string, unknown>[];
}): Promise<{
  classification: "stable" | "additive_non_breaking" | "breaking";
  reasoning: string;
  added_fields: string[];
  removed_fields: string[];
  changed_types: string[];
}> {
  const { source, storedSchemaKeys, observedRowSamples } = opts;
  const prompt = [
    "You are a schema drift classifier for a data ingestion pipeline.",
    `Source: ${source}`,
    `Stored schema field set: ${JSON.stringify(storedSchemaKeys)}`,
    "Observed sample rows (first 3 from latest sync):",
    JSON.stringify(observedRowSamples, null, 2),
    "",
    "Classify the diff as ONE OF:",
    "  - stable: no diff, or diff is in allowed mutation set (extra fields fine)",
    "  - additive_non_breaking: new optional field present, no existing field removed/null",
    "  - breaking: required field removed, type changed, or population pattern flipped",
    "    (e.g. previously-populated field is now consistently null)",
    "",
    "Respond with ONLY a JSON object matching this exact schema:",
    `{"classification": "...", "reasoning": "...", "added_fields": [...], "removed_fields": [...], "changed_types": [...]}`,
  ].join("\n");

  const resp = await anthropic().messages.create({
    model: "claude-sonnet-4-5",
    max_tokens: 1024,
    messages: [{ role: "user", content: prompt }],
  });
  const text = resp.content[0].type === "text" ? resp.content[0].text : "";
  try {
    return JSON.parse(text);
  } catch {
    const m = text.match(/\{[\s\S]*\}/);
    if (m) return JSON.parse(m[0]);
    throw new Error(`drift classifier returned non-JSON: ${text.slice(0, 200)}`);
  }
}
