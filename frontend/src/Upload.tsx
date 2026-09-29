import { useState, type FormEvent } from "react";
import { ApiError, downloadUrl, upload, type Me, type UploadResult } from "./api";

export default function Upload({ me }: { me: Me }) {
  const [folder, setFolder] = useState(me.upload_folders[0] ?? "");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<UploadResult | null>(null);

  if (me.upload_folders.length === 0) {
    return <p className="muted">You can&apos;t upload anywhere yet: no folder is available to you.</p>;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setError("");
    setDone(null);
    if (file.size > me.max_upload_mb * 1024 * 1024) {
      setError(`That file is larger than ${me.max_upload_mb} MB.`);
      return;
    }
    setBusy(true);
    try {
      setDone(await upload(folder, file));
      setFile(null);
      (document.getElementById("file") as HTMLInputElement | null)?.form?.reset();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="upload" aria-labelledby="upload-title">
      <h1 id="upload-title">Upload a document</h1>
      <label>
        Folder
        <select value={folder} onChange={(e) => setFolder(e.target.value)}>
          {me.upload_folders.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
      </label>
      <label>
        File ({me.upload_types.join(", ")}, up to {me.max_upload_mb} MB)
        <input
          id="file"
          type="file"
          accept={me.upload_types.join(",")}
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </label>
      <button type="submit" disabled={!file || busy}>
        {busy ? "Uploading…" : "Upload"}
      </button>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {done && (
        <p className="ok" role="status">
          Saved as <code>{done.folder}/{done.filename}</code>.{" "}
          {done.indexed ? (
            <>
              It is searchable now.{" "}
              {done.id && <a href={downloadUrl(done.id)}>Download it</a>}
            </>
          ) : (
            "It will become searchable after the next index run."
          )}
        </p>
      )}
    </form>
  );
}
