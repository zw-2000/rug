import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";

/** Load data on mount and on demand; expose the error text and a reload function. */
export function useLoad<T>(fn: () => Promise<T>): {
  data: T | null;
  error: string;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const reload = useCallback(() => {
    fn()
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not reach the server."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(reload, [reload]);
  return { data, error, reload };
}

export function message(e: unknown): string {
  return e instanceof ApiError ? e.message : "Could not reach the server.";
}
