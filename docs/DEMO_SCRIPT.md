# Demo Video Script (target: 2:30-2:50)

Language: English (spoken or subtitles). Screen recording at 1920x1080, browser zoomed to ~125% for legibility.

## Beat 1 — The problem (0:00-0:25)

**Screen:** plain text slide or the Create Studio with an empty form.

> "Financial scams in Myanmar often start with a single misread transfer slip.
> FYF turns a Burmese topic into a fully reviewed vertical explainer video —
> with every fact, image, and caption traceable before anything is published."

**Action:** type the topic `မိဘများအတွက် မှန်ကန်သော OCR ပြေစာစစ်ဆေးမှုဖြင့် ငွေလွှဲအမှားများကို ကာကွယ်ခြင်း` into Create Studio and press Generate.

## Beat 2 — Agentic script production (0:25-1:00)

**Screen:** progress card cycling through `adk_orchestration → storyboard → lock`.

> "A Google ADK producer agent researches the topic, drafts segments with
> evidence-backed claims, audits its own quality, then freezes an immutable
> story lock. Every claim carries its evidence class — nothing is invented
> silently."

**Action:** show the locked script view briefly (segments + claims).

## Beat 3 — Video generation (1:00-1:40)

**Screen:** video job card: `visuals → voice → rendering`, then Library with the finished MP4 playing.

> "Visuals are planned and verified per scene with Vertex AI, narrated with
> Gemini TTS, and rendered as a 1080x1920 Remotion composition with Burmese
> captions — segmented renders keep checkpoints so a constrained container can
> always resume instead of failing.
>
> Deterministic QA, creative QA, and a final rendered-meaning check must all
> pass before the video reaches the Library."

**Action:** play 5-8 seconds of the final MP4 inside the Library page.

## Beat 4 — Self-auditing warehouse via ClickHouse MCP (1:40-2:30)

**Screen:** split view — terminal curl to `/api/insights` (or the Telemetry page), then the ClickHouse SQL console showing `video_pipeline_jobs` rows.

> "Here is where ClickHouse comes in. Every run dual-writes sanitized job,
> scene, QA, and model-call telemetry into ClickHouse Cloud.
>
> Our Data Officer — an ADK agent wired to the official mcp-clickhouse MCP
> server — answers questions about that warehouse in natural language:
> 'How many jobs passed QA this week?' ... answered live, from real rows.
>
> The factory doesn't just produce videos — it can audit itself."

**Action:** run two insight questions live; show one matching SQL query result in the ClickHouse console.

## Beat 5 — Stack recap (2:30-2:50)

**Screen:** closing slide.

> "FYF: Gemini on Vertex AI, Google ADK, Remotion, Cloud Run, and ClickHouse —
> production-ready agentic video for audiences that deserve accurate
> information. Source code is open under MIT."

---

### Recording checklist
- [ ] Fresh topic run recorded end-to-end (or pre-recorded segments spliced)
- [ ] `/api/insights` answered live on camera (tool_used true)
- [ ] ClickHouse console shows the same numbers as the answer
- [ ] English subtitles track exported
- [ ] Upload YouTube/Vimeo public, add link to Devpost form
