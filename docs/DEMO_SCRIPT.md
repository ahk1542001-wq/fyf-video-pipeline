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

**Action:** open the Telemetry page and use the "Ask the Data Officer" box (no terminal needed).

Verified questions that answer live from ClickHouse Cloud (2026-08-25):

| Ask this | Expected shape of the answer |
| --- | --- |
| "How many video jobs are recorded, how many succeeded, and what did they cost in total?" | counts + total cost from `video_pipeline_jobs` |
| "How many model calls did the latest job use?" | call count from `video_vertex_calls` |
| "What is the title of the most recent completed job?" | Burmese title from the latest row |

Answers carry a green badge: **✓ answered from live ClickHouse query**. If a
question times out (>26s budget), the panel shows a clean retryable message —
just ask again; do not re-record the whole take.

## Beat 5 — Stack recap (2:30-2:50)

**Screen:** closing slide.

> "FYF: Gemini on Vertex AI, Google ADK, Remotion, Cloud Run, and ClickHouse —
> production-ready agentic video for audiences that deserve accurate
> information. Source code is open under MIT."

---

### Recording checklist
- [x] Production URL ready: https://fyf-pipeline-605161166139.asia-southeast1.run.app
- [x] Approved library already holds a cloud-rendered job (`b2ec7c5d`, OTP-safety explainer) usable for Beat 3
- [ ] Fresh topic run recorded end-to-end (or pre-recorded segments spliced; existing MP4s: e49aa2d5 32.8s, 838803f2 34.7s, b2ec7c5d)
- [ ] Data Officer answered live on camera via the Telemetry page panel
- [ ] ClickHouse console shows the same numbers as the answer
- [ ] English subtitles track exported
- [ ] Upload YouTube/Vimeo public, add link to Devpost form
