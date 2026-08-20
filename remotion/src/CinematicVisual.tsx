import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {theme} from "./theme";
import {VisualType} from "./types";
import {MotionEvidence} from "./MotionEvidence";
import {TreatmentVisual} from "./TreatmentVisual";
import {resolveVisualGrammar} from "./treatmentRouting";

export const isCinematicVisual = (visual: VisualType | null | undefined) =>
  Boolean(
    visual &&
      (visual.evidence_shots?.some(
        (shot) => shot.verification_status === "passed" &&
          (shot.media_type === "motion_graphic" ? Boolean(shot.motion_spec) : Boolean(shot.asset_path)),
      ) ?? false),
  );

export const shouldShowCinematicMascot = (
  visual: VisualType | null | undefined,
  localFrame: number,
  fps: number,
  segmentFrames?: number,
) => {
  if (!visual) return false;
  const shots = visual.evidence_shots?.filter((shot) => shot.verification_status === "passed") ?? [];
  if (shots.length && segmentFrames) {
    const total = shots.reduce((sum, shot) => sum + shot.hold_fraction, 0);
    const position = Math.min(0.999, localFrame / Math.max(1, segmentFrames)) * total;
    let boundary = 0;
    for (const shot of shots) {
      boundary += shot.hold_fraction;
      if (position < boundary) return shot.mascot_presence !== "none";
    }
  }
  if (visual.kind === "inventory_mismatch") return localFrame >= fps * 4;
  if (visual.kind === "balance_pair") return localFrame >= fps * 3;
  return visual.kind === "auto_action" && visual.action === "pause_notify";
};

export const CinematicVisual: React.FC<{
  visual: VisualType;
  startFrame: number;
  endFrame: number;
}> = ({visual, startFrame, endFrame}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const localFrame = Math.max(0, frame - startFrame);
  const evidenceShots = visual.evidence_shots?.filter(
    (item) => item.verification_status === "passed" &&
      (item.media_type === "motion_graphic" ? Boolean(item.motion_spec) : Boolean(item.asset_path)),
  );
  const totalWeight = evidenceShots?.reduce((sum, item) => sum + item.hold_fraction, 0) ?? 0;
  const normalizedPosition = totalWeight > 0
    ? Math.min(0.999, localFrame / Math.max(1, endFrame - startFrame)) * totalWeight
    : 0;
  let evidenceShot = evidenceShots?.[0];
  let shotStartWeight = 0;
  if (evidenceShots?.length) {
    let boundary = 0;
    for (const item of evidenceShots) {
      const start = boundary;
      boundary += item.hold_fraction;
      if (normalizedPosition < boundary) {
        evidenceShot = item;
        shotStartWeight = start;
        break;
      }
    }
  }
  if (!evidenceShot) {
    throw new Error("CinematicVisual requires a passed evidence shot");
  }
  const shot = {
    asset: evidenceShot.asset_path ?? "",
    scale: evidenceShot.motion_preset === "static" ? 1 : 1.04,
    x: 0,
    y: 0,
    caption: evidenceShot.caption,
    motionPreset: evidenceShot.motion_preset ?? "slow_push",
  };
  const asset = shot.asset;
  const activePreset = shot.motionPreset;
  const segmentFrames = Math.max(1, endFrame - startFrame);
  const shotDurationFrames = Math.max(1, segmentFrames * evidenceShot.hold_fraction / Math.max(totalWeight, 0.001));
  const shotLocalFrame = Math.max(0, (normalizedPosition - shotStartWeight) / evidenceShot.hold_fraction * shotDurationFrames);
  const push = interpolate(shotLocalFrame, [0, fps * 12], [0, activePreset === "static" ? 0 : 0.035], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const driftX = activePreset === "pan_left"
    ? interpolate(shotLocalFrame, [0, fps * 12], [22, -22], {extrapolateLeft: "clamp", extrapolateRight: "clamp"})
    : activePreset === "pan_right"
      ? interpolate(shotLocalFrame, [0, fps * 12], [-22, 22], {extrapolateLeft: "clamp", extrapolateRight: "clamp"})
      : activePreset === "drift"
        ? Math.sin(shotLocalFrame / Math.max(1, fps) * 0.8) * 10
        : 0;
  const entrance = interpolate(shotLocalFrame, [0, Math.max(5, fps * 0.25)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const grammar = resolveVisualGrammar(evidenceShot);
  if (grammar === "motion-diagram" && evidenceShot.motion_spec) {
    return <MotionEvidence shot={evidenceShot} localFrame={shotLocalFrame} />;
  }

  if (evidenceShot?.treatment) {
    return <TreatmentVisual shot={evidenceShot} localFrame={shotLocalFrame} treatment={evidenceShot.treatment} />;
  }

  const transitionX = evidenceShot.transition === "push" ? interpolate(entrance, [0, 1], [110, 0]) : 0;
  const wipe = evidenceShot.transition === "wipe" ? `${Math.round(entrance * 100)}%` : "100%";
  const fit = evidenceShot.composition === "full_bleed" ? "cover" : "contain";

  if (evidenceShot?.media_type === "generated_video" && evidenceShot.asset_path) {
    return (
      <AbsoluteFill style={{background: "#eee9dd"}}>
        <div style={{position: "absolute", inset: 0, opacity: entrance, transform: `translateX(${transitionX}px)`, clipPath: `inset(0 ${100 - Number.parseFloat(wipe)}% 0 0)`}}>
          <OffthreadVideo muted src={staticFile(evidenceShot.asset_path)} style={{width: "100%", height: "100%", objectFit: fit}} />
        </div>
      </AbsoluteFill>
    );
  }

  return (
    <AbsoluteFill style={{background: "#eee9dd"}}>
      <Img
        src={staticFile(asset)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: fit,
          transform: `translate3d(${shot.x + driftX + transitionX}px, ${shot.y}px, 0) scale(${shot.scale + push})`,
          transformOrigin: "center center",
          opacity: entrance,
          clipPath: `inset(0 ${100 - Number.parseFloat(wipe)}% 0 0)`,
        }}
      />
    </AbsoluteFill>
  );
};
