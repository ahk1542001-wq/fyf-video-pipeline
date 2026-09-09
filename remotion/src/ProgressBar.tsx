import React from "react";
import {interpolate, useCurrentFrame, useVideoConfig} from "remotion";
import {theme} from "./theme";

export type ProgressBarProps = {
  position?: "top" | "bottom";
  reducedMotion?: boolean;
};

/** Deterministic retention progress indicator for the active render timeline. */
export const ProgressBar: React.FC<ProgressBarProps> = ({position = "bottom", reducedMotion = false}) => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const progress = reducedMotion ? 1 : interpolate(frame, [0, Math.max(1, durationInFrames)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const edgeStyle = position === "top" ? {top: 62} : {bottom: 36};

  return (
    <div
      aria-hidden="true"
      style={{
        position: "absolute",
        left: 80,
        right: 80,
        height: 5,
        ...edgeStyle,
        borderRadius: 999,
        background: "rgba(168,183,162,0.62)",
        overflow: "hidden",
        zIndex: 40,
      }}
    >
      <div
        style={{
          height: "100%",
          width: `${progress * 100}%`,
          borderRadius: 999,
          background: `linear-gradient(90deg, ${theme.colors.primary}, ${theme.colors.accent})`,
        }}
      />
    </div>
  );
};
