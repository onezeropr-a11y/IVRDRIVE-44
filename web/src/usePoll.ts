import { useCallback, useEffect, useState } from "react";

/** Polling beats a websocket here: every screen is one small query and the
 *  dispatcher tolerates a few seconds of staleness. */
export function usePoll<T>(load: () => Promise<T>, seconds: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string>("");

  const refresh = useCallback(() => {
    load()
      .then((value) => {
        setData(value);
        setError("");
      })
      .catch((err: Error) => setError(err.message));
  }, [load]);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, seconds * 1000);
    return () => clearInterval(timer);
  }, [refresh, seconds]);

  return { data, error, refresh };
}

export const clock = (iso: string) =>
  new Date(iso).toLocaleString("he-IL", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
