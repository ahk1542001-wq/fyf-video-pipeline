export type AspectRatio = "9:16" | "16:9" | "1:1";

export type RenderControls = {
  cta_text: string;
  retention_progress_bar: boolean;
  animated_lower_thirds: boolean;
  aspect_ratio: AspectRatio;
};

export const DEFAULT_RENDER_CONTROLS: RenderControls = {
  cta_text: "",
  retention_progress_bar: true,
  animated_lower_thirds: true,
  aspect_ratio: "9:16",
};

const DIMENSIONS: Record<AspectRatio, {width: number; height: number}> = {
  "9:16": {width: 1080, height: 1920},
  "16:9": {width: 1920, height: 1080},
  "1:1": {width: 1080, height: 1080},
};

export function aspectRatioDimensions(value: unknown): {width: number; height: number} {
  if (value !== "9:16" && value !== "16:9" && value !== "1:1") {
    throw new Error(`Unsupported aspect_ratio: ${String(value)}`);
  }
  return DIMENSIONS[value];
}

export function resolveReducedMotion(
  input: Record<string, unknown> | null | undefined,
): boolean {
  const source = input && typeof input === "object" ? input : {};
  if (!Object.prototype.hasOwnProperty.call(source, "reduced_motion")) return false;
  if (typeof source.reduced_motion !== "boolean") {
    throw new Error("reduced_motion must be a boolean");
  }
  return source.reduced_motion;
}

export function normalizeRenderControls(input: Record<string, unknown> | null | undefined): RenderControls {
  const source = input && typeof input === "object" ? input : {};
  const nested = source.render_controls;
  const hasNested = Object.prototype.hasOwnProperty.call(source, "render_controls");
  if (hasNested && nested !== null && (typeof nested !== "object" || Array.isArray(nested))) {
    throw new Error("render_controls must be an object");
  }
  const nestedValues = nested as Record<string, unknown> | null | undefined;
  const controlNames = Object.keys(DEFAULT_RENDER_CONTROLS);
  if (nestedValues) {
    const unknown = Object.keys(nestedValues).filter((key) => !controlNames.includes(key));
    if (unknown.length > 0) {
      throw new Error(`render_controls contains unsupported field: ${unknown[0]}`);
    }
    for (const name of controlNames) {
      if (Object.prototype.hasOwnProperty.call(source, name)
        && Object.prototype.hasOwnProperty.call(nestedValues, name)
        && source[name] !== nestedValues[name]) {
        throw new Error(`${name} does not match render_controls`);
      }
    }
  }
  const values = nestedValues
    ? {...source, ...nestedValues}
    : source;

  let cta = DEFAULT_RENDER_CONTROLS.cta_text;
  if (Object.prototype.hasOwnProperty.call(values, "cta_text")) {
    if (typeof values.cta_text !== "string") {
      throw new Error("cta_text must be a string");
    }
    cta = values.cta_text.trim();
    if (values.cta_text.length > 0 && cta.length === 0) {
      throw new Error("cta_text must not be whitespace only");
    }
    if (cta.length > 80) {
      throw new Error("cta_text must be at most 80 characters");
    }
  }

  const retention = Object.prototype.hasOwnProperty.call(values, "retention_progress_bar")
    ? values.retention_progress_bar
    : DEFAULT_RENDER_CONTROLS.retention_progress_bar;
  if (typeof retention !== "boolean") {
    throw new Error("retention_progress_bar must be a boolean");
  }

  const requestedAnimated = Object.prototype.hasOwnProperty.call(values, "animated_lower_thirds")
    ? values.animated_lower_thirds
    : DEFAULT_RENDER_CONTROLS.animated_lower_thirds;
  if (typeof requestedAnimated !== "boolean") {
    throw new Error("animated_lower_thirds must be a boolean");
  }
  const animated = resolveReducedMotion(source) ? false : requestedAnimated;

  const aspect = Object.prototype.hasOwnProperty.call(values, "aspect_ratio")
    ? values.aspect_ratio
    : DEFAULT_RENDER_CONTROLS.aspect_ratio;
  aspectRatioDimensions(aspect);

  return {
    cta_text: cta,
    retention_progress_bar: retention,
    animated_lower_thirds: animated,
    aspect_ratio: aspect as AspectRatio,
  };
}
