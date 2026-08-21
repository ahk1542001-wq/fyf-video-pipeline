import { Composition } from "remotion";
import { VideoPipeline } from "./VideoPipeline";
import { RenderInput } from "./types";
import {VisualSystemV3Preview} from "./VisualSystemV3Preview";
import {VisualSystemV3Full} from "./VisualSystemV3Full";

// Sample script data (in production this comes from the FastAPI backend)
const sampleInput: RenderInput = {
  title: "AI Agent",
  language: "my-MM",
  fps: 30,
  durationInFrames: 150,
  audioSrc: "",
  segments: [
    {
      startFrame: 0,
      endFrame: 45,
      text: "ဒါက သာမန် AI မဟုတ်ပါဘူး။",
      scene_type: "demo",
      mascot_action: "present",
      emotion: "neutral",
      emphasis: [],
    },
    {
      startFrame: 45,
      endFrame: 90,
      text: "ကိုယ့်ဘာသာ စဉ်းစားပြီး အလုပ်လုပ်တဲ့ **AI Agent** ပါ။",
      scene_type: "demo",
      mascot_action: "explain",
      emotion: "confident",
      emphasis: ["AI", "Agent"],
    },
    {
      startFrame: 90,
      endFrame: 150,
      text: "မင်းရဲ့ အလုပ်တွေကို **အလိုအလျောက်** လုပ်ပေးပါလိမ့်မယ်။",
      scene_type: "whiteboard",
      mascot_action: "approve",
      emotion: "warm",
      emphasis: ["အလိုအလျောက်"],
    },
  ],
  mouthCues: [
    { start: 0.1, end: 0.5, value: "A" },
    { start: 0.5, end: 1.0, value: "B" },
    { start: 1.0, end: 1.5, value: "C" },
    { start: 1.5, end: 3.0, value: "X" },
  ],
  v3SceneAssets: [
    ["fyf-v2/scene-a1.png"],
    ["fyf-v2/scene-a1-system.png"],
    ["fyf-v2/scene-a2.png"],
  ],
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
    <Composition
      id="VideoPipeline"
      component={VideoPipeline}
      durationInFrames={sampleInput.durationInFrames}
      fps={sampleInput.fps}
      width={1080}
      height={1920}
      defaultProps={sampleInput}
      calculateMetadata={({ props }) => {
        return {
          durationInFrames: props.durationInFrames,
          props,
        };
      }}
    />
    <Composition
      id="VisualSystemV3Preview"
      component={VisualSystemV3Preview}
      durationInFrames={420}
      fps={30}
      width={1080}
      height={1920}
    />
    <Composition
      id="VisualSystemV3Full"
      component={VisualSystemV3Full}
      durationInFrames={sampleInput.durationInFrames}
      fps={sampleInput.fps}
      width={1080}
      height={1920}
      defaultProps={sampleInput}
      calculateMetadata={({props}) => ({durationInFrames: props.durationInFrames, props})}
    />
    </>
  );
};
