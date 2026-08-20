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
import { theme } from "./theme";
import { VisualType } from "./types";

const surface = theme.colors.bgAlt;
const ink = theme.colors.text;
const muted = theme.colors.textDim;
const viridian = theme.colors.primary;
const sage = theme.colors.accent;
const alert = theme.colors.alert;
const border = "rgba(48, 56, 44, 0.18)";

const panel: React.CSSProperties = {
  background: "rgba(255, 255, 255, 0.82)",
  border: `2px solid ${border}`,
  borderRadius: 28,
  boxShadow: "0 22px 50px rgba(48, 56, 44, 0.10)",
};

const StatusDots: React.FC<{ frame: number }> = ({ frame }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
    {[0, 1, 2].map((index) => {
      const opacity = interpolate(
        (frame + index * 8) % 36,
        [0, 18, 36],
        [0.28, 1, 0.28],
      );
      return (
        <div
          key={index}
          style={{
            width: 12,
            height: 12,
            borderRadius: "50%",
            background: viridian,
            opacity,
          }}
        />
      );
    })}
    <span style={{ color: ink, fontSize: 22, fontWeight: 700 }}>
      လူက စစ်ဆေးနေဆဲ
    </span>
  </div>
);

const PaperWorld: React.FC<{ frame: number; visual: VisualType }> = ({ frame, visual }) => {
  const focus =
    visual.kind === "inventory_mismatch"
      ? { x: -18, y: 4, scale: 1.06 }
      : visual.kind === "auto_action" && visual.action === "reorder"
        ? { x: 12, y: -18, scale: 1.08 }
        : visual.kind === "balance_pair"
          ? { x: 0, y: -4, scale: 1.02 }
          : visual.kind === "approval_gate"
            ? { x: 12, y: -30, scale: 1.08 }
            : { x: 0, y: -22, scale: 1.05 };
  const driftX = Math.sin(frame / 80) * 4;
  const driftY = Math.cos(frame / 92) * 4;

  return (
  <AbsoluteFill
    style={{
      backgroundColor: theme.colors.bg,
      overflow: "hidden",
    }}
  >
    <Img
      src={staticFile("fyf-cut-paper-world.png")}
      style={{
        position: "absolute",
        inset: -34,
        width: "calc(100% + 68px)",
        height: "calc(100% + 68px)",
        objectFit: "cover",
        transform: `translate(${focus.x + driftX}px, ${focus.y + driftY}px) scale(${focus.scale})`,
        transformOrigin: "center center",
      }}
    />
    <AbsoluteFill style={{ background: "rgba(244,240,230,0.08)" }} />
  </AbsoluteFill>
  );
};

const EditorialLabel: React.FC<{
  eyebrow: string;
  value: string;
  tone?: "normal" | "alert";
  style?: React.CSSProperties;
}> = ({ eyebrow, value, tone = "normal", style }) => (
  <div
    style={{
      position: "absolute",
      padding: "16px 20px 18px",
      background: "rgba(244,240,230,0.94)",
      borderLeft: `7px solid ${tone === "alert" ? alert : viridian}`,
      boxShadow: "0 14px 26px rgba(48,56,44,0.13)",
      ...style,
    }}
  >
    <div style={{ color: muted, fontSize: 17, fontWeight: 800 }}>{eyebrow}</div>
    <div style={{ color: tone === "alert" ? alert : ink, fontSize: 44, fontWeight: 900, marginTop: 6 }}>{value}</div>
  </div>
);

const InventoryMismatch: React.FC<{
  visual: Extract<VisualType, { kind: "inventory_mismatch" }>;
  frame: number;
  fps: number;
}> = ({ visual, frame, fps }) => {
  const left = spring({ frame, fps, config: theme.spring.smooth });
  const right = spring({ frame: frame - Math.round(fps * 0.65), fps, config: theme.spring.smooth });
  const warning = spring({ frame: frame - Math.round(fps * 1.25), fps, config: theme.spring.snappy });
  return (
    <div style={{ position: "relative", width: 920, height: 560 }}>
      <EditorialLabel eyebrow="ဂိုဒေါင် • လက်တွေ့ရှိပစ္စည်း" value={`${visual.physical_stock} ခု`} style={{ left: 8, top: 58, opacity: left, transform: `translateY(${interpolate(left, [0, 1], [28, 0])}px)` }} />
      <EditorialLabel eyebrow="ကွန်ပျူတာစာရင်း" value={`${visual.system_stock} ခု`} tone="alert" style={{ right: 16, top: 255, opacity: right, transform: `translateY(${interpolate(right, [0, 1], [28, 0])}px)` }} />
      <div style={{ position: "absolute", left: 300, top: 185, width: 330, padding: "18px 24px", background: alert, color: "#fff", fontSize: 29, lineHeight: 1.35, fontWeight: 900, textAlign: "center", boxShadow: "0 16px 30px rgba(200,88,61,0.24)", opacity: warning, transform: `rotate(-2deg) scale(${interpolate(warning, [0, 1], [0.86, 1])})` }}>
        လက်တွေ့နဲ့ စာရင်း<br />မကိုက်ညီပါ
      </div>
    </div>
  );
};

const ApprovalGate: React.FC<{
  visual: Extract<VisualType, { kind: "approval_gate" }>;
  frame: number;
  fps: number;
}> = ({ visual, frame, fps }) => {
  const physical = visual.physical_stock;
  const system = visual.system_stock;
  const flow = interpolate(frame, [8, 32], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const reveals = [0, Math.round(fps * 0.7), Math.round(fps * 1.4)].map((delay) =>
    spring({ frame: frame - delay, fps, config: { damping: 18, stiffness: 150 } }),
  );
  const revealStyle = (index: number): React.CSSProperties => ({
    opacity: reveals[index],
    transform: `translateY(${interpolate(reveals[index], [0, 1], [46, 0])}px) scale(${interpolate(reveals[index], [0, 1], [0.9, 1])})`,
  });

  return (
    <div style={{ width: 920, position: "relative", paddingTop: 72 }}>
      <div style={{ position: "absolute", top: 4, left: 80, right: 80, height: 8, borderRadius: 8, background: sage, overflow: "hidden" }}>
        <div style={{ width: `${flow * 100}%`, height: "100%", background: viridian }} />
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "250px 310px 250px",
          gap: 40,
          alignItems: "stretch",
        }}
      >
        <div style={{ ...panel, padding: 28, ...revealStyle(0), borderRadius: 4, transform: `${revealStyle(0).transform} rotate(-1.5deg)` }}>
          <div style={{ color: viridian, fontSize: 18, fontWeight: 800 }}>
            AI အဆိုပြုချက်
          </div>
          <div style={{ color: ink, fontSize: 30, lineHeight: 1.35, fontWeight: 800, marginTop: 20 }}>
            စာရင်းကို
            <br />
            မပြင်သေးပါ
          </div>
          <div style={{ height: 6, background: sage, borderRadius: 8, marginTop: 30 }}>
            <div
              style={{
                height: "100%",
                width: `${flow * 62}%`,
                background: viridian,
                borderRadius: 8,
              }}
            />
          </div>
        </div>

        <div
          style={{
            ...panel,
            padding: 30,
            borderColor: viridian,
            borderRadius: 6,
            textAlign: "center",
            boxShadow: "0 26px 60px rgba(22,133,107,0.18)",
            ...revealStyle(1),
          }}
        >
          <div
            style={{
              width: 92,
              height: 92,
              borderRadius: "50%",
              margin: "0 auto 22px",
              background: "rgba(22,133,107,0.12)",
              border: `3px solid ${viridian}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: viridian,
              fontSize: 38,
              fontWeight: 900,
            }}
          >
            လူ
          </div>
          <div style={{ color: ink, fontSize: 28, fontWeight: 900 }}>
            အတည်မပြုမီ
            <br />
            စစ်ဆေးမည်
          </div>
          <div style={{ marginTop: 28, display: "flex", justifyContent: "center" }}>
            <StatusDots frame={frame} />
          </div>
        </div>

        <div style={{ ...panel, padding: 28, ...revealStyle(2), borderRadius: 4, transform: `${revealStyle(2).transform} rotate(1.5deg)` }}>
          <div style={{ color: muted, fontSize: 18, fontWeight: 800 }}>
            စစ်ဆေးရမည့် အထောက်အထား
          </div>
          <div style={{ marginTop: 24, color: ink, fontSize: 24, lineHeight: 1.8, fontWeight: 800 }}>
            ဂိုဒေါင် {physical == null ? "ပြန်ရေတွက်" : <span style={{ color: viridian, fontSize: 36 }}>{physical}</span>}
            <br />
            စနစ် {system == null ? "စာရင်းစစ်" : <span style={{ color: alert, fontSize: 36 }}>{system}</span>}
          </div>
          <div style={{ marginTop: 20, color: alert, fontSize: 18, fontWeight: 800 }}>
            မကိုက်ညီမှုကို လူကဆုံးဖြတ်မည်
          </div>
        </div>
      </div>
    </div>
  );
};

const InventoryCorrection: React.FC<{
  visual: Extract<VisualType, { kind: "inventory_correction" }>;
  frame: number;
}> = ({ visual, frame }) => {
  const progress = interpolate(frame, [18, 72], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const displayed = Math.round(
    visual.from_value + (visual.to_value - visual.from_value) * progress,
  );
  const completed = visual.phase === "completed" && visual.completion_ui === true;

  return (
    <div style={{ ...panel, width: 760, padding: "44px 54px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div style={{ color: muted, fontSize: 20, fontWeight: 800 }}>
            Inventory System • Stock field
          </div>
          <div style={{ color: ink, fontSize: 34, fontWeight: 900, marginTop: 8 }}>
            စနစ်စာရင်း ပြင်ဆင်ခြင်း
          </div>
        </div>
        <div
          style={{
            padding: "10px 18px",
            borderRadius: 999,
            background: completed ? "rgba(22,133,107,0.12)" : "rgba(168,183,162,0.28)",
            color: completed ? viridian : ink,
            fontSize: 18,
            fontWeight: 800,
          }}
        >
          {completed ? "ပြင်ဆင်ပြီး" : "စစ်ဆေး/ပြင်ဆင်နေဆဲ"}
        </div>
      </div>

      <div
        style={{
          marginTop: 42,
          display: "grid",
          gridTemplateColumns: "1fr 100px 1fr",
          alignItems: "center",
          textAlign: "center",
        }}
      >
        <div>
          <div style={{ color: muted, fontSize: 18, fontWeight: 700 }}>မူလ စနစ်စာရင်း</div>
          <div style={{ color: alert, fontSize: 74, fontWeight: 900 }}>{visual.from_value}</div>
        </div>
        <div style={{ color: muted, fontSize: 34, fontWeight: 900 }}>→</div>
        <div>
          <div style={{ color: muted, fontSize: 18, fontWeight: 700 }}>အတည်ပြုထားသော ပမာဏ</div>
          <div style={{ color: viridian, fontSize: 74, fontWeight: 900 }}>{visual.to_value}</div>
        </div>
      </div>

      <div
        style={{
          marginTop: 34,
          padding: "24px 28px",
          borderRadius: 20,
          border: `2px solid ${completed ? viridian : sage}`,
          background: "rgba(244,240,230,0.72)",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <div>
          <div style={{ color: muted, fontSize: 17, fontWeight: 700 }}>လက်ရှိ input preview</div>
          <div style={{ color: ink, fontSize: 54, fontWeight: 900 }}>{displayed}</div>
        </div>
        {completed ? (
          <div style={{ display: "flex", alignItems: "center", gap: 14, color: viridian, fontWeight: 900 }}>
            <div
              style={{
                width: 30,
                height: 18,
                borderLeft: `5px solid ${viridian}`,
                borderBottom: `5px solid ${viridian}`,
                transform: "rotate(-45deg) translateY(-5px)",
              }}
            />
            ပြင်ဆင်ပြီး
          </div>
        ) : (
          <div style={{ width: 270 }}>
            <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
              <StatusDots frame={frame} />
            </div>
            <div style={{ height: 8, borderRadius: 9, background: sage, overflow: "hidden" }}>
              <div style={{ height: "100%", width: `${progress * 86}%`, background: viridian }} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

const AutoActionRenderer: React.FC<{
  visual: Extract<VisualType, { kind: "auto_action" }>;
  frame: number;
  fps: number;
}> = ({ visual, frame, fps }) => {
  const isReorder = visual.action === "reorder";

  if (isReorder) {
    const systemReveal = spring({ frame, fps, config: { damping: 18, stiffness: 150 } });
    const orderReveal = spring({ frame: frame - Math.round(fps * 0.75), fps, config: { damping: 18, stiffness: 150 } });
    const boxesReveal = spring({ frame: frame - Math.round(fps * 1.55), fps, config: { damping: 18, stiffness: 150 } });
    return (
      <div style={{ width: 900, position: "relative", height: 430 }}>
        <div style={{ position: "absolute", left: 100, right: 100, top: 187, height: 8, background: sage, borderRadius: 8, overflow: "hidden" }}>
          <div style={{ height: "100%", width: `${Math.min(100, Math.max(0, frame * 1.15))}%`, background: alert, borderRadius: 8 }} />
        </div>
        <div style={{ ...panel, position: "absolute", left: 0, top: 34, width: 310, padding: 34, borderRadius: 5, opacity: systemReveal, transform: `translateX(${interpolate(systemReveal, [0, 1], [-45, 0])}px) rotate(-1.5deg)` }}>
          <div>
            <div style={{ color: muted, fontSize: 18, fontWeight: 700 }}>စနစ်မှတ်တမ်း</div>
            <div style={{ color: ink, fontSize: 26, fontWeight: 800, marginTop: 12, lineHeight: 1.4 }}>
              ပစ္စည်းပြတ်လပ်မှု<br />စစ်ဆေးတွေ့ရှိသည်
            </div>
            <div style={{ height: 6, background: sage, marginTop: 24, borderRadius: 3 }}>
              <div style={{ width: `${Math.min(100, frame * 2)}%`, height: "100%", background: viridian, borderRadius: 3 }} />
            </div>
          </div>
        </div>
        <div style={{ position: "absolute", left: 328, top: 132, color: alert, fontSize: 52, fontWeight: 900, opacity: orderReveal, transform: `scaleX(${orderReveal})` }}>→</div>
        <div style={{ ...panel, position: "absolute", left: 410, top: 0, width: 260, padding: 28, borderRadius: 5, background: visual.severity === "mistake" ? "rgba(255,240,232,0.94)" : "rgba(244,240,230,0.94)", opacity: orderReveal, transform: `translateY(${interpolate(orderReveal, [0, 1], [50, 0])}px) scale(${interpolate(orderReveal, [0, 1], [0.9, 1])}) rotate(2deg)` }}>
            <div style={{ width: 100, height: 140, background: alert, borderRadius: 8, margin: "0 auto", display: "flex", flexDirection: "column", padding: 12, border: "2px solid #fff" }}>
              <div style={{ height: 8, width: 40, background: "#fff", opacity: 0.8, borderRadius: 4, marginBottom: 8 }} />
              <div style={{ height: 4, width: 60, background: "#fff", opacity: 0.5, borderRadius: 2, marginBottom: 6 }} />
              <div style={{ height: 4, width: 50, background: "#fff", opacity: 0.5, borderRadius: 2 }} />
            </div>
            <div style={{ color: alert, fontSize: 20, fontWeight: 800, textAlign: "center", marginTop: 20 }}>
              AI က အလိုအလျောက် ထပ်မှာ
            </div>
        </div>
        <div style={{ position: "absolute", left: 680, top: 132, color: alert, fontSize: 52, fontWeight: 900, opacity: boxesReveal, transform: `scaleX(${boxesReveal})` }}>→</div>
        <div style={{ ...panel, position: "absolute", right: 0, top: 52, width: 180, padding: 24, textAlign: "center", borderColor: "rgba(200,88,61,0.45)", borderRadius: 5, opacity: boxesReveal, transform: `translateX(${interpolate(boxesReveal, [0, 1], [52, 0])}px) scale(${interpolate(boxesReveal, [0, 1], [0.9, 1])}) rotate(-2deg)` }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 48px)", gap: 10, justifyContent: "center" }}>
            {[0, 1, 2, 3, 4, 5].map((index) => (
              <div key={index} style={{ width: 48, height: 40, borderRadius: 7, background: "#D7C9A7", border: "2px solid rgba(48,56,44,0.28)", transform: `translateY(${Math.sin((frame - index * 3) / 8) * 2}px)` }} />
            ))}
          </div>
          <div style={{ color: alert, fontSize: 18, lineHeight: 1.35, fontWeight: 900, marginTop: 18 }}>မလိုအပ်သော<br />ပစ္စည်းများ ရောက်လာ</div>
        </div>
      </div>
    );
  }

  const stopReveal = spring({ frame, fps, config: theme.spring.snappy });
  const askReveal = spring({ frame: frame - Math.round(fps * 0.85), fps, config: theme.spring.smooth });
  const questionReveal = spring({ frame: frame - Math.round(fps * 1.75), fps, config: theme.spring.smooth });
  return (
    <div style={{ position: "relative", width: 900, height: 480 }}>
      <div style={{ position: "absolute", top: 222, left: 92, right: 92, height: 8, borderRadius: 8, background: sage }} />
      <div style={{ position: "absolute", left: 20, top: 116, width: 190, height: 190, borderRadius: "50%", background: alert, border: "12px solid rgba(244,240,230,0.95)", color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center", fontSize: 29, lineHeight: 1.25, fontWeight: 900, boxShadow: "0 18px 34px rgba(200,88,61,0.25)", opacity: stopReveal, transform: `scale(${interpolate(stopReveal, [0, 1], [0.72, 1])})` }}>
        မသေချာလျှင်<br />ရပ်ပါ
      </div>
      <div style={{ ...panel, position: "absolute", left: 296, top: 92, width: 285, padding: 30, borderRadius: 5, textAlign: "center", opacity: askReveal, transform: `translateY(${interpolate(askReveal, [0, 1], [36, 0])}px) rotate(-1deg)` }}>
        <div style={{ color: viridian, fontSize: 21, fontWeight: 900 }}>လူကို အရင်မေးပါ</div>
        <div style={{ color: ink, fontSize: 26, lineHeight: 1.45, fontWeight: 850, marginTop: 16 }}>ဆုံးဖြတ်ချက်မချမီ<br />အတည်ပြုချက်ယူပါ</div>
      </div>
      <div style={{ position: "absolute", right: 4, top: 55, width: 252, padding: "32px 26px", background: "rgba(244,240,230,0.96)", border: `3px solid ${viridian}`, boxShadow: "0 18px 34px rgba(48,56,44,0.16)", color: ink, fontSize: 24, lineHeight: 1.45, fontWeight: 900, textAlign: "center", opacity: questionReveal, transform: `translateX(${interpolate(questionReveal, [0, 1], [44, 0])}px) rotate(2deg)` }}>
        ဘယ်အလုပ်ကို<br />လူက စစ်ရမလဲ?
      </div>
    </div>
  );
};

const ConsequenceRenderer: React.FC<{
  visual: Extract<VisualType, { kind: "consequence" }>;
  frame: number;
}> = ({ visual, frame }) => {
  if (visual.mode === "loss_chart") {
    const drawProgress = interpolate(frame, [0, 32], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    return (
      <div style={{ ...panel, width: 800, padding: 48 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 26 }}>
          <div style={{ color: alert, fontSize: 28, fontWeight: 900 }}>
            အမှားဆက်ဖြစ်လျှင် ဆုံးရှုံးမှုတိုးမည်
          </div>
          <div style={{ color: muted, fontSize: 17, fontWeight: 700 }}>အန္တရာယ် ခန့်မှန်းချက်</div>
        </div>
        <div style={{ position: "relative", height: 280 }}>
          <svg width="704" height="260" viewBox="0 0 704 260" role="img" aria-label="Loss rises as mistakes continue">
            <line x1="48" y1="18" x2="48" y2="220" stroke={muted} strokeWidth="4" />
            <line x1="48" y1="220" x2="680" y2="220" stroke={muted} strokeWidth="4" />
            <line x1="48" y1="154" x2="680" y2="154" stroke={sage} strokeWidth="2" strokeDasharray="8 10" opacity="0.7" />
            <line x1="48" y1="88" x2="680" y2="88" stroke={sage} strokeWidth="2" strokeDasharray="8 10" opacity="0.7" />
            <path
              d="M48 220 L175 194 L305 158 L440 105 L565 66 L680 30 L680 220 Z"
              fill="rgba(200,88,61,0.12)"
              opacity={drawProgress}
            />
            <path
              d="M70 205 L175 194 L305 158 L440 105 L565 66 L662 34"
              fill="none"
              stroke={alert}
              strokeWidth="9"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeDasharray="760"
              strokeDashoffset={760 * (1 - drawProgress)}
            />
            {[
              [175, 194],
              [305, 158],
              [440, 105],
              [565, 66],
              [662, 34],
            ].map(([x, y], index) => (
              <circle key={index} cx={x} cy={y} r="9" fill="#fff" stroke={alert} strokeWidth="6" opacity={drawProgress} />
            ))}
          </svg>
          <div style={{ position: "absolute", top: -2, left: 58, color: alert, fontSize: 17, fontWeight: 900 }}>
            ဆုံးရှုံးမှု မြင့်
          </div>
          <div style={{ position: "absolute", bottom: 0, right: 18, color: muted, fontSize: 17, fontWeight: 800 }}>
            အချိန် ကြာလာသည် →
          </div>
        </div>
      </div>
    );
  }

  return (
    <div style={{ width: 900, display: "grid", gridTemplateColumns: `repeat(${Math.min(visual.items.length, 3)}, 1fr)`, gap: 24 }}>
      {visual.items.slice(0, 3).map((item, idx) => (
        <div key={idx} style={{ ...panel, padding: 32, textAlign: "center", opacity: Math.min(1, Math.max(0, (frame - idx * 10) / 10)) }}>
          <div style={{ width: 64, height: 64, borderRadius: 32, background: "rgba(200,88,61,0.1)", color: alert, fontSize: 32, fontWeight: 900, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 20px" }}>
            {idx + 1}
          </div>
          <div style={{ color: ink, fontSize: 24, fontWeight: 800, lineHeight: 1.4 }}>
            {item}
          </div>
        </div>
      ))}
    </div>
  );
};

const ProcessTimelineRenderer: React.FC<{
  visual: Extract<VisualType, { kind: "process_timeline" }>;
  frame: number;
}> = ({ visual }) => {
  return (
    <div style={{ ...panel, width: 800, padding: 48 }}>
      <div style={{ color: ink, fontSize: 28, fontWeight: 800, marginBottom: 48, textAlign: "center" }}>
        {visual.step === "detect" ? "စစ်ဆေးခြင်း အဆင့်" : "စစ်ဆေးအတည်ပြုခြင်း အဆင့်"}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", position: "relative", padding: "0 20px" }}>
        <div style={{ position: "absolute", top: 24, left: 40, right: 40, height: 4, background: sage, zIndex: 0 }} />
        {Array.from({ length: visual.total_steps }).map((_, idx) => {
          const isActive = idx + 1 === visual.active_step;
          const isPast = idx + 1 < visual.active_step;
          const isCompleted = visual.phase === "completed" && idx + 1 === visual.total_steps;
          const showTick = visual.step === "audit" && (isPast || isCompleted);

          return (
            <div key={idx} style={{ position: "relative", zIndex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
              <div style={{
                width: 52, height: 52, borderRadius: 26,
                background: (isPast || isCompleted) ? viridian : isActive ? surface : sage,
                border: isActive ? `4px solid ${viridian}` : "none",
                display: "flex", alignItems: "center", justifyContent: "center",
                color: (isPast || isCompleted) ? "#fff" : isActive ? viridian : muted,
                fontSize: 20, fontWeight: 900
              }}>
                {showTick ? (
                  <div style={{ width: 14, height: 8, borderLeft: "3px solid #fff", borderBottom: "3px solid #fff", transform: "rotate(-45deg) translateY(-2px)" }} />
                ) : (
                  idx + 1
                )}
              </div>
              <div style={{ color: isActive ? ink : muted, fontSize: 18, fontWeight: 800 }}>
                အဆင့် {idx + 1}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

const HumanVerificationRenderer: React.FC<{
  visual: Extract<VisualType, { kind: "human_verification" }>;
  frame: number;
}> = ({ visual, frame }) => {
  if (visual.mode === "count") {
    return (
      <div style={{ ...panel, width: 700, padding: 48 }}>
        <div style={{ color: muted, fontSize: 20, fontWeight: 700, marginBottom: 32, textAlign: "center" }}>
          ဂိုဒေါင်အတွင်း စာရင်းရေတွက်ခြင်း
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {[1, 2, 3].map((row) => (
            <div key={row} style={{ display: "flex", gap: 12 }}>
              {Array.from({ length: 8 }).map((_, col) => {
                const itemIndex = row * 8 + col;
                const isCounted = frame > itemIndex * 2;
                return (
                  <div key={col} style={{
                    flex: 1, height: 40, borderRadius: 6,
                    background: isCounted ? viridian : sage,
                    opacity: isCounted ? 1 : 0.4
                  }} />
                );
              })}
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (visual.mode === "checklist") {
    return (
      <div style={{ ...panel, width: 760, padding: 48 }}>
        <div style={{ color: ink, fontSize: 28, fontWeight: 800, marginBottom: 32, textAlign: "center" }}>
          လူသား ဆုံးဖြတ်ချက်
        </div>
        <div style={{ display: "grid", gridTemplateColumns: visual.options?.length === 2 ? "1fr 1fr" : "1fr", gap: 24 }}>
          {visual.options?.slice(0, 2).map((opt, idx) => (
            <div key={idx} style={{
              ...panel, padding: 24, textAlign: "center", border: `2px solid ${viridian}`,
              background: idx === 0 ? "rgba(22,133,107,0.1)" : surface
            }}>
              <div style={{ color: idx === 0 ? viridian : ink, fontSize: 22, fontWeight: 800 }}>
                {opt}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const isCompleted = visual.phase === "completed";
  const progress = Math.min(100, frame * 2);

  return (
    <div style={{ ...panel, width: 600, padding: 48, textAlign: "center" }}>
      <div style={{ color: ink, fontSize: 28, fontWeight: 800, marginBottom: 40 }}>
        အတည်ပြုရန်
      </div>
      <div style={{
        width: 240, height: 240, borderRadius: 120, margin: "0 auto",
        background: isCompleted ? viridian : "rgba(22,133,107,0.1)",
        border: `8px solid ${isCompleted ? viridian : sage}`,
        display: "flex", alignItems: "center", justifyContent: "center",
        position: "relative", overflow: "hidden"
      }}>
        {!isCompleted && (
          <div style={{
            position: "absolute", bottom: 0, left: 0, right: 0,
            height: `${progress}%`, background: "rgba(22,133,107,0.2)"
          }} />
        )}
        <div style={{ color: isCompleted ? "#fff" : viridian, fontSize: 32, fontWeight: 900, zIndex: 1 }}>
          {isCompleted ? "အတည်ပြုပြီး" : "ဖိထားပါ"}
        </div>
      </div>
    </div>
  );
};

const ApprovalRecordRenderer: React.FC<{
  visual: Extract<VisualType, { kind: "approval_record" }>;
  frame: number;
}> = ({ visual, frame }) => {
  return (
    <div style={{ ...panel, width: 700, padding: 48, position: "relative" }}>
      <div style={{ color: ink, fontSize: 28, fontWeight: 800, marginBottom: 32, borderBottom: `2px solid ${sage}`, paddingBottom: 16 }}>
        မှတ်တမ်း
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr", gap: 24, fontSize: 22 }}>
        <div style={{ color: muted, fontWeight: 700 }}>စစ်ဆေးသူ</div>
        <div style={{ color: ink, fontWeight: 800 }}>{visual.reviewer}</div>

        <div style={{ color: muted, fontWeight: 700 }}>အထောက်အထား</div>
        <div style={{ color: ink, fontWeight: 800 }}>{visual.evidence}</div>

        <div style={{ color: muted, fontWeight: 700 }}>ဆုံးဖြတ်ချက်</div>
        <div style={{ color: viridian, fontWeight: 900 }}>{visual.decision}</div>
      </div>

      {visual.phase === "completed" && (
        <div style={{
          position: "absolute", right: 48, top: 48,
          border: `4px solid ${viridian}`, color: viridian,
          padding: "8px 16px", borderRadius: 8, transform: "rotate(-12deg)",
          fontSize: 24, fontWeight: 900, opacity: Math.min(1, frame / 10)
        }}>
          ပြီးစီး
        </div>
      )}
    </div>
  );
};

const BalancePairRenderer: React.FC<{
  visual: Extract<VisualType, { kind: "balance_pair" }>;
  frame: number;
  fps: number;
}> = ({ visual, frame, fps }) => {
  const aiReveal = spring({ frame, fps, config: theme.spring.smooth });
  const worldReveal = spring({ frame: frame - Math.round(fps * 0.75), fps, config: theme.spring.smooth });
  const bridgeReveal = spring({ frame: frame - Math.round(fps * 1.45), fps, config: theme.spring.snappy });

  return (
    <div style={{ position: "relative", width: 900, height: 500 }}>
      <div style={{ position: "absolute", left: 44, top: 82, width: 300, padding: "30px 28px", background: "rgba(244,240,230,0.95)", borderLeft: `9px solid ${viridian}`, boxShadow: "0 18px 34px rgba(48,56,44,0.14)", opacity: aiReveal, transform: `translateX(${interpolate(aiReveal, [0, 1], [-42, 0])}px) rotate(-2deg)` }}>
        <div style={{ color: viridian, fontSize: 36, fontWeight: 950 }}>AI</div>
        <div style={{ color: ink, fontSize: 25, lineHeight: 1.45, fontWeight: 850, marginTop: 12 }}>{visual.left_label}</div>
        <div style={{ color: alert, fontSize: 66, fontWeight: 950, marginTop: 18 }}>2</div>
      </div>
      <div style={{ position: "absolute", right: 30, top: 160, width: 310, padding: "30px 28px", background: "rgba(244,240,230,0.95)", borderLeft: `9px solid ${alert}`, boxShadow: "0 18px 34px rgba(48,56,44,0.14)", opacity: worldReveal, transform: `translateX(${interpolate(worldReveal, [0, 1], [42, 0])}px) rotate(2deg)` }}>
        <div style={{ color: alert, fontSize: 24, fontWeight: 900 }}>လက်တွေ့ကမ္ဘာ</div>
        <div style={{ color: ink, fontSize: 25, lineHeight: 1.45, fontWeight: 850, marginTop: 12 }}>{visual.right_label}</div>
        <div style={{ color: viridian, fontSize: 66, fontWeight: 950, marginTop: 18 }}>12</div>
      </div>
      <div style={{ position: "absolute", left: 350, top: 226, width: 200, padding: "14px 12px", background: ink, color: "#fff", fontSize: 20, lineHeight: 1.35, fontWeight: 900, textAlign: "center", opacity: bridgeReveal, transform: `scale(${interpolate(bridgeReveal, [0, 1], [0.82, 1])}) rotate(-1deg)` }}>
        AI က data ကိုပဲ<br />မြင်နိုင်ပါတယ်
      </div>
    </div>
  );
};

const OutroRenderer: React.FC<{
  visual: Extract<VisualType, { kind: "outro" }>;
  frame: number;
}> = ({ visual }) => {
  return (
    <div style={{ textAlign: "center", marginTop: 40 }}>
      <div style={{ color: viridian, fontSize: 120, fontWeight: 900, letterSpacing: -2, lineHeight: 1 }}>
        FYF
      </div>
      <div style={{ color: ink, fontSize: 36, fontWeight: 800, marginTop: 24 }}>
        {visual.tagline}
      </div>
      <div style={{ ...panel, display: "inline-block", padding: "16px 32px", marginTop: 64 }}>
        <div style={{ color: muted, fontSize: 20, fontWeight: 700 }}>
          ပူးပေါင်းဆောင်ရွက်မှု မှတ်တမ်း
        </div>
      </div>
    </div>
  );
};

export const TypedVisual: React.FC<{
  visual: VisualType;
  startFrame: number;
}> = ({ visual, startFrame }) => {
  const globalFrame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const frame = globalFrame - startFrame;
  const entrance = spring({ frame, fps, config: { damping: 18, stiffness: 120 } });
  const cameraTarget =
    visual.camera === "close_up"
      ? 1.045
      : visual.camera === "push_in"
        ? 1.03
        : visual.camera === "over_shoulder"
          ? 1.02
          : 1;
  const cameraScale = interpolate(frame, [0, 120], [1, cameraTarget], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const cameraX = visual.camera === "over_shoulder" ? -18 : 0;
  const storyDriven =
    visual.kind === "inventory_mismatch" ||
    visual.kind === "balance_pair" ||
    visual.kind === "approval_gate" ||
    (visual.kind === "auto_action" &&
      (visual.action === "reorder" || visual.action === "pause_notify"));

  return (
    <AbsoluteFill>
      <PaperWorld frame={frame} visual={visual} />
      {!storyDriven ? <div
        style={{
          position: "absolute",
          top: 275,
          left: 90,
          right: 90,
          zIndex: 3,
          textAlign: "center",
          color: ink,
          fontFamily: theme.fonts.display,
        }}
      >
        {visual.screen_text.slice(0, 2).map((line, index) => (
          <div
            key={line}
            style={{
              fontSize: index === 0 ? 38 : 28,
              lineHeight: 1.42,
              fontWeight: index === 0 ? 900 : 650,
              color: index === 0 ? ink : muted,
            }}
          >
            {line}
          </div>
        ))}
      </div> : null}
      <div
        style={{
          position: "absolute",
          top: 440,
          left: 60,
          right: 60,
          height: 780,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          opacity: entrance,
          transform: `translate(${cameraX}px, ${interpolate(entrance, [0, 1], [38, 0])}px) scale(${cameraScale})`,
          transformOrigin: "center center",
          fontFamily: theme.fonts.body,
        }}
      >
        {visual.kind === "inventory_mismatch" ? (
          <InventoryMismatch visual={visual} frame={frame} fps={fps} />
        ) : visual.kind === "approval_gate" ? (
          <ApprovalGate visual={visual} frame={frame} fps={fps} />
        ) : visual.kind === "inventory_correction" ? (
          <InventoryCorrection visual={visual} frame={frame} />
        ) : visual.kind === "auto_action" ? (
          <AutoActionRenderer visual={visual} frame={frame} fps={fps} />
        ) : visual.kind === "consequence" ? (
          <ConsequenceRenderer visual={visual} frame={frame} />
        ) : visual.kind === "process_timeline" ? (
          <ProcessTimelineRenderer visual={visual} frame={frame} />
        ) : visual.kind === "human_verification" ? (
          <HumanVerificationRenderer visual={visual} frame={frame} />
        ) : visual.kind === "approval_record" ? (
          <ApprovalRecordRenderer visual={visual} frame={frame} />
        ) : visual.kind === "balance_pair" ? (
          <BalancePairRenderer visual={visual} frame={frame} fps={fps} />
        ) : visual.kind === "outro" ? (
          <OutroRenderer visual={visual} frame={frame} />
        ) : (
          <div style={{ ...panel, padding: 48, color: ink, fontSize: 30, fontWeight: 800 }}>
            {visual.screen_text.join(" • ")}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
