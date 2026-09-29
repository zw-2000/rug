import { describe, expect, it } from "vitest";
import { splitCitations, toBlocks } from "./citations";

describe("splitCitations", () => {
  it("separates [n] markers from text", () => {
    expect(splitCitations("Scope is X [1] and Y [2].")).toEqual([
      { kind: "text", text: "Scope is X " },
      { kind: "cite", n: 1 },
      { kind: "text", text: " and Y " },
      { kind: "cite", n: 2 },
      { kind: "text", text: "." },
    ]);
  });

  it("treats markup as inert text", () => {
    const pieces = splitCitations('<img src=x onerror=alert(1)> [1]');
    expect(pieces[0]).toEqual({ kind: "text", text: "<img src=x onerror=alert(1)> " });
  });

  it("leaves non-numeric brackets alone", () => {
    expect(splitCitations("see [a] and [1234]")).toEqual([{ kind: "text", text: "see [a] and [1234]" }]);
  });
});

describe("toBlocks", () => {
  it("groups bullets and paragraphs", () => {
    expect(toBlocks("Summary line\n- one\n- two\n\nAfter")).toEqual([
      { kind: "p", text: "Summary line" },
      { kind: "ul", items: ["one", "two"] },
      { kind: "p", text: "After" },
    ]);
  });
});
