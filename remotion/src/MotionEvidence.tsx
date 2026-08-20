import React from "react";
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from "remotion";
import {theme} from "./theme";
import {VisualType} from "./types";
import {relationshipPresentation, visibleMotionValues} from "./relationshipLayout";

type Shot = NonNullable<VisualType["evidence_shots"]>[number];

const tokenStyle: React.CSSProperties = {
  minWidth: 132,
  minHeight: 118,
  borderRadius: 24,
  border: `3px solid ${theme.colors.primary}`,
  background: "rgba(247,244,235,0.96)",
  boxShadow: "0 18px 40px rgba(43,55,47,0.14)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: "20px 24px",
  color: theme.colors.text,
  fontFamily: theme.fonts.display,
  fontSize: 42,
  fontWeight: 750,
  textAlign: "center",
};

export const MotionEvidence: React.FC<{shot: Shot; localFrame: number}> = ({shot, localFrame}) => {
  const {fps} = useVideoConfig();
  const spec = shot.motion_spec!;
  const labels = spec.labels;
  const visibleValues = visibleMotionValues(labels, spec.values);
  const repeated = spec.layout === "count" && spec.object_count
    ? Array.from({length: spec.object_count}, (_, index) => String(index + 1))
    : labels;
  const items = spec.layout === "comparison"
    ? labels.slice(0, 2)
    : spec.layout === "sequence"
      ? labels
      : repeated;

  if (spec.layout === "comparison") {
    return (
      <AbsoluteFill style={{background: "#eee9dd", padding: "250px 54px 420px"}}>
        <div style={{display: "flex", flex: 1, gap: 26, alignItems: "stretch", justifyContent: "center"}}>
          {items.map((label, index) => {
            const enter = spring({frame: localFrame - index * Math.round(fps * 0.28), fps, config: {damping: 17, stiffness: 145}});
            const rawValue = visibleValues[index] ?? "";
            const count = /^\d+$/.test(rawValue) ? Math.min(30, Number(rawValue)) : 0;
            return (
              <div key={`${label}-${index}`} style={{flex: 1, borderRadius: 30, border: `4px solid ${index === spec.accent_index ? "#c95f45" : theme.colors.primary}`, background: index === spec.accent_index ? "#f6d8ce" : "rgba(247,244,235,0.97)", boxShadow: "0 20px 44px rgba(43,55,47,0.14)", padding: "34px 22px", display: "flex", flexDirection: "column", alignItems: "center", opacity: enter, transform: `translateY(${interpolate(enter, [0,1], [60,0])}px)`}}>
                <div style={{fontFamily: theme.fonts.display, color: theme.colors.text, fontSize: 36, fontWeight: 750, textAlign: "center", minHeight: 104}}>{label}</div>
                <div style={{fontFamily: theme.fonts.display, color: index === spec.accent_index ? "#a44732" : theme.colors.primary, fontSize: 104, lineHeight: 1, fontWeight: 900, margin: "22px 0 30px"}}>{rawValue}</div>
                {count > 0 && <div style={{display: "grid", gridTemplateColumns: `repeat(${count > 9 ? 4 : 3}, 46px)`, gap: 12, justifyContent: "center"}}>
                  {Array.from({length: count}, (_, objectIndex) => <div key={objectIndex} style={{width: 42, height: 34, borderRadius: 7, background: index === spec.accent_index ? "#c95f45" : theme.colors.primary, opacity: spring({frame: localFrame - index * Math.round(fps * 0.28) - objectIndex * 2, fps, config: {damping: 18, stiffness: 150}})}} />)}
                </div>}
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    );
  }

  if (spec.layout === "concept") {
    return (
      <AbsoluteFill style={{background: "#eee9dd", padding: "260px 76px 430px"}}>
        <div style={{display: "flex", flex: 1, alignItems: "center", justifyContent: "center", gap: 24}}>
          {items.map((label, index) => {
            const enter = spring({frame: localFrame - index * Math.round(fps * 0.3), fps, config: {damping: 17, stiffness: 145}});
            const isLast = index === items.length - 1;
            const symbol = String(index + 1);
            return (
              <React.Fragment key={`${label}-${index}`}>
                <div style={{width: 250, minHeight: 330, borderRadius: 34, border: `4px solid ${index === spec.accent_index ? "#c95f45" : theme.colors.primary}`, background: index === spec.accent_index ? "#f6d8ce" : "rgba(247,244,235,0.98)", boxShadow: "0 20px 44px rgba(43,55,47,0.14)", padding: "32px 24px", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 30, opacity: enter, transform: `translateY(${interpolate(enter, [0, 1], [55, 0])}px)`}}>
                  <div style={{width: 118, height: 118, borderRadius: 28, display: "flex", alignItems: "center", justifyContent: "center", background: index === spec.accent_index ? "#c95f45" : theme.colors.primary, color: "#f7f4eb", fontFamily: theme.fonts.display, fontSize: 62, fontWeight: 900}}>{symbol}</div>
                  <div style={{fontFamily: theme.fonts.display, color: theme.colors.text, fontSize: 35, fontWeight: 750, lineHeight: 1.45, textAlign: "center"}}>{label}</div>
                </div>
                {!isLast && <div style={{fontSize: 58, color: theme.colors.primary, opacity: enter}}>→</div>}
              </React.Fragment>
            );
          })}
        </div>
      </AbsoluteFill>
    );
  }

  if (spec.layout === "relationship") {
    const presentation = relationshipPresentation(spec);
    const connectorEnter = spring({frame: localFrame - Math.round(fps * 0.24), fps, config: {damping: 17, stiffness: 145}});
    const compact = presentation.nodes.length >= 3;
    return (
      <AbsoluteFill style={{background: "#eee9dd", padding: "245px 56px 400px"}}>
        <div style={{display: "flex", flex: 1, flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 34}}>
          <div style={{display: "flex", width: "100%", alignItems: "center", justifyContent: "center", gap: compact ? 12 : 28}}>
            {presentation.nodes.map((label, index) => {
              const enter = spring({frame: localFrame - index * Math.round(fps * 0.28), fps, config: {damping: 17, stiffness: 145}});
              return <React.Fragment key={`${label}-${index}`}>
                <div style={{flex: 1, minWidth: 0, minHeight: compact ? 220 : 240, borderRadius: 34, border: `4px solid ${theme.colors.primary}`, background: "rgba(247,244,235,0.98)", boxShadow: "0 20px 44px rgba(43,55,47,0.14)", display: "flex", alignItems: "center", justifyContent: "center", padding: compact ? "26px 16px" : 32, opacity: enter, transform: `translateY(${interpolate(enter, [0, 1], [50, 0])}px)`, fontFamily: theme.fonts.display, color: theme.colors.text, fontSize: compact ? 31 : 43, lineHeight: 1.42, fontWeight: 820, textAlign: "center", overflowWrap: "normal", wordBreak: "normal", lineBreak: "loose"}}>{label}</div>
                {index < presentation.nodes.length - 1 && <div style={{flex: "0 0 auto", fontSize: compact ? 48 : 82, lineHeight: 1, color: "#c95f45", fontWeight: 900, opacity: connectorEnter}}>{presentation.connector}</div>}
              </React.Fragment>;
            })}
          </div>
          <div style={{display: "flex", flexWrap: "wrap", justifyContent: "center", gap: 16, width: "100%"}}>
            {presentation.relations.map((value, index) => {
              const enter = spring({frame: localFrame - Math.round(fps * 0.42) - index * 4, fps, config: {damping: 18, stiffness: 145}});
              const isRelation = index === presentation.relations.length - 1;
              return <div key={`${value}-${index}`} style={{borderRadius: 999, padding: "15px 24px", background: isRelation ? "#c95f45" : "rgba(29,127,112,0.12)", border: `2px solid ${isRelation ? "#c95f45" : theme.colors.primary}`, color: isRelation ? "#fffaf2" : theme.colors.text, fontFamily: theme.fonts.display, fontSize: 29, fontWeight: 760, opacity: enter, transform: `translateY(${interpolate(enter, [0, 1], [26, 0])}px)`}}>{value}</div>;
            })}
          </div>
        </div>
      </AbsoluteFill>
    );
  }

  if (spec.layout === "directional_branch") {
    const [source, ...outcomes] = labels;
    const sourceEnter = spring({frame: localFrame, fps, config: {damping: 17, stiffness: 145}});
    return (
      <AbsoluteFill style={{background: "#eee9dd", padding: "235px 64px 390px"}}>
        <div style={{display: "flex", flex: 1, flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 24}}>
          <div style={{width: "58%", minHeight: 190, borderRadius: 34, border: `4px solid ${theme.colors.primary}`, background: "rgba(247,244,235,0.98)", boxShadow: "0 20px 44px rgba(43,55,47,0.14)", display: "flex", alignItems: "center", justifyContent: "center", padding: 30, opacity: sourceEnter, transform: `translateY(${interpolate(sourceEnter, [0, 1], [45, 0])}px)`, fontFamily: theme.fonts.display, color: theme.colors.text, fontSize: 40, lineHeight: 1.42, fontWeight: 820, textAlign: "center"}}>{source}</div>
          <div style={{fontSize: 58, color: "#c95f45", fontWeight: 900, lineHeight: 1}}>↓</div>
          <div style={{display: "flex", width: "100%", gap: 22, alignItems: "stretch", justifyContent: "center"}}>
            {outcomes.map((label, index) => {
              const enter = spring({frame: localFrame - Math.round(fps * 0.25) - index * Math.round(fps * 0.18), fps, config: {damping: 17, stiffness: 145}});
              return <div key={`${label}-${index}`} style={{flex: 1, minWidth: 0, minHeight: 230, borderRadius: 34, border: `4px solid ${index === spec.accent_index ? "#c95f45" : theme.colors.primary}`, background: index === spec.accent_index ? "#f6d8ce" : "rgba(247,244,235,0.98)", boxShadow: "0 20px 44px rgba(43,55,47,0.14)", display: "flex", alignItems: "center", justifyContent: "center", padding: 28, opacity: enter, transform: `translateY(${interpolate(enter, [0, 1], [50, 0])}px)`, fontFamily: theme.fonts.display, color: theme.colors.text, fontSize: 35, lineHeight: 1.42, fontWeight: 800, textAlign: "center", overflowWrap: "normal", wordBreak: "normal", lineBreak: "loose"}}>{label}</div>;
            })}
          </div>
          {visibleValues.some(Boolean) && <div style={{display: "flex", flexWrap: "wrap", justifyContent: "center", gap: 14}}>{visibleValues.map((value, index) => value ? <div key={`${value}-${index}`} style={{borderRadius: 999, padding: "13px 22px", background: "rgba(29,127,112,0.12)", border: `2px solid ${theme.colors.primary}`, color: theme.colors.text, fontFamily: theme.fonts.display, fontSize: 27, fontWeight: 740}}>{value}</div> : null)}</div>}
        </div>
      </AbsoluteFill>
    );
  }

  if (spec.layout === "sequence") {
    const compact = items.length >= 3;
    const dense = items.length >= 4;
    return (
      <AbsoluteFill style={{background: "#eee9dd", padding: "250px 52px 420px"}}>
        <div style={{display: "flex", flex: 1, alignItems: "center", justifyContent: "center", gap: compact ? 14 : 24}}>
          {items.map((label, index) => {
            const enter = spring({frame: localFrame - index * Math.round(fps * 0.24), fps, config: {damping: 17, stiffness: 145}});
            const value = visibleValues[index];
            return (
              <React.Fragment key={`${label}-${index}`}>
                <div style={{
                  flex: "1 1 0",
                  minWidth: 0,
                  minHeight: compact ? 300 : 330,
                  borderRadius: 30,
                  border: `4px solid ${index === spec.accent_index ? "#c95f45" : theme.colors.primary}`,
                  background: index === spec.accent_index ? "#f6d8ce" : "rgba(247,244,235,0.98)",
                  boxShadow: "0 20px 44px rgba(43,55,47,0.14)",
                  padding: dense ? "26px 10px" : compact ? "30px 18px" : "34px 28px",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 22,
                  opacity: enter,
                  transform: `translateY(${interpolate(enter, [0, 1], [55, 0])}px)`,
                  overflow: "hidden",
                }}>
                  <div style={{
                    width: compact ? 78 : 92,
                    height: compact ? 78 : 92,
                    flex: "0 0 auto",
                    borderRadius: 24,
                    background: index === spec.accent_index ? "#c95f45" : theme.colors.primary,
                    color: "#f7f4eb",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontFamily: theme.fonts.display,
                    fontSize: compact ? 43 : 50,
                    fontWeight: 900,
                  }}>{index + 1}</div>
                  <div style={{
                    width: "100%",
                    fontFamily: theme.fonts.display,
                    color: theme.colors.text,
                    fontSize: dense ? 24 : compact ? 31 : 37,
                    fontWeight: 780,
                    lineHeight: 1.42,
                    textAlign: "center",
                    overflowWrap: "normal",
                    wordBreak: "normal",
                    lineBreak: "loose",
                  }}>{label}</div>
                  {value && <div style={{
                    width: "100%",
                    borderTop: "2px solid rgba(29,127,112,0.24)",
                    paddingTop: 18,
                    fontFamily: theme.fonts.display,
                    color: index === spec.accent_index ? "#a44732" : theme.colors.primary,
                    fontSize: compact ? 25 : 29,
                    fontWeight: 720,
                    lineHeight: 1.4,
                    textAlign: "center",
                    overflowWrap: "normal",
                    wordBreak: "normal",
                    lineBreak: "loose",
                  }}>{value}</div>}
                </div>
                {index < items.length - 1 && (
                  <div style={{flex: "0 0 auto", fontSize: compact ? 42 : 54, color: theme.colors.primary, opacity: enter}}>→</div>
                )}
              </React.Fragment>
            );
          })}
        </div>
      </AbsoluteFill>
    );
  }

  return (
    <AbsoluteFill style={{background: "#eee9dd", padding: "250px 70px 420px"}}>
      <div style={{display: "flex", flex: 1, alignItems: "center", justifyContent: "center", gap: 28, flexWrap: "wrap"}}>
        {items.map((label, index) => {
          const enter = spring({frame: localFrame - index * Math.round(fps * 0.22), fps, config: {damping: 17, stiffness: 145}});
          const y = interpolate(enter, [0, 1], [55, 0]);
          const isCountObject = spec.layout === "count" && Boolean(spec.object_count);
          return (
            <React.Fragment key={`${label}-${index}`}>
              <div style={{
                ...tokenStyle,
                minWidth: isCountObject ? 72 : tokenStyle.minWidth,
                minHeight: isCountObject ? 72 : tokenStyle.minHeight,
                padding: isCountObject ? 10 : tokenStyle.padding,
                borderColor: index === spec.accent_index ? "#c95f45" : theme.colors.primary,
                background: index === spec.accent_index ? "#f6d8ce" : tokenStyle.background,
                opacity: enter,
                transform: `translateY(${y}px) scale(${0.82 + enter * 0.18})`,
              }}>
                {isCountObject ? "◆" : label}
              </div>
            </React.Fragment>
          );
        })}
      </div>
      {visibleValues.some(Boolean) && (
        <div style={{display: "flex", justifyContent: "center", gap: 42, marginBottom: 52}}>
          {visibleValues.map((value, index) => value ? (
            <div key={`${value}-${index}`} style={{fontFamily: theme.fonts.display, fontWeight: 800, fontSize: 72, color: index === spec.accent_index ? "#c95f45" : theme.colors.primary}}>{value}</div>
          ) : null)}
        </div>
      )}
    </AbsoluteFill>
  );
};
