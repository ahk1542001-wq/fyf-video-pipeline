export interface MouthCue {
  start: number;
  end: number;
  value: "A" | "B" | "C" | "D" | "E" | "F" | "G" | "H" | "X";
}

export type TreatmentType =
  | "story_scene"
  | "object_action"
  | "ui_proof"
  | "editorial_data"
  | "comparison_transform"
  | "motion_diagram"
  | "kinetic_type"
  | "mascot_performance";

export interface VisualTreatment {
  treatment_type: TreatmentType;
  focal_object: string;
  action: string;
  change: string;
  visual_world: string;
  motion_family: "camera" | "object" | "interface" | "diagram" | "typography" | "character";
  text_mode: "none" | "caption" | "label" | "kinetic" | "data";
  attention_reset: boolean;
  director_reason: string;
}

export interface VisualBase {
  phase: "setup" | "in_progress" | "completed" | "alert";
  camera: "wide" | "push_in" | "close_up" | "over_shoulder";
  screen_text: string[]; // 1-2 strings
  evidence_claims?: Array<{
    claim_id: string;
    statement: string;
    evidence_type: "count" | "comparison" | "state" | "sequence" | "relationship" | "concept";
    values: string[];
  }>;
  evidence_shots?: Array<{
    shot_id: string;
    proves_claim_ids: string[];
    prompt: string;
    caption: string;
    hold_fraction: number;
    media_type?: "generated_image" | "motion_graphic" | "generated_video";
    motion_preset?: "slow_push" | "pan_left" | "pan_right" | "drift" | "static";
    transition?: "cut" | "crossfade" | "push" | "wipe";
    composition?: "full_bleed" | "focal_center" | "split_stage";
    mascot_presence?: "none" | "reaction" | "explain";
    motion_spec?: {
      layout: "count" | "comparison" | "sequence" | "relationship" | "directional_branch" | "concept";
      labels: string[];
      values: string[];
      object_count?: number | null;
      accent_index?: number | null;
      relation_mode?: "directional" | "bidirectional" | "non_replacement" | null;
    } | null;
    asset_path?: string | null;
    fallback_asset_path?: string | null;
     fallback_used?: boolean;
     treatment?: VisualTreatment | null;
     verification_status: "planned" | "passed";

  }>;
}

export interface GenericVisual extends VisualBase {
  kind: "generic";
}

export interface AutoActionVisual extends VisualBase {
  kind: "auto_action";
  action: "reorder" | "pause_notify";
  severity: "mistake" | "warning";
}

export interface ConsequenceVisual extends VisualBase {
  kind: "consequence";
  mode: "loss_chart" | "three_impacts";
  items: string[]; // min 1, max 3
}

export interface ProcessTimelineVisual extends VisualBase {
  kind: "process_timeline";
  step: "detect" | "audit";
  active_step: number;
  total_steps: number;
}

export interface HumanVerificationVisual extends VisualBase {
  kind: "human_verification";
  mode: "count" | "checklist" | "approve";
  options?: string[]; // max 2
}

export interface ApprovalRecordVisual extends VisualBase {
  kind: "approval_record";
  reviewer: string;
  evidence: string;
  decision: string;
}

export interface BalancePairVisual extends VisualBase {
  kind: "balance_pair";
  left_label: string;
  right_label: string;
}

export interface OutroVisual extends VisualBase {
  kind: "outro";
  tagline: string;
}

export interface InventoryMismatchVisual extends VisualBase {
  kind: "inventory_mismatch";
  physical_stock: number;
  system_stock: number;
}

export interface ApprovalGateVisual extends VisualBase {
  kind: "approval_gate";
  actor: "ai" | "human" | "both";
  physical_stock?: number | null;
  system_stock?: number | null;
}

export interface InventoryCorrectionVisual extends VisualBase {
  kind: "inventory_correction";
  from_value: number;
  to_value: number;
  completion_ui?: boolean | null;
}

export type VisualType =
  | GenericVisual
  | AutoActionVisual
  | ConsequenceVisual
  | ProcessTimelineVisual
  | HumanVerificationVisual
  | ApprovalRecordVisual
  | BalancePairVisual
  | OutroVisual
  | InventoryMismatchVisual
  | ApprovalGateVisual
  | InventoryCorrectionVisual;

export interface ScriptSegment {
  startFrame: number;
  endFrame: number;
  id?: string;
  text: string;
  visual_action?: string;
  scene_type: "whiteboard" | "demo";
  mascot_action: "present" | "explain" | "think" | "warn" | "approve";
  emotion: "neutral" | "warm" | "focused" | "concerned" | "confident";
  emphasis: string[];
  visual?: VisualType | null;
}

export interface RenderInput extends Record<string, unknown> {
  title: string;
  language: string;
  fps: number;
  durationInFrames: number;
  audioSrc?: string;
  segments: ScriptSegment[];
  mouthCues: MouthCue[];
  mouthCueSource?: "rhubarb-phonetic" | "burmese-text-audio" | "amplitude-fallback";
  segmentTimingSource?: "single-segment" | "text-weight-fallback" | "wav-silence-snap";
}
