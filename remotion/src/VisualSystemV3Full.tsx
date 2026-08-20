import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {MascotAnimator} from "./MascotAnimator";
import {
  CinematicVisual,
  isCinematicVisual,
  shouldShowCinematicMascot,
} from "./CinematicVisual";
import {theme} from "./theme";
import {RenderInput} from "./types";
import {nonDuplicateStoryLabels} from "./storyLabels";
import {selectActiveTreatment, shouldShowOverlayLabel} from "./treatmentRouting";

type V3Input = RenderInput & {
  v3SceneAssets?: string[][];
  v3MascotSegments?: number[];
};

const StoryPlate: React.FC<{
  asset: string;
  localFrame: number;
  beatFrames: number;
  direction: number;
}> = ({asset, localFrame, beatFrames, direction}) => {
  const fade = interpolate(
    localFrame,
    [0, 9, Math.max(10, beatFrames - 10), beatFrames],
    [0, 1, 1, 0],
    {extrapolateLeft: "clamp", extrapolateRight: "clamp"},
  );
  const scale = interpolate(localFrame, [0, beatFrames], [1.025, 1.09], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const x = interpolate(localFrame, [0, beatFrames], [direction * 14, direction * -18], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{opacity: fade, overflow: "hidden"}}>
      <Img
        src={staticFile(asset)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform: `translateX(${x}px) scale(${scale})`,
          transformOrigin: "center center",
        }}
      />
    </AbsoluteFill>
  );
};

const activeBeatTiming = (
  localFrame: number,
  segmentFrames: number,
  labelsCount: number,
  weights: number[],
) => {
  const beatCount = Math.max(1, labelsCount);
  if (weights.length !== beatCount) {
    const duration = segmentFrames / beatCount;
    const index = Math.min(beatCount - 1, Math.floor(localFrame / duration));
    return {index, localFrame: localFrame - index * duration, startFrame: index * duration};
  }
  const total = weights.reduce((sum, value) => sum + value, 0);
  const position = Math.min(0.999, localFrame / segmentFrames) * total;
  let boundary = 0;
  for (let index = 0; index < weights.length; index += 1) {
    const start = boundary;
    boundary += weights[index];
    if (position < boundary) {
      const beatStartFrame = start / total * segmentFrames;
      return {index, localFrame: localFrame - beatStartFrame, startFrame: beatStartFrame};
    }
  }
  return {index: beatCount - 1, localFrame: 0, startFrame: 0};
};

const StoryLabel: React.FC<{
  text: string;
  localFrame: number;
  warning: boolean;
}> = ({text, localFrame, warning}) => {
  const {fps} = useVideoConfig();
  const enter = spring({frame: localFrame - 5, fps, config: {damping: 18, stiffness: 150}});
  return (
    <div
      style={{
        position: "absolute",
        top: 142,
        left: 68,
        right: 68,
        display: "flex",
        justifyContent: "center",
        opacity: enter,
        transform: `translateY(${interpolate(enter, [0, 1], [-28, 0])}px)`,
        zIndex: 30,
      }}
    >
      <div
        style={{
          maxWidth: 900,
          borderRadius: 999,
          padding: "17px 32px 20px",
          background: warning ? "rgba(201,95,69,0.94)" : "rgba(247,244,235,0.94)",
          color: warning ? "#fffaf2" : theme.colors.text,
          boxShadow: "0 16px 42px rgba(35,45,38,0.18)",
          fontFamily: theme.fonts.display,
          fontSize: 40,
          lineHeight: 1.35,
          fontWeight: 800,
          textAlign: "center",
        }}
      >
        {text}
      </div>
    </div>
  );
};

export const VisualSystemV3Full: React.FC<V3Input> = (props) => {
  const frame = useCurrentFrame();
  const {durationInFrames, fps} = useVideoConfig();
  const segmentIndex = Math.max(0, props.segments.findIndex((segment) => frame >= segment.startFrame && frame < segment.endFrame));
  const segment = props.segments[segmentIndex] ?? props.segments[props.segments.length - 1];
  const local = Math.max(0, frame - segment.startFrame);
  const segmentFrames = Math.max(1, segment.endFrame - segment.startFrame);
  const evidenceLabels = segment.visual?.evidence_shots
    ?.flatMap((shot) => shot.motion_spec?.labels ?? []) ?? [];
  const labels = segment.visual?.screen_text?.length
    ? nonDuplicateStoryLabels(segment.visual.screen_text, evidenceLabels)
    : [segment.text];
  const passedShots = segment.visual?.evidence_shots?.filter((shot) => shot.verification_status === "passed") ?? [];
  const approvedAssets = props.v3SceneAssets?.[segmentIndex];
  const approvedPreset = Boolean(approvedAssets?.length);
  const beatWeights = approvedPreset ? [] : passedShots.map((shot) => shot.hold_fraction);
  const beat = activeBeatTiming(local, segmentFrames, labels.length, beatWeights);
  const beatIndex = beat.index;
  const beatLocal = beat.localFrame;
  const label = labels[Math.min(beatIndex, labels.length - 1)];
  const warning = segment.visual?.phase === "alert" || (approvedPreset && segmentIndex === 0 && beatIndex === 1);
  const cinematic = isCinematicVisual(segment.visual);
  if (!approvedPreset && (!cinematic || !segment.visual)) {
    throw new Error(`V3 requires verified visual evidence for segment ${segment.id ?? segmentIndex}`);
  }
  const activeTreatment = selectActiveTreatment(passedShots, local, segmentFrames);
  const showOverlayLabel = approvedPreset || (evidenceLabels.length === 0 && shouldShowOverlayLabel(activeTreatment));
  const showMascot = approvedPreset
    ? (props.v3MascotSegments ?? []).includes(segmentIndex) && beatIndex === 0
    : activeTreatment?.treatment_type === "mascot_performance" || shouldShowCinematicMascot(segment.visual, local, fps, segmentFrames);
  const progress = frame / Math.max(1, durationInFrames);

  return (
    <AbsoluteFill style={{background: "#eee9dd", overflow: "hidden"}}>
      {props.audioSrc && <Audio src={staticFile(props.audioSrc)} />}
      {approvedPreset ? (
        <StoryPlate
          asset={approvedAssets![Math.min(beatIndex, approvedAssets!.length - 1)]}
          localFrame={beatLocal}
          beatFrames={segmentFrames / Math.max(1, labels.length)}
          direction={beatIndex % 2 === 0 ? 1 : -1}
        />
      ) : (
        <CinematicVisual
          visual={segment.visual!}
          startFrame={segment.startFrame}
          endFrame={segment.endFrame}
        />
      )}
      {showOverlayLabel && label && <StoryLabel text={label} localFrame={beatLocal} warning={warning} />}

      {showMascot && (
        <MascotAnimator
          startFrame={segment.startFrame + beat.startFrame}
          mascotAction={segment.mascot_action}
          emotion={segment.emotion}
          mouthCues={props.mouthCues}
          isTypedVisual
           mascotPosition={activeTreatment?.treatment_type === "mascot_performance" ? "center_stage" : "bottom_left"}
        />
      )}

      <div style={{position: "absolute", top: 62, left: 80, right: 80, height: 5, borderRadius: 4, background: "rgba(168,183,162,0.62)", overflow: "hidden", zIndex: 40}}>
        <div style={{height: "100%", width: `${progress * 100}%`, background: theme.colors.primary}} />
      </div>
      <div style={{position: "absolute", right: 64, bottom: 58, zIndex: 40, color: theme.colors.text, fontFamily: theme.fonts.display, textAlign: "right"}}>
        <div style={{fontWeight: 850, fontSize: 26}}>FYF</div>
        <div style={{fontSize: 13, opacity: 0.62}}>Understand AI. Build Real Systems.</div>
      </div>
    </AbsoluteFill>
  );
};
