import React from "react";
import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {theme} from "./theme";

const TOTAL_FRAMES = 420;

const SceneImage: React.FC<{
  src: string;
  from: number;
  to: number;
  scaleFrom?: number;
  scaleTo?: number;
  xFrom?: number;
  xTo?: number;
}> = ({src, from, to, scaleFrom = 1.03, scaleTo = 1.1, xFrom = 0, xTo = 0}) => {
  const frame = useCurrentFrame();
  const local = frame - from;
  const opacity = interpolate(local, [0, 10, Math.max(11, to - from - 10), to - from], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const scale = interpolate(local, [0, to - from], [scaleFrom, scaleTo], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const x = interpolate(local, [0, to - from], [xFrom, xTo], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{opacity, overflow: "hidden"}}>
      <Img
        src={staticFile(src)}
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

const BeatLabel: React.FC<{from: number; text: string; tone?: "normal" | "warning"}> = ({from, text, tone = "normal"}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame: frame - from, fps, config: {damping: 18, stiffness: 150}});
  return (
    <div style={{
      position: "absolute",
      top: 150,
      left: 72,
      right: 72,
      display: "flex",
      justifyContent: "center",
      opacity: enter,
      transform: `translateY(${interpolate(enter, [0, 1], [-28, 0])}px)`,
      zIndex: 20,
    }}>
      <div style={{
        borderRadius: 999,
        padding: "17px 32px 20px",
        background: tone === "warning" ? "rgba(201,95,69,0.94)" : "rgba(247,244,235,0.94)",
        color: tone === "warning" ? "#fffaf2" : theme.colors.text,
        boxShadow: "0 16px 42px rgba(35,45,38,0.18)",
        fontFamily: theme.fonts.display,
        fontSize: 42,
        lineHeight: 1.35,
        fontWeight: 800,
        textAlign: "center",
      }}>{text}</div>
    </div>
  );
};

const CountBadge: React.FC<{from: number; value: string; label: string; side: "left" | "right"; warning?: boolean}> = ({from, value, label, side, warning}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame: frame - from, fps, config: {damping: 16, stiffness: 165}});
  return (
    <div style={{
      position: "absolute",
      top: 1230,
      [side]: 64,
      width: 300,
      borderRadius: 30,
      padding: "26px 24px 28px",
      background: warning ? "rgba(246,216,206,0.96)" : "rgba(247,244,235,0.96)",
      border: `4px solid ${warning ? "#c95f45" : theme.colors.primary}`,
      boxShadow: "0 22px 50px rgba(35,45,38,0.22)",
      textAlign: "center",
      opacity: enter,
      transform: `scale(${0.78 + enter * 0.22})`,
      zIndex: 22,
    }}>
      <div style={{fontFamily: theme.fonts.display, fontSize: 92, lineHeight: 1, fontWeight: 900, color: warning ? "#b64f39" : theme.colors.primary}}>{value}</div>
      <div style={{marginTop: 14, fontFamily: theme.fonts.display, fontSize: 31, lineHeight: 1.35, fontWeight: 750, color: theme.colors.text}}>{label}</div>
    </div>
  );
};

export const VisualSystemV3Preview: React.FC = () => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const progress = frame / durationInFrames;
  const mascotIn = spring({frame: frame - 124, fps: 30, config: {damping: 17, stiffness: 145}});
  const mascotOut = interpolate(frame, [174, 188], [1, 0], {extrapolateLeft: "clamp", extrapolateRight: "clamp"});

  return (
    <AbsoluteFill style={{background: "#eee9dd", overflow: "hidden"}}>
      {/* The public preview is intentionally silent; production audio is job-local. */}

      <SceneImage src="fyf-v2/scene-a1.png" from={0} to={108} scaleFrom={1.02} scaleTo={1.09} xFrom={0} xTo={-24} />
      <SceneImage src="fyf-v2/scene-a1-system.png" from={96} to={198} scaleFrom={1.08} scaleTo={1.15} xFrom={22} xTo={-12} />
      <SceneImage src="fyf-v2/scene-a2.png" from={186} to={294} scaleFrom={1.02} scaleTo={1.1} xFrom={-18} xTo={20} />
      <SceneImage src="v3-preview-overflow.png" from={282} to={TOTAL_FRAMES + 1} scaleFrom={1.06} scaleTo={1.14} xFrom={12} xTo={-18} />

      {frame >= 8 && frame < 96 && <BeatLabel from={8} text="ဂိုဒေါင်ထဲမှာ တကယ် ၁၂ ခု" />}
      {frame >= 18 && frame < 98 && <CountBadge from={18} value="၁၂" label="အပြင်လက်ကျန်" side="right" />}

      {frame >= 108 && frame < 190 && <BeatLabel from={108} text="ဒါပေမယ့် စနစ်က ၂ ခုပဲမြင်တယ်" tone="warning" />}
      {frame >= 118 && frame < 190 && <CountBadge from={118} value="၂" label="စနစ်ထဲကစာရင်း" side="right" warning />}

      {frame >= 124 && frame < 188 && <Img src={staticFile("fyf-mascot-presenting.png")} style={{position: "absolute", left: 28, bottom: 42, width: 270, opacity: mascotIn * mascotOut, transform: `translateY(${interpolate(mascotIn, [0, 1], [70, 0])}px) scale(${0.84 + mascotIn * 0.16})`, transformOrigin: "bottom left", zIndex: 24, filter: "drop-shadow(0 16px 28px rgba(43,55,47,0.2))"}} />}

      {frame >= 198 && frame < 286 && <BeatLabel from={198} text="AI က အော်ဒါ ထပ်တင်လိုက်တယ်" />}
      {frame >= 296 && frame < 416 && <BeatLabel from={296} text="မလိုအပ်တဲ့ ပစ္စည်းတွေ ရောက်လာတယ်" tone="warning" />}

      <div style={{position: "absolute", top: 62, left: 80, right: 80, height: 5, borderRadius: 4, background: "rgba(168,183,162,0.62)", overflow: "hidden", zIndex: 30}}>
        <div style={{height: "100%", width: `${progress * 100}%`, background: theme.colors.primary}} />
      </div>
      <div style={{position: "absolute", right: 64, bottom: 58, zIndex: 30, color: theme.colors.text, fontFamily: theme.fonts.display, textAlign: "right"}}>
        <div style={{fontWeight: 850, fontSize: 26}}>FYF</div>
        <div style={{fontSize: 13, opacity: 0.62}}>Visual System V3 Preview</div>
      </div>
    </AbsoluteFill>
  );
};
