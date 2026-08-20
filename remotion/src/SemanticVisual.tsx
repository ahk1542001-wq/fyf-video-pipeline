import React from "react";
import { useCurrentFrame, useVideoConfig, spring, interpolate } from "remotion";
import { theme } from "./theme";

interface SemanticVisualProps {
  visualAction?: string;
  sceneType?: string;
  startFrame: number;
}

export const SemanticVisual: React.FC<SemanticVisualProps> = ({
  visualAction = "",
  sceneType = "",
  startFrame,
}) => {
  const globalFrame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const frame = globalFrame - startFrame;
  const action = visualAction.toLowerCase();

  // Common visual container style
  const containerStyle: React.CSSProperties = {
    position: "absolute",
    top: 250,
    left: "5%",
    width: "90%",
    height: 500, // Reduced from 600 so it ends near/above y=750 (250+500)
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    transform: `scale(1.0) translateY(${interpolate(
      spring({ frame, fps, config: theme.spring.smooth }),
      [0, 1],
      [40, 0]
    )}px)`,
    opacity: spring({ frame, fps, config: theme.spring.smooth }),
  };

  const getVisualContent = () => {
    // Three consequence cards
    if (action.includes("three consequence cards") || action.includes("consequence cards")) {
      const cards = [
        { color: "#FF5F56", delay: 0 },
        { color: theme.colors.accent, delay: 15 },
        { color: theme.colors.primary, delay: 30 }
      ];
      return (
        <div style={{ display: "flex", gap: 30, alignItems: "center" }}>
          {cards.map((card, i) => {
            const cardFrame = frame - card.delay;
            const progress = spring({ frame: cardFrame, fps, config: theme.spring.bouncy });
            return (
              <div key={i} style={{
                width: 150,
                height: 150,
                background: theme.colors.bg,
                borderRadius: 16,
                border: `3px solid ${card.color}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                transform: `scale(${progress}) translateY(${interpolate(progress, [0, 1], [50, 0])}px)`,
                opacity: interpolate(cardFrame, [0, 10], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
              }}>
                <div style={{
                  width: 80, height: 80, borderRadius: "50%", background: `${card.color}22`,
                  display: "flex", alignItems: "center", justifyContent: "center"
                }}>
                  <div style={{ width: 40, height: 40, background: card.color, borderRadius: 8 }} />
                </div>
              </div>
            );
          })}
        </div>
      );
    }

    // Paused workflow notification
    if (action.includes("paused workflow sends notification") || action.includes("paused workflow")) {
      const progress = interpolate(frame, [0, 30], [0, 1], { extrapolateRight: "clamp" });
      const notificationPop = spring({ frame: frame - 30, fps, config: theme.spring.bouncy });
      return (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 40 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
            <div style={{ width: 60, height: 60, borderRadius: "50%", background: theme.colors.primary }} />
            <div style={{ width: 200, height: 10, background: theme.colors.accent, borderRadius: 5, overflow: "hidden" }}>
              <div style={{ width: `${progress * 100}%`, height: "100%", background: theme.colors.primary }} />
            </div>
            <div style={{
              width: 60, height: 60, borderRadius: 8,
              background: frame > 30 ? "#FF5F56" : theme.colors.accent,
              transform: `scale(${frame > 30 ? 1 + Math.sin((frame - 30) / 3) * 0.1 : 1})`
            }} />
          </div>
          <div style={{
            background: theme.colors.bg,
            border: `3px solid #FF5F56`,
            width: 100, height: 100,
            display: "flex", alignItems: "center", justifyContent: "center",
            borderRadius: "50%",
            transform: `scale(${notificationPop})`,
            opacity: notificationPop,
          }}>
            <div style={{ fontSize: 48, fontWeight: "bold", color: "#FF5F56" }}>!</div>
          </div>
        </div>
      );
    }

    // Audit trail timeline
    if (action.includes("audit trail timeline") || action.includes("audit trail")) {
      const nodes = [
        { delay: 0, color: "#FF5F56" },
        { delay: 20, color: theme.colors.accent },
        { delay: 40, color: theme.colors.primary }
      ];
      return (
        <div style={{ display: "flex", alignItems: "center", position: "relative", width: 600 }}>
          <div style={{ position: "absolute", left: 50, right: 50, height: 8, background: theme.colors.accent, zIndex: 0 }} />
          <div style={{
            position: "absolute", left: 50, height: 8, background: theme.colors.primary, zIndex: 0,
            width: `${interpolate(frame, [0, 60], [0, 500], { extrapolateRight: "clamp" })}px`
          }} />
          <div style={{ display: "flex", justifyContent: "space-between", width: "100%", zIndex: 1 }}>
            {nodes.map((node, i) => {
              const nodeFrame = frame - node.delay;
              const pop = spring({ frame: nodeFrame, fps, config: theme.spring.bouncy });
              return (
                <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 15 }}>
                  <div style={{
                    width: 60, height: 60, borderRadius: "50%",
                    background: frame >= node.delay ? node.color : theme.colors.bg,
                    border: `4px solid ${frame >= node.delay ? node.color : theme.colors.accent}`,
                  transform: `scale(${pop})`,
                    display: "flex", alignItems: "center", justifyContent: "center"
                  }}>
                    {frame >= node.delay + 10 && <div style={{ width: 20, height: 20, borderRadius: "50%", background: "#fff" }} />}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      );
    }

    // Approval record card
    if (action.includes("approval record card") || action.includes("approval record")) {
      const cardSpring = spring({ frame, fps, config: theme.spring.smooth });
      const rows = [
        { label: "1", value: "✓", delay: 10 },
        { label: "2", value: "✓", delay: 25 },
        { label: "3", value: "✓", delay: 40, highlight: true }
      ];
      return (
        <div style={{
          width: 300,
          background: theme.colors.bg,
          borderRadius: 20,
          border: `2px solid ${theme.colors.accent}`,
          padding: 40,
          transform: `scale(${cardSpring}) translateY(${interpolate(cardSpring, [0, 1], [40, 0])}px)`,
        }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            {rows.map((row, i) => {
              const rowSpring = spring({ frame: frame - row.delay, fps, config: theme.spring.smooth });
              return (
                <div key={i} style={{
                  display: "flex", justifyContent: "space-between", alignItems: "center",
                  opacity: rowSpring,
                  transform: `translateX(${interpolate(rowSpring, [0, 1], [-20, 0])}px)`
                }}>
                  <div style={{ width: 30, height: 30, borderRadius: "50%", background: theme.colors.accent, display: "flex", alignItems: "center", justifyContent: "center", color: theme.colors.text }}>{row.label}</div>
                  <div style={{
                    fontSize: 24, fontWeight: "bold",
                    color: row.highlight ? theme.colors.primary : theme.colors.text,
                    background: row.highlight ? `${theme.colors.primary}22` : "transparent",
                    padding: row.highlight ? "5px 15px" : 0,
                    borderRadius: 8
                  }}>{row.value}</div>
                </div>
              );
            })}
          </div>
        </div>
      );
    }

    // 12 physical boxes vs system 2
    if (action.includes("12 physical boxes") || action.includes("system 2")) {
      return (
        <div style={{ display: "flex", gap: 60, alignItems: "center" }}>
          <div style={{ textAlign: "center" }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
              {[...Array(12)].map((_, i) => (
                <div key={i} style={{
                  width: 40, height: 40, background: theme.colors.accent, borderRadius: 8,
                  transform: `scale(${spring({ frame: frame - i * 3, fps, config: theme.spring.bouncy })})`
                }} />
              ))}
            </div>
            <div style={{ fontSize: 32, fontWeight: "bold", color: theme.colors.text, marginTop: 20 }}>12</div>
          </div>
          <div style={{ fontSize: 60, color: theme.colors.primary }}>≠</div>
          <div style={{ textAlign: "center" }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 10 }}>
              {[...Array(2)].map((_, i) => (
                <div key={i} style={{
                  width: 40, height: 40, background: theme.colors.primary, borderRadius: 8,
                  transform: `scale(${spring({ frame: frame - 20 - i * 5, fps, config: theme.spring.bouncy })})`
                }} />
              ))}
            </div>
            <div style={{ fontSize: 32, fontWeight: "bold", color: theme.colors.text, marginTop: 20 }}>2</div>
          </div>
        </div>
      );
    }

    // automated purchase order
    if (action.includes("automated purchase order") || action.includes("generating po")) {
      const orderProgress = interpolate(frame, [0, 60], [0, 100], { extrapolateRight: "clamp" });
      return (
        <div style={{
          width: 300, height: 100, background: theme.colors.bg, borderRadius: 16, padding: 30,
          border: `2px solid ${theme.colors.primary}`, display: "flex", flexDirection: "column", justifyContent: "center"
        }}>
          <div style={{ width: "100%", height: 20, background: theme.colors.accent, borderRadius: 10, overflow: "hidden" }}>
            <div style={{ width: `${orderProgress}%`, height: "100%", background: theme.colors.primary }} />
          </div>
          <div style={{ marginTop: 20, fontSize: 32, color: theme.colors.text, textAlign: "center", fontWeight: "bold" }}>
            {orderProgress === 100 ? "✓" : "..."}
          </div>
        </div>
      );
    }

    // financial loss/over-ordering
    if (action.includes("financial loss") || action.includes("over-ordering")) {
      const chartPoints = "0,150 100,120 200,160 300,100 400,200 500,250";
      const pathLength = interpolate(frame, [0, 45], [0, 600], { extrapolateRight: "clamp" });
      return (
        <div style={{ position: "relative", width: 500, height: 300, display: "flex", flexDirection: "column", alignItems: "center" }}>
          <svg width="500" height="250" viewBox="0 0 500 250">
            <polyline
              points={chartPoints}
              fill="none"
              stroke="#FF5F56"
              strokeWidth="10"
              strokeLinejoin="round"
              strokeDasharray="600"
              strokeDashoffset={600 - pathLength}
            />
            <circle cx="500" cy="250" r="15" fill="#FF5F56" opacity={frame > 45 ? 1 : 0} />
          </svg>
        </div>
      );
    }

    // human approval gate
    if (action.includes("human approval gate") || action.includes("approval gate")) {
      const gateRotation = interpolate(spring({ frame, fps, config: theme.spring.smooth }), [0, 1], [0, -90]);
      return (
        <div style={{ display: "flex", alignItems: "center", gap: 40 }}>
          <div style={{
            width: 80, height: 80, borderRadius: "50%", background: theme.colors.primary,
            transform: `translateX(${interpolate(frame, [0, 45, 90], [0, 80, 200], { extrapolateRight: "clamp" })}px)`
          }} />
          <div style={{
        width: 20, height: 150, background: frame > 30 ? theme.colors.primary : "#FF5F56",
        transform: `rotate(${gateRotation}deg)`, transformOrigin: "bottom center"
          }} />
        </div>
      );
    }

    // discrepancy alert
    if (action.includes("discrepancy alert") || action.includes("discrepancy")) {
      const isVisible = Math.floor(frame / 10) % 2 === 0;
      return (
        <div style={{
          padding: "30px 50px", background: isVisible ? "#FF5F56" : theme.colors.bg,
          color: isVisible ? "#fff" : theme.colors.text, borderRadius: 20,
          border: `4px solid #FF5F56`, fontSize: 48, fontWeight: "bold",
          transform: `scale(${interpolate(spring({ frame, fps, config: theme.spring.bouncy }), [0, 1], [0.8, 1])})`
        }}>
          12 ≠ 2
        </div>
      );
    }

    // warehouse count
    if (action.includes("warehouse count") || action.includes("count")) {
       return (
         <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 20 }}>
            {[...Array(15)].map((_, i) => (
              <div key={i} style={{
                width: 50, height: 50, background: theme.colors.accent,
                display: "flex", alignItems: "center", justifyContent: "center",
                opacity: frame > i * 5 ? 1 : 0.2,
                transform: `scale(${interpolate(spring({ frame: frame - i * 5, fps, config: theme.spring.bouncy }), [0, 1], [1, 1.1])})`
              }}>
                {frame > i * 5 && <span style={{ color: theme.colors.primary, fontWeight: "bold" }}>✓</span>}
              </div>
            ))}
         </div>
       );
    }

    // missing-data-vs-bug checklist
    if (action.includes("checklist") || action.includes("missing-data") || action.includes("bug")) {
      return (
        <div style={{
          background: theme.colors.bg, padding: 40, borderRadius: 20,
          border: `2px solid ${theme.colors.accent}`, width: 200
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 20, marginBottom: 20, opacity: spring({ frame: frame - 15, fps, config: theme.spring.smooth }) }}>
            <div style={{ width: 30, height: 30, borderRadius: 6, border: `2px solid ${theme.colors.primary}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <div style={{ width: 20, height: 20, background: theme.colors.primary, borderRadius: 3 }} />
            </div>
            <div style={{ flex: 1, height: 16, background: theme.colors.textDim, borderRadius: 8 }} />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 20, opacity: spring({ frame: frame - 45, fps, config: theme.spring.smooth }) }}>
            <div style={{ width: 30, height: 30, borderRadius: 6, border: `2px solid #FF5F56` }} />
            <div style={{ flex: 1, height: 16, background: theme.colors.textDim, borderRadius: 8 }} />
          </div>
        </div>
      );
    }

    // correcting stock 2 to 12
    if (action.includes("correcting stock") || action.includes("2 to 12") || action.includes("preparing to input correct stock number")) {
      const currentNumber = Math.floor(interpolate(frame, [15, 60], [2, 12], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }));
      return (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 20 }}>
          <div style={{
            fontSize: 120, fontWeight: "bold", color: currentNumber === 12 ? theme.colors.primary : "#FF5F56",
            fontVariantNumeric: "tabular-nums"
          }}>
            {currentNumber}
          </div>
          {currentNumber === 12 && (
            <div style={{ fontSize: 48, color: theme.colors.primary, opacity: spring({ frame: frame - 60, fps, config: theme.spring.bouncy }) }}>
              ✓
            </div>
          )}
        </div>
      );
    }

    // human approve action
    if (action.includes("human approve") || action.includes("approve action") || action.includes("human clicking approve button for ai to proceed")) {
      return (
        <div style={{
          width: 200, height: 200, borderRadius: "50%", background: theme.colors.primary,
          display: "flex", alignItems: "center", justifyContent: "center",
          transform: `scale(${spring({ frame, fps, config: theme.spring.bouncy })})`,
          boxShadow: `0 0 40px ${theme.colors.primary}66`
        }}>
          <svg width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="#F4F0E6" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" strokeDasharray="30" strokeDashoffset={interpolate(frame, [15, 45], [30, 0], { extrapolateRight: "clamp" })} />
          </svg>
        </div>
      );
    }

    // AI speed vs human accuracy balance
    if (action.includes("balance") || action.includes("ai speed")) {
       const rotation = interpolate(frame, [0, 60], [-20, 0], { extrapolateRight: "clamp" });
       return (
         <div style={{ position: "relative", width: 600, height: 300, display: "flex", justifyContent: "center" }}>
           {/* Pivot */}
           <div style={{ position: "absolute", bottom: 0, width: 0, height: 0, borderLeft: "40px solid transparent", borderRight: "40px solid transparent", borderBottom: `60px solid ${theme.colors.accent}` }} />
           {/* Beam */}
           <div style={{
             position: "absolute", bottom: 60, width: 500, height: 16, background: theme.colors.text, borderRadius: 8,
             transform: `rotate(${rotation}deg)`, transformOrigin: "center",
             display: "flex", justifyContent: "space-between", alignItems: "flex-end"
           }}>
             {/* Left Weight (AI Speed) */}
             <div style={{ width: 100, height: 80, background: theme.colors.primary, borderRadius: 12, transform: "translateY(-16px)", display: "flex", alignItems: "center", justifyContent: "center" }} />
             {/* Right Weight (Human Accuracy) */}
             <div style={{ width: 110, height: 110, background: theme.colors.accent, borderRadius: "50%", transform: "translateY(-16px)", display: "flex", alignItems: "center", justifyContent: "center" }} />
           </div>
         </div>
       );
    }

    // FYF wrap-up
    if (action.includes("fyf wrap-up") || action.includes("wrap-up") || action.includes("fyf logo with final wrap up text")) {
      return (
        <div style={{ textAlign: "center", opacity: spring({ frame, fps, config: theme.spring.smooth }) }}>
          <div style={{
            width: 150, height: 150, borderRadius: "50%",
            border: `10px solid ${theme.colors.primary}`,
            display: "flex", alignItems: "center", justifyContent: "center"
          }}>
            <div style={{ fontSize: 60, color: theme.colors.primary, fontWeight: "bold" }}>✓</div>
          </div>
        </div>
      );
    }

    // Polished Generic Fallback
    return (
      <div style={{
        width: 300, height: 300, borderRadius: "50%",
        border: `4px dashed ${theme.colors.accent}`,
        display: "flex", alignItems: "center", justifyContent: "center",
        transform: `rotate(${frame * 0.5}deg)`
      }}>
        <div style={{
          width: 200, height: 200, borderRadius: "50%",
          background: theme.colors.primary, opacity: 0.1,
          transform: `scale(${1 + Math.sin(frame / 15) * 0.2})`
        }} />
      </div>
    );
  };

  return <div style={containerStyle}>{getVisualContent()}</div>;
};
