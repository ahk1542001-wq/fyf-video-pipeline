import { Composition } from "remotion";
import { VideoPipeline } from "./VideoPipeline";
import {VisualSystemV3Full} from "./VisualSystemV3Full";
import {aspectRatioDimensions, normalizeRenderControls} from "./renderControls";
import {requireExplicitRenderInput} from "./types";

// Remotion requires registration-time dimensions. These are not render props;
// calculateMetadata replaces them with dimensions from the injected input.
const REGISTERED_DURATION_IN_FRAMES = 1;
const REGISTERED_FPS = 30;
const REGISTERED_WIDTH = 1080;
const REGISTERED_HEIGHT = 1920;

const ExplicitVideoPipeline: React.FC<Record<string, unknown>> = (props) => {
  const input = requireExplicitRenderInput(props);
  return <VideoPipeline {...input} />;
};

const ExplicitVisualSystemV3Full: React.FC<Record<string, unknown>> = (props) => {
  const input = requireExplicitRenderInput(props);
  return <VisualSystemV3Full {...input} />;
};

function calculateProductionMetadata({props}: {props: Record<string, unknown>}) {
  const input = requireExplicitRenderInput(props);
  const controls = normalizeRenderControls(input);
  const dimensions = aspectRatioDimensions(controls.aspect_ratio);
  return {
    durationInFrames: input.durationInFrames,
    fps: input.fps,
    width: dimensions.width,
    height: dimensions.height,
    props: {...input, ...controls, render_controls: controls},
  };
}

export const RemotionRoot: React.FC = () => {
  return (
    <>
    <Composition
      id="VideoPipeline"
      component={ExplicitVideoPipeline}
      durationInFrames={REGISTERED_DURATION_IN_FRAMES}
      fps={REGISTERED_FPS}
      width={REGISTERED_WIDTH}
      height={REGISTERED_HEIGHT}
      calculateMetadata={calculateProductionMetadata}
    />
    <Composition
      id="VisualSystemV3Full"
      component={ExplicitVisualSystemV3Full}
      durationInFrames={REGISTERED_DURATION_IN_FRAMES}
      fps={REGISTERED_FPS}
      width={REGISTERED_WIDTH}
      height={REGISTERED_HEIGHT}
      calculateMetadata={calculateProductionMetadata}
    />
    </>
  );
};
