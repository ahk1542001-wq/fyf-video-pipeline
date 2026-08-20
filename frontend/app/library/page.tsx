"use client";

import { useEffect, useState } from "react";
import ApprovedVideoLibrary from "../../components/approved-video-library";
import StudioHeader from "../../components/studio-header";
import {
  API_URL,
  isRuntimeInfo,
  STATIC_RUNTIME_FALLBACK,
  type RuntimeInfo,
} from "../../lib/video-ui";

export default function LibraryPage() {
  const [runtime, setRuntime] = useState<RuntimeInfo>(STATIC_RUNTIME_FALLBACK);
  const [runtimeSource, setRuntimeSource] = useState<"api" | "fallback">("fallback");

  useEffect(() => {
    let mounted = true;
    async function loadRuntime() {
      try {
        const response = await fetch(`${API_URL}/api/runtime`);
        const data: unknown = await response.json();
        if (!response.ok || !isRuntimeInfo(data)) throw new Error("Runtime API unavailable");
        if (mounted) {
          setRuntime(data);
          setRuntimeSource("api");
        }
      } catch {
        if (mounted) {
          setRuntime(STATIC_RUNTIME_FALLBACK);
          setRuntimeSource("fallback");
        }
      }
    }
    void loadRuntime();
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <div className="app-shell">
      <StudioHeader runtime={runtime} runtimeSource={runtimeSource} />
      <main className="library-main">
        <ApprovedVideoLibrary />
      </main>
    </div>
  );
}
