import React from "react";
import {interpolate, spring, useCurrentFrame, useVideoConfig} from "remotion";
import {theme} from "./theme";

export type CallToActionProps = {
  text: string;
};

/** A deterministic end-card CTA that enters during the final four seconds. */
export const CallToAction: React.FC<CallToActionProps> = ({text}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const label = text.trim();
  if (!label) return null;

  const entranceFrame = Math.max(0, durationInFrames - fps * 4);
  const entered = spring({
    frame: frame - entranceFrame,
    fps,
    config: {damping: 16, stiffness: 150, mass: 0.8},
  });
  const opacity = interpolate(entered, [0, 1], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const translateY = interpolate(entered, [0, 1], [34, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <div
      aria-label={label}
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        bottom: 128,
        display: "flex",
        justifyContent: "center",
        opacity,
        transform: `translateY(${translateY}px)`,
        zIndex: 45,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          padding: "18px 34px 20px",
          borderRadius: 999,
          background: theme.colors.primary,
          color: "#fffaf2",
          fontFamily: theme.fonts.display,
          fontSize: 34,
          fontWeight: 850,
          letterSpacing: 0.2,
          boxShadow: "0 16px 42px rgba(22,133,107,0.35)",
        }}
      >
        {label} →
      </div>
    </div>
  );
};
