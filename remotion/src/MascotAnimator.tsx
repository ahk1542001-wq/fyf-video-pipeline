import React from "react";
import { Img, useCurrentFrame, interpolate, staticFile, useVideoConfig } from "remotion";
import { ScriptSegment, MouthCue } from "./types";

interface MascotAnimatorProps {
  startFrame: number;
  mascotAction: ScriptSegment["mascot_action"];
  emotion: ScriptSegment["emotion"];
  mouthCues: MouthCue[];
  isTypedVisual?: boolean;
  mascotPosition?: "bottom_left" | "bottom_right" | "center_stage";
}

// Map logical actions/emotions to one of 8 deterministic poses (0-7)
// Pose 0: Neutral / base
// Pose 1: Subtle shift
// Pose 2: Small gesture
// Pose 3: Think / gesture
// Pose 4: Focused / serious
// Pose 5: Warm / subtle smile
// Pose 6: Explain / active
// Pose 7: Big gesture / confident
function getPoseIndex(mascotAction: string, emotion: string, startFrame: number): number {
  // Use a slow deterministic cycle between two valid poses for the action based ONLY on startFrame
  // so the pose changes only on scene/segment changes, not rapidly looping.
  const cycle = Math.floor(startFrame / 150) % 2;

  if (mascotAction === "explain") {
    return cycle === 0 ? 6 : 7;
  }
  if (mascotAction === "think") {
    return cycle === 0 ? 3 : 2;
  }
  if (mascotAction === "warn") {
    return cycle === 0 ? 4 : 0;
  }
  if (mascotAction === "approve") {
    return cycle === 0 ? 5 : 1;
  }

  // Default present / neutral fallback
  if (emotion === "confident") return cycle === 0 ? 7 : 0;
  if (emotion === "warm") return cycle === 0 ? 5 : 0;

  return cycle === 0 ? 0 : 1;
}

export const MascotAnimator: React.FC<MascotAnimatorProps> = ({
  startFrame,
  mascotAction,
  emotion,
  mouthCues,
  isTypedVisual,
  mascotPosition,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const localFrame = frame - startFrame;
  const timeInSeconds = frame / fps;

  // Entrance animation runs once per video; segment-local motion resets separately.
  const p = interpolate(frame, [0, 12], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Calculate subtle stable baseline motion. No rapid mechanical loop, warp, or whole-sprite rotation.
  const breathingScale = 1 + (Math.sin(localFrame / 24) * 0.005);

  const sceneMotion = 0;

  const poseIndex = getPoseIndex(mascotAction, emotion, startFrame);

  const cellW = 192;
  const cellH = 208;

  // Layout styling based on typed vs semantic
  const isCenterStage = isTypedVisual && mascotPosition === "center_stage";
  const baseStyle: React.CSSProperties = {
    position: "absolute",
    bottom: isCenterStage ? 96 : isTypedVisual ? 96 : 150,
    width: isCenterStage ? 500 : isTypedVisual ? 250 : 330,
    height: isCenterStage ? 590 : isTypedVisual ? 295 : 390,
    opacity: p,
    transformOrigin: "bottom center",
  };

  if (isTypedVisual) {
    if (isCenterStage) {
      baseStyle.left = "50%";
      baseStyle.transform = `translate(-50%, ${(1 - p) * 50}px) scale(${(0.92 + p * 0.08) * breathingScale})`;
    } else {
      if (mascotPosition === "bottom_right") {
        baseStyle.right = 48;
      } else {
        baseStyle.left = 48;
      }
      baseStyle.transform = `translate(${sceneMotion}px, ${(1 - p) * 50}px) scale(${(0.92 + p * 0.08) * breathingScale})`;
    }
    baseStyle.zIndex = 5;
  } else {
    baseStyle.left = 24;
    baseStyle.transform = `translate(0px, ${(1 - p) * 50}px) scale(${(0.92 + p * 0.08) * breathingScale})`;
  }

  // Calculate dynamic dimensions for container
  const containerW = isCenterStage ? 500 : isTypedVisual ? 250 : 330;
  const containerH = isCenterStage ? 590 : isTypedVisual ? 295 : 390;

  // Calculate scale to "contain" the 192x208 cell inside 330x390
  const scaleX = containerW / cellW;
  const scaleY = containerH / cellH;
  const renderScale = Math.min(scaleX, scaleY);

  const renderW = cellW * renderScale;
  const renderH = cellH * renderScale;
  const letterboxX = (containerW - renderW) / 2;
  const letterboxY = (containerH - renderH) / 2;

  // Anchor positions for each pose [0-7] based on x=[104,112,114,114,105,119,126,107], y=[67,68,68,68,68,68,69,69]
  // Shift the replacement mouth slightly toward the visual center of the face.
  // The source portraits are three-quarter views, so the raw muzzle edge is too far right
  // for a larger cartoon viseme and makes speech look detached from the character.
  const sourceAnchorsX = [99, 107, 109, 109, 100, 114, 121, 102];
  const sourceAnchorsY = [67, 68, 68, 68, 68, 68, 69, 69];

  // Map absolute source pixel to container pixel via explicit math
  const anchorX = letterboxX + (sourceAnchorsX[poseIndex] * renderScale);
  const anchorY = letterboxY + (sourceAnchorsY[poseIndex] * renderScale);

  // Find current mouth cue
  let currentCue: MouthCue["value"] = "X"; // Default closed
  for (const cue of mouthCues) {
    if (timeInSeconds >= cue.start && timeInSeconds < cue.end) {
      currentCue = cue.value;
      break;
    }
  }

  // Real 2D cartoon replacement-mouth visemes matching official Rhubarb A-H-X semantics
  // Filled dark mouth interiors, cream teeth where relevant, muted warm tongue where relevant, thin ink outline.
  const mouthPaths: Record<MouthCue["value"], React.ReactNode> = {
    // Relaxed closed X
    X: (
      <>
        <path d="M 1 2.4 C 2.5 0.8, 5.5 0.8, 7 2.4 C 5.5 5.2, 2.5 5.2, 1 2.4 Z" fill="#30382C" stroke="none" />
        <path d="M 1.8 2.7 C 3.1 3.15, 4.9 3.15, 6.2 2.7" stroke="#F4F0E6" strokeWidth="0.3" strokeLinecap="round" fill="none" />
      </>
    ),
    // Pressed closed A
    A: (
      <>
        <path d="M 1 2.5 C 2.7 1.2, 5.3 1.2, 7 2.5 C 5.4 4.5, 2.6 4.5, 1 2.5 Z" fill="#30382C" stroke="none" />
        <path d="M 1.8 2.7 C 3.2 2.95, 4.8 2.95, 6.2 2.7" stroke="#F4F0E6" strokeWidth="0.2" strokeLinecap="round" fill="none" />
      </>
    ),
    // Teeth B (slightly open with teeth)
    B: (
      <>
        <path d="M 1 2 C 4 1, 7 2, 7 4 C 4 3, 1 4, 1 2 Z" fill="#30382C" stroke="#30382C" strokeWidth="0.5" />
        <path d="M 2 2.5 L 6 2.5" stroke="#F4F0E6" strokeWidth="1" strokeLinecap="round" />
      </>
    ),
    // Open C
    C: <path d="M 1 2 C 4 0.5, 7 2, 6 5 C 4 6, 2 6, 1 2 Z" fill="#30382C" stroke="#30382C" strokeWidth="0.5" strokeLinejoin="round" />,
    // Wide open D (with tongue)
    D: (
      <>
        <path d="M 0.5 2 C 4 0, 7.5 2, 7 6.5 C 4 8, 1 8, 0.5 2 Z" fill="#30382C" stroke="#30382C" strokeWidth="0.5" strokeLinejoin="round" />
        <path d="M 2 5.5 C 4 4.5, 6 5.5, 5.5 6.5 C 4 7, 2.5 7, 2 5.5 Z" fill="#B36B65" />
      </>
    ),
    // Rounded E
    E: <path d="M 2.5 2 C 5.5 2, 5.5 5, 2.5 5 C -0.5 5, -0.5 2, 2.5 2 Z" fill="#30382C" stroke="#30382C" strokeWidth="0.5" />,
    // Puckered F
    F: <path d="M 3 2.5 C 5 2.5, 5 4, 3 4 C 1 4, 1 2.5, 3 2.5 Z" fill="#30382C" stroke="#30382C" strokeWidth="0.5" />,
    // F/V teeth-lip G (upper teeth on lower lip)
    G: (
      <>
        <path d="M 1.5 2.5 C 4 1.5, 6.5 2.5, 6 4 C 4 4.5, 2 4.5, 1.5 2.5 Z" fill="#30382C" stroke="#30382C" strokeWidth="0.5" />
        <path d="M 2 2.5 C 4 2, 6 2.5, 6 2.5" stroke="#F4F0E6" strokeWidth="1" strokeLinecap="round" />
      </>
    ),
    // Tongue/teeth H
    H: (
      <>
        <path d="M 1 2 C 4 1, 7 2, 6.5 4.5 C 4 5, 1.5 5, 1 2 Z" fill="#30382C" stroke="#30382C" strokeWidth="0.5" />
        <path d="M 2 3 C 4 2, 6 3, 5.5 4 C 4 4.5, 2.5 4.5, 2 3 Z" fill="#B36B65" />
      </>
    ),
  };

  return (
    <div style={baseStyle}>
      {/* Atlas Viewport (shows exactly 1 of 8 cells) */}
      <div style={{
        position: "absolute",
        top: 0,
        left: 0,
        width: "100%",
        height: "100%",
        overflow: "hidden"
      }}>
        <Img
          src={staticFile("fyf-mascot-talking-atlas.png")}
          style={{
            position: "absolute",
            width: `${1536 * renderScale}px`,
            height: `${208 * renderScale}px`,
            left: `${letterboxX - (poseIndex * cellW * renderScale)}px`,
            top: `${letterboxY}px`,
            filter: "drop-shadow(0 18px 24px rgba(48, 56, 44, 0.16))",
          }}
        />
      </div>

      {/* Dynamic Mouth Overlay anchored to current pose cell */}
      <div
        style={{
          position: "absolute",
          top: `${anchorY}px`,
          left: `${anchorX}px`,
          transform: "translate(-50%, -50%)", // Center mouth exactly on anchor point
          width: "32px",
          height: "25px",
          zIndex: 10,
        }}
      >
        <svg
          viewBox="0 0 8 8"
          style={{ width: "100%", height: "100%", overflow: "visible" }}
        >
          {mouthPaths[currentCue]}
        </svg>
      </div>
    </div>
  );
};
