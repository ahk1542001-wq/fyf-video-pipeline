import type { NextConfig } from "next";

const backendUrl = (process.env.FYF_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  ...(process.env.BUILD_STANDALONE === "true" ? { output: "standalone" as const } : {}),
  // Playwright and local split-process development use the loopback hostname;
  // allow Next's dev chunks to hydrate when the browser connects via 127.0.0.1.
  allowedDevOrigins: ["127.0.0.1"],
  async rewrites() {
    return [
      { source: "/create", destination: "/" },
      { source: "/insights", destination: "/telemetry" },
      { source: "/api/:path*", destination: `${backendUrl}/api/:path*` },
      { source: "/health", destination: `${backendUrl}/health` },
    ];
  },
};

export default nextConfig;
