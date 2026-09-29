export type Piece = { kind: "text"; text: string } | { kind: "cite"; n: number };

/** Split answer text into plain pieces and [n] citation markers. Never produces HTML. */
export function splitCitations(text: string): Piece[] {
  const out: Piece[] = [];
  const re = /\[(\d{1,3})\]/g;
  let last = 0;
  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    if (m.index > last) out.push({ kind: "text", text: text.slice(last, m.index) });
    out.push({ kind: "cite", n: Number(m[1]) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
  return out;
}

export type Block = { kind: "p"; text: string } | { kind: "ul"; items: string[] };

/** Group lines into paragraphs and bullet lists ("- x" or "* x"). */
export function toBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    if (bullet) {
      const last = blocks[blocks.length - 1];
      if (last && last.kind === "ul") last.items.push(bullet[1]);
      else blocks.push({ kind: "ul", items: [bullet[1]] });
    } else {
      blocks.push({ kind: "p", text: line.trim() });
    }
  }
  return blocks;
}
