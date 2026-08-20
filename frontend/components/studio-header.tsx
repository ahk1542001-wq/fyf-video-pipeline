"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { RuntimeInfo } from "../lib/video-ui";
import RuntimeDisclosure from "./runtime-disclosure";

type StudioHeaderProps = {
  runtime: RuntimeInfo;
  runtimeSource: "api" | "fallback";
};

export default function StudioHeader({ runtime, runtimeSource }: StudioHeaderProps) {
  const pathname = usePathname();
  const isCreate = pathname === "/";
  const isLibrary = pathname === "/library" || pathname.startsWith("/library/");
  const isTelemetry = pathname === "/telemetry" || pathname.startsWith("/telemetry/");

  return (
    <header className="studio-header">
      <div className="studio-header__inner">
        <Link href="/" className="studio-brand" aria-label="FYF Create workspace">
          <span className="studio-brand__mark">FYF</span>
          <span className="studio-brand__subline">Video workspace</span>
        </Link>

        <nav className="studio-nav" aria-label="Primary navigation">
          <Link href="/" className={`studio-nav__link${isCreate ? " studio-nav__link--active" : ""}`} aria-current={isCreate ? "page" : undefined}>
            Create
          </Link>
          <Link href="/library" className={`studio-nav__link${isLibrary ? " studio-nav__link--active" : ""}`} aria-current={isLibrary ? "page" : undefined}>
            Library
          </Link>
          <Link href="/telemetry" className={`studio-nav__link${isTelemetry ? " studio-nav__link--active" : ""}`} aria-current={isTelemetry ? "page" : undefined}>
            Telemetry
          </Link>
        </nav>

        <RuntimeDisclosure runtime={runtime} runtimeSource={runtimeSource} />
      </div>
    </header>
  );
}
