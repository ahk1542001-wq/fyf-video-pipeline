import React from "react";
import {
  AbsoluteFill,
  useCurrentFrame,
  interpolate,
  staticFile,
  useVideoConfig,
  Audio,
} from "remotion";
import { theme } from "./theme";
import { MascotAnimator } from "./MascotAnimator";
import { RenderInput } from "./types";
import {
  Entrance,
  Stagger,
  WordReveal,
  BgMesh,
  Grain,
  Vignette,
  Breathe,
} from "./components";
import { SemanticVisual } from "./SemanticVisual";
import { TypedVisual } from "./TypedVisual";
import {
  CinematicVisual,
  isCinematicVisual,
  shouldShowCinematicMascot,
} from "./CinematicVisual";

export const VideoPipeline: React.FC<RenderInput> = (props) => {
  const frame = useCurrentFrame();
  const { durationInFrames, fps } = useVideoConfig();
  const { title, segments, audioSrc, mouthCues } = props;

  // Current segment based on frame
  const currentSegment = segments.find(
    (s) => frame >= s.startFrame && frame < s.endFrame,
  );
  const isDemo = currentSegment?.scene_type === "demo";
  const localFrame = frame - (currentSegment?.startFrame ?? 0);
  const cinematic = isCinematicVisual(currentSegment?.visual);
  const showMascot = !cinematic || shouldShowCinematicMascot(
    currentSegment?.visual,
    localFrame,
    fps,
    currentSegment ? currentSegment.endFrame - currentSegment.startFrame : undefined,
  );

  // Progress (for the top progress bar)
  const progress = interpolate(frame, [0, durationInFrames], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, overflow: "hidden" }}>
      {audioSrc && <Audio src={staticFile(audioSrc)} />}

      {/* Layer 1: background mesh — only for legacy/semantic */}
      {!currentSegment?.visual && <BgMesh dark={isDemo} />}

      {/* Structural Scene Animations */}
      {currentSegment?.visual ? (
        cinematic ? (
          <CinematicVisual visual={currentSegment.visual} startFrame={currentSegment.startFrame} endFrame={currentSegment.endFrame} />
        ) : (
          <TypedVisual visual={currentSegment.visual} startFrame={currentSegment.startFrame} />
        )
      ) : (
        <SemanticVisual
          visualAction={currentSegment?.visual_action}
          sceneType={currentSegment?.scene_type}
          startFrame={currentSegment?.startFrame ?? 0}
        />
      )}

      {/* Layer 2: content */}
      {/* Top progress bar */}
      <div style={{ position: "absolute", top: cinematic ? 62 : 120, left: 80, right: 80 }}>
        <div
          style={{
            height: 5,
            background: theme.colors.accent,
            borderRadius: 3,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${progress * 100}%`,
              background: theme.colors.primary,
              borderRadius: 3,
            }}
          />
        </div>
      </div>

      {/* Scene badge */}
      {!cinematic && <div style={{ position: "absolute", top: 150, left: 80, zIndex: 20 }}>
        <Entrance>
          <span
            style={{
              background: theme.colors.accent,
              color: theme.colors.text,
              fontSize: 15,
              fontWeight: 600,
              padding: "7px 18px",
              borderRadius: 999,
            }}
          >
            {currentSegment?.visual
              ? `${
                  currentSegment.visual.kind === 'inventory_mismatch' ? 'စာရင်းကွာဟချက်' :
                  currentSegment.visual.kind === 'approval_gate' ? 'လူသားစစ်ဆေးမှု' :
                  currentSegment.visual.kind === 'inventory_correction' ? 'စနစ်ပြင်ဆင်မှု' :
                  currentSegment.visual.kind === 'auto_action' ? 'အလိုအလျောက်လုပ်ဆောင်မှု' :
                  currentSegment.visual.kind === 'consequence' ? 'အကျိုးဆက်' :
                  currentSegment.visual.kind === 'process_timeline' ? 'လုပ်ငန်းစဉ်' :
                  currentSegment.visual.kind === 'human_verification' ? 'လူသားစစ်ဆေးမှု' :
                  currentSegment.visual.kind === 'approval_record' ? 'အတည်ပြုမှတ်တမ်း' :
                  currentSegment.visual.kind === 'balance_pair' ? 'ဟန်ချက်ညီမှု' :
    currentSegment.visual.kind === 'outro' ? 'နိဂုံး' :
    'အထွေထွေ'
                }${
                  currentSegment.visual.phase === 'in_progress' ? ' - စစ်ဆေးနေဆဲ' :
                  currentSegment.visual.phase === 'alert' ? ' - သတိပေးချက်' :
                  currentSegment.visual.phase === 'completed' && currentSegment.visual.kind === 'auto_action' && currentSegment.visual.action === 'pause_notify' ? ' - လူထံလွှဲပြောင်းပြီး' :
                  currentSegment.visual.phase === 'completed' ? ' - ပြီးစီး' : ''
                }`
              : (isDemo ? "လက်တွေ့ပြသမှု" : "ရှင်းလင်းချက်")}
          </span>
        </Entrance>
      </div>}

      {/* Title */}
      {title && !currentSegment?.visual && (
        <div
          style={{
            position: "absolute",
            top: 220,
            left: 0,
            right: 0,
            display: "flex",
            justifyContent: "center",
            padding: "0 60px",
            opacity: frame < fps * 3 ? 1 : interpolate(frame, [fps * 3, fps * 3 + 15], [1, 0], { extrapolateRight: "clamp" }),
          }}
        >
          <Entrance delay={5}>
            <p
              style={{
                fontFamily: theme.fonts.display,
                fontWeight: 700,
                fontSize: 44,
                letterSpacing: 0,
                color: theme.colors.text,
                textAlign: "center",
                margin: 0,
              }}
            >
              {title}
            </p>
          </Entrance>
        </div>
      )}

      {/* Main caption area — word-by-word karaoke reveal */}
      {!currentSegment?.visual && (
        <div
          style={{
            position: "absolute",
            top: 840,
            bottom: 430,
            left: 0,
            right: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "0 60px",
            pointerEvents: "none",
          }}
        >
          {currentSegment ? (
            <Breathe>
              <WordReveal
                text={currentSegment.text}
                delay={0}
                per={3}
                size={56}
                highlight={false} // Use markdown bold for selective highlight, not global highlight
                startFrame={currentSegment.startFrame}
                emphasis={currentSegment.emphasis}
              />
            </Breathe>
          ) : null}
        </div>
      )}

      {/* Approved FYF mascot */}
      {showMascot && <MascotAnimator
        startFrame={currentSegment?.startFrame ?? 0}
        mascotAction={currentSegment?.mascot_action ?? "present"}
      emotion={currentSegment?.emotion ?? "neutral"}
      mouthCues={mouthCues}
      isTypedVisual={!!currentSegment?.visual}
      mascotPosition="bottom_left"
    />}

      {/* FYF logo mark (bottom-right) */}
      <div
        style={{
          position: "absolute",
          bottom: 258,
          right: 72,
          textAlign: "right",
        }}
      >
        <Stagger
          items={[
            <p
              key="1"
              style={{
                fontFamily: theme.fonts.display,
                fontWeight: 700,
                fontSize: 24,
                color: theme.colors.text,
                margin: 0,
              }}
            >
              FYF
            </p>,
            <p
              key="2"
              style={{
                fontFamily: theme.fonts.body,
                fontSize: 13,
                color: theme.colors.textDim,
                margin: "2px 0 0 0",
              }}
            >
              Understand AI. Build Real Systems.
            </p>,
          ]}
          start={fps}
          per={6}
        />
      </div>

      {/* Layer 4: color grade (subtle warm tint) */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: "100%",
          height: "100%",
          background: cinematic ? "rgba(48,56,44,0.008)" : "rgba(48,56,44,0.03)",
          pointerEvents: "none",
        }}
      />

      {/* Layer 5: grain + vignette — must be on top */}
      {!cinematic && <Grain />}
      {!cinematic && <Vignette />}
    </AbsoluteFill>
  );
};
