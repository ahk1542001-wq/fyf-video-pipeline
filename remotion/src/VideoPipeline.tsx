import React from "react";
import {
  AbsoluteFill,
  useCurrentFrame,
  interpolate,
  staticFile,
  useVideoConfig,
  Audio,
} from "remotion";
import { theme, getFonts } from "./theme";
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
import { CallToAction } from "./CallToAction";
import { ProgressBar } from "./ProgressBar";
import { normalizeRenderControls, resolveReducedMotion } from "./renderControls";
import { studioBranding } from "./studioBranding";
import {
  CinematicVisual,
  isCinematicVisual,
  shouldShowCinematicMascot,
} from "./CinematicVisual";

export const VideoPipeline: React.FC<RenderInput> = (props) => {
  const frame = useCurrentFrame();
  const { durationInFrames, fps } = useVideoConfig();
  const { title, segments, audioSrc, mouthCues } = props;
  const renderControls = normalizeRenderControls(props);
  const reducedMotion = resolveReducedMotion(props);
  const fonts = getFonts(props.language);
  const branding = studioBranding(props.studio_name);
  const isEnglish = props.language === "en-US" || props.language === "en";
  const isVoiceoverOnly = props.presenter_mode === "voiceover_only";

  // Current segment based on frame
  const currentSegment = segments.find(
    (s) => frame >= s.startFrame && frame < s.endFrame,
  );
  const isDemo = currentSegment?.scene_type === "demo";
  const localFrame = frame - (currentSegment?.startFrame ?? 0);
  const cinematic = isCinematicVisual(currentSegment?.visual);
  const showMascot = !isVoiceoverOnly && (!cinematic || shouldShowCinematicMascot(
    currentSegment?.visual,
    localFrame,
    fps,
    currentSegment ? currentSegment.endFrame - currentSegment.startFrame : undefined,
  ));

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
      {renderControls.retention_progress_bar && <ProgressBar position="top" reducedMotion={reducedMotion} />}

      {/* Scene badge */}
      {!cinematic && <div style={{ position: "absolute", top: 150, left: 80, zIndex: 20 }}>
        <Entrance animate={renderControls.animated_lower_thirds}>
          <span
            style={{
              fontFamily: fonts.body,
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
                  currentSegment.visual.kind === 'inventory_mismatch' ? (isEnglish ? 'Inventory Mismatch' : 'စာရင်းကွာဟချက်') :
                  currentSegment.visual.kind === 'approval_gate' ? (isEnglish ? 'Human Gate' : 'လူသားစစ်ဆေးမှု') :
                  currentSegment.visual.kind === 'inventory_correction' ? (isEnglish ? 'System Correction' : 'စနစ်ပြင်ဆင်မှု') :
                  currentSegment.visual.kind === 'auto_action' ? (isEnglish ? 'Automated Action' : 'အလိုအလျောက်လုပ်ဆောင်မှု') :
                  currentSegment.visual.kind === 'consequence' ? (isEnglish ? 'Consequence' : 'အကျိုးဆက်') :
                  currentSegment.visual.kind === 'process_timeline' ? (isEnglish ? 'Workflow' : 'လုပ်ငန်းစဉ်') :
                  currentSegment.visual.kind === 'human_verification' ? (isEnglish ? 'Human Verification' : 'လူသားစစ်ဆေးမှု') :
                  currentSegment.visual.kind === 'approval_record' ? (isEnglish ? 'Audit Record' : 'အတည်ပြုမှတ်တမ်း') :
                  currentSegment.visual.kind === 'balance_pair' ? (isEnglish ? 'Balance' : 'ဟန်ချက်ညီမှု') :
                  currentSegment.visual.kind === 'outro' ? (isEnglish ? 'Conclusion' : 'နိဂုံး') :
                  (isEnglish ? 'General' : 'အထွေထွေ')
                }${
                  currentSegment.visual.phase === 'in_progress' ? (isEnglish ? ' - In Progress' : ' - စစ်ဆေးနေဆဲ') :
                  currentSegment.visual.phase === 'alert' ? (isEnglish ? ' - Alert' : ' - သတိပေးချက်') :
                  currentSegment.visual.phase === 'completed' && currentSegment.visual.kind === 'auto_action' && currentSegment.visual.action === 'pause_notify' ? (isEnglish ? ' - Handed Off' : ' - လူထံလွှဲပြောင်းပြီး') :
                  currentSegment.visual.phase === 'completed' ? (isEnglish ? ' - Completed' : ' - ပြီးစီး') : ''
                }`
              : (isDemo ? (isEnglish ? "Live Demo" : "လက်တွေ့ပြသမှု") : (isEnglish ? "Explainer" : "ရှင်းလင်းချက်"))}
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
                fontFamily: fonts.display,
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
                fontFamily={fonts.display}
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

      {/* Studio logo mark (bottom-right) */}
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
                fontFamily: fonts.display,
                fontWeight: 700,
                fontSize: 24,
                color: theme.colors.text,
                margin: 0,
              }}
            >
              {branding.name}
            </p>,
            <p
              key="2"
              style={{
                fontFamily: fonts.body,
                fontSize: 13,
                color: theme.colors.textDim,
                margin: "2px 0 0 0",
              }}
            >
              {branding.tagline}
            </p>,
          ]}
          start={fps}
          per={6}
        />
      </div>

      {renderControls.cta_text && <CallToAction text={renderControls.cta_text} reducedMotion={reducedMotion} />}

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
