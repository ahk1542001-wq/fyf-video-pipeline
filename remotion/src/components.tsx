import React from "react";
import { useCurrentFrame, useVideoConfig, spring, interpolate } from "remotion";
import { theme } from "./theme";

// Premium Entrance
export const Entrance: React.FC<{ delay?: number; children: React.ReactNode }> = ({ delay = 0, children }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const p = spring({ frame: frame - delay, fps, config: theme.spring.smooth });
  return (
    <div style={{
      opacity: p,
      transform: `translateY(${interpolate(p, [0, 1], [40, 0])}px) scale(${interpolate(p, [0, 1], [0.94, 1])})`,
    }}>
      {children}
    </div>
  );
};

export const Stagger: React.FC<{ items: React.ReactNode[]; start?: number; per?: number }> = ({ items, start = 0, per = 4 }) => (
  <>{items.map((item, i) => <Entrance key={i} delay={start + i * per}>{item}</Entrance>)}</>
);

// Ultra-premium Karaoke Text
export const WordReveal: React.FC<{
  text: string; delay?: number; per?: number; size?: number; highlight?: boolean; style?: React.CSSProperties; startFrame?: number; emphasis?: string[];
}> = ({ text, delay = 0, per = 3, size = 60, highlight = false, style, startFrame = 0, emphasis }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const isKeyword = (w: string) => w.startsWith("**") && w.endsWith("**");

  // Match every word contained in the contract's emphasis phrases.
  const emphasisWords = (emphasis ?? []).flatMap((phrase) =>
    phrase.toLowerCase().replace(/[.,!?။၊]/g, "").split(" ").filter(Boolean),
  );

  return (
    <div style={{
      width: "100%",
      display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "center", gap: "10px 16px", maxWidth: 960, ...style
    }}>
      {text.split(" ").map((rawWord, i) => {
        const cleanRawWord = rawWord.replace(/\*\*/g, "").toLowerCase().replace(/[.,!?။၊]/g, "");
        const inEmphasis = emphasisWords.includes(cleanRawWord);
        const highlightWord = highlight || isKeyword(rawWord) || inEmphasis;
        const word = rawWord.replace(/\*\*/g, "");
        const localFrame = frame - startFrame;
        const p = spring({ frame: localFrame - delay - i * per, fps, config: theme.spring.snappy });

        // Vercel-style pop: scale up slightly, color shift to Viridian
        const scale = highlightWord ? interpolate(p, [0, 1], [0.95, 1.03]) : interpolate(p, [0, 1], [0.95, 1]);
        const color = highlightWord ? theme.colors.primary : theme.colors.text;

        return (
          <span key={i} style={{
            display: "inline-block", opacity: p,
            transform: `translateY(${interpolate(p, [0, 1], [20, 0])}px) scale(${scale})`,
            fontFamily: theme.fonts.display, fontWeight: highlightWord ? 700 : 500, fontSize: size,
            lineHeight: 1.4, letterSpacing: "0", wordSpacing: "-0.05em", color,
            textShadow: highlightWord
              ? "0 3px 12px rgba(22, 133, 107, 0.35)"
              : "0 3px 18px rgba(244, 240, 230, 0.95)",
          }}>
            {word}
          </span>
        );
      })}
    </div>
  );
};

// Vercel-style Aurora Background
export const BgMesh: React.FC<{ dark?: boolean }> = ({ dark = false }) => {
  const { width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const speed = 150;

  // Smooth flowing coordinates
  const x1 = Math.sin(frame / speed) * 300;
  const y1 = Math.cos(frame / speed) * 200;
  const x2 = Math.cos((frame / speed) * 1.2) * 250;
  const y2 = Math.sin((frame / speed) * 1.2) * 300;

  return (
    <div style={{ position: "absolute", top: 0, left: 0, width, height, background: "#111111", overflow: "hidden" }}>
      {/* Deep dark base to make glowing colors pop */}

      {/* Aurora Orbs */}
      <div style={{ position: "absolute", top: -200, left: -200, width: "150%", height: "150%", filter: "blur(140px)" }}>
        <div style={{ position: "absolute", top: "10%", right: "20%", width: 900, height: 900, background: theme.colors.primary, opacity: 0.25, borderRadius: "50%", transform: `translate(${x1}px, ${y1}px)` }} />
        <div style={{ position: "absolute", bottom: "10%", left: "10%", width: 1100, height: 1100, background: theme.colors.accent, opacity: 0.15, borderRadius: "50%", transform: `translate(${x2}px, ${y2}px)` }} />
      </div>

      {/* Glass Frosting Layer */}
      <div style={{ position: "absolute", top: 0, left: 0, width, height, background: "rgba(244, 240, 230, 0.92)", backdropFilter: "blur(50px)" }} />
    </div>
  );
};

// 3D Perspective Glowing Agent Network
export const AgentNetwork: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const nodes = [
    { id: 0, x: 200, y: 150, delay: 5 }, { id: 1, x: 800, y: 100, delay: 15 },
    { id: 2, x: 850, y: 650, delay: 25 }, { id: 3, x: 150, y: 600, delay: 35 },
    { id: 4, x: 500, y: 400, delay: 20 }
  ];
  const conns = [[0, 4], [1, 4], [2, 4], [3, 4], [0, 1], [3, 2]];

  return (
    <div style={{
      position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
      perspective: 1200, display: "flex", alignItems: "center", justifyContent: "center", opacity: 0.5
    }}>
      {/* Tilt the entire network to make it look 3D (Stripe style) */}
      <div style={{ width: 1080, height: 800, transform: "rotateX(55deg) rotateZ(-15deg) scale(1.3)", position: "relative", transformStyle: "preserve-3d" }}>
        <svg width="100%" height="100%" viewBox="0 0 1080 800" style={{ overflow: "visible" }}>

          {/* Connecting Beams */}
          {conns.map(([a, b], i) => {
            const drawP = spring({ frame: frame - Math.max(nodes[a].delay, nodes[b].delay), fps, config: theme.spring.smooth });
            const pulse = ((frame - nodes[a].delay) * 5) % 100;
            return (
              <g key={`c${i}`}>
                {/* Base glass tube */}
                <line x1={nodes[a].x} y1={nodes[a].y} x2={interpolate(drawP, [0,1], [nodes[a].x, nodes[b].x])} y2={interpolate(drawP, [0,1], [nodes[a].y, nodes[b].y])} stroke="rgba(22, 133, 107, 0.15)" strokeWidth={4} strokeLinecap="round" />
                {/* Laser pulse */}
                {drawP > 0.9 && (
                  <line x1={nodes[a].x} y1={nodes[a].y} x2={nodes[b].x} y2={nodes[b].y} stroke={theme.colors.primary} strokeWidth={4} strokeDasharray="15 150" strokeDashoffset={-pulse} style={{ filter: "drop-shadow(0 0 12px rgba(22,133,107,0.8))" }} strokeLinecap="round" />
                )}
              </g>
            );
          })}

          {/* Nodes (Floating Glass Orbs) */}
          {nodes.map(n => {
            const p = spring({ frame: frame - n.delay, fps, config: theme.spring.bouncy });
            const scale = interpolate(p, [0, 1], [0, 1]);
            const floatY = Math.sin((frame - n.delay) / 20) * 15;
            return (
              <g key={`n${n.id}`} transform={`translate(${n.x}, ${n.y + floatY}) scale(${scale})`}>
                <circle r={35} fill="rgba(255,255,255,0.8)" style={{ filter: "drop-shadow(0 20px 30px rgba(0,0,0,0.15))" }} />
                <circle r={18} fill={theme.colors.primary} />
                <circle r={10} fill="#ffffff" style={{ filter: "blur(2px)" }} />
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
};

export const TerminalDemo: React.FC<{ codeText?: string }> = ({
  codeText = "import { Agent } from '@fyf/core';\n\nconst brain = new Agent({\n  model: 'claude-3-opus',\n  tools: ['search']\n});\n\nawait brain.execute('Build video pipeline');\n// >> Pipeline connected. All systems go."
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const entrance = spring({ frame, fps, config: theme.spring.smooth });
  const translateY = interpolate(entrance, [0, 1], [400, 0]);

  const chars = Math.max(0, Math.floor((frame - 15) / 1.5));
  const currentCode = codeText.substring(0, chars);

  return (
    <div style={{
      position: "absolute", top: 340, left: "5%", width: "90%", height: 540,
      translate: `0px ${translateY}px`, opacity: entrance * 0.72,
      background: "rgba(255, 255, 255, 0.55)", backdropFilter: "blur(24px)", WebkitBackdropFilter: "blur(24px)",
      border: "1px solid rgba(255, 255, 255, 0.7)", borderRadius: 32,
      boxShadow: "0 28px 64px rgba(48, 56, 44, 0.08)",
      display: "flex", flexDirection: "column", overflow: "hidden"
    }}>
      <div style={{ height: 60, background: "rgba(0,0,0,0.03)", display: "flex", alignItems: "center", padding: "0 30px", borderBottom: "1px solid rgba(0,0,0,0.05)", gap: 10 }}>
        <div style={{ width: 14, height: 14, borderRadius: "50%", background: "#FF5F56" }} />
        <div style={{ width: 14, height: 14, borderRadius: "50%", background: "#FFBD2E" }} />
        <div style={{ width: 14, height: 14, borderRadius: "50%", background: "#27C93F" }} />
        <div style={{ flex: 1, textAlign: "center", color: "rgba(0,0,0,0.4)", fontSize: 14, fontWeight: 600, fontFamily: theme.fonts.body }}>agent-core.ts</div>
      </div>
      <div style={{ padding: "40px", flex: 1 }}>
        <pre style={{ margin: 0, color: theme.colors.text, fontFamily: theme.fonts.mono, fontSize: 24, lineHeight: 1.7, whiteSpace: "pre-wrap", fontWeight: 500 }}>
          {currentCode}<span style={{ opacity: Math.floor(frame / 15) % 2 === 0 ? 1 : 0, color: theme.colors.primary }}>_</span>
        </pre>
      </div>
    </div>
  );
};

export const Breathe: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return <div style={{ transform: `scale(${1 + Math.sin((frame / fps) * Math.PI) * 0.01})` }}>{children}</div>;
};

export const Grain: React.FC = () => {
  const frame = useCurrentFrame();
  return <div style={{
    position: "absolute", top: 0, left: 0, width: "100%", height: "100%", opacity: 0.04, pointerEvents: "none",
    backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")`,
    transform: `translate(${(frame % 5) * 2}px, ${(frame % 7) * 2}px)`
  }} />;
};

export const Vignette: React.FC = () => (
  <div style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", background: `radial-gradient(ellipse at center, transparent 60%, rgba(0,0,0,0.12) 100%)`, pointerEvents: "none" }} />
);
