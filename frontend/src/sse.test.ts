import { describe, expect, it } from "vitest";
import { parseSse } from "./sse";

describe("parseSse", () => {
  it("parses complete events and keeps the unfinished tail", () => {
    const a = parseSse('event: status\ndata: {"state":"working"}\n\nevent: token\ndata: {"te');
    expect(a.events).toEqual([{ event: "status", data: { state: "working" } }]);
    const b = parseSse(a.rest + 'xt":"hi"}\n\n');
    expect(b.events).toEqual([{ event: "token", data: { text: "hi" } }]);
    expect(b.rest).toBe("");
  });

  it("ignores keepalive comments, bad JSON and CRLF", () => {
    const r = parseSse(': keepalive\n\nevent: x\r\ndata: {bad\r\n\r\nevent: answer\r\ndata: {"a":1}\r\n\r\n');
    expect(r.events).toEqual([{ event: "answer", data: { a: 1 } }]);
  });

  it("keeps non-ASCII text intact", () => {
    const r = parseSse('event: token\ndata: {"text":"café — 日本語"}\n\n');
    expect(r.events[0].data).toEqual({ text: "café — 日本語" });
  });
});
