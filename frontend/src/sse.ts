export interface SseEvent {
  event: string;
  data: unknown;
}

/**
 * Split a growing text buffer into complete Server-Sent Events. Returns the events found and the
 * unfinished remainder to prepend to the next chunk. Comment lines (": keepalive") are ignored;
 * a block whose data is not valid JSON is skipped rather than allowed to break the stream.
 */
export function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: SseEvent[] = [];
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length === 0) continue;
    try {
      events.push({ event, data: JSON.parse(data.join("\n")) });
    } catch {
      continue;
    }
  }
  return { events, rest };
}
