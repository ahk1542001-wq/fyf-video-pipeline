#!/usr/bin/env python3
"""
Autonomous High-Definition Screen Recording Walkthrough for FYF Agentic Business Studio.
Designed for Google Agentic Cinema: The Blockbuster Hackathon submission.
Captures full 1080p browser walkthrough with:
- Glowing cursor overlay
- Human-like smooth mouse movement
- Act 1: Create Studio, Presets, Brand Kit (Aspect ratios, CTA, Retention)
- Act 2: Library & High-Quality Approved Video Playback with full Remotion animations
- Act 3: ClickHouse Cloud Telemetry & Query Console
- Converts final video to optimized 1080p MP4 ready for YouTube upload
"""

import os
import sys
import time
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "output"
REC_DIR = OUTPUT_DIR / "demo_recordings"
REC_DIR.mkdir(parents=True, exist_ok=True)
FINAL_MP4 = OUTPUT_DIR / "fyf_agentic_cinema_demo.mp4"

def inject_cursor_overlay(page):
    page.evaluate("""() => {
        if (document.getElementById('__demo_cursor')) return;
        const cursor = document.createElement('div');
        cursor.id = '__demo_cursor';
        cursor.style.position = 'fixed';
        cursor.style.width = '24px';
        cursor.style.height = '24px';
        cursor.style.borderRadius = '50%';
        cursor.style.backgroundColor = 'rgba(99, 102, 241, 0.5)';
        cursor.style.border = '2px solid #818cf8';
        cursor.style.boxShadow = '0 0 14px rgba(129, 140, 248, 0.7)';
        cursor.style.pointerEvents = 'none';
        cursor.style.zIndex = '999999';
        cursor.style.transition = 'transform 0.12s ease, background-color 0.15s ease';
        cursor.style.transform = 'translate(-50%, -50%)';
        cursor.style.display = 'block';
        cursor.style.left = '400px';
        cursor.style.top = '300px';
        document.body.appendChild(cursor);

        window.addEventListener('mousemove', (e) => {
            cursor.style.left = e.clientX + 'px';
            cursor.style.top = e.clientY + 'px';
        });
        window.addEventListener('mousedown', () => {
            cursor.style.transform = 'translate(-50%, -50%) scale(0.7)';
            cursor.style.backgroundColor = 'rgba(239, 68, 68, 0.7)';
            cursor.style.borderColor = '#f87171';
        });
        window.addEventListener('mouseup', () => {
            cursor.style.transform = 'translate(-50%, -50%) scale(1)';
            cursor.style.backgroundColor = 'rgba(99, 102, 241, 0.5)';
            cursor.style.borderColor = '#818cf8';
        });
    }""")

def smooth_move_and_click(page, locator, steps=20, wait_after=1.0):
    try:
        box = locator.bounding_box()
        if box:
            target_x = box["x"] + box["width"] / 2
            target_y = box["y"] + box["height"] / 2
            page.mouse.move(target_x, target_y, steps=steps)
            time.sleep(0.3)
            locator.click()
            time.sleep(wait_after)
    except Exception as e:
        print(f"Warning clicking element: {e}")

def main():
    from playwright.sync_api import sync_playwright

    print("🎬 [1/5] Initializing Playwright 1080p Screen Recorder...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--font-render-hinting=medium",
                "--enable-font-antialiasing",
                "--autoplay-policy=no-user-gesture-required",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            record_video_dir=str(REC_DIR),
            record_video_size={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        t_start = time.time()
        act_timings = {}

        # ==========================================
        # ACT 1: Create Studio & Brand Kit Controls
        # ==========================================
        print("  ▶ [Act 1] Navigating to Create Studio at http://localhost:3001/...")
        page.goto("http://localhost:3001/", wait_until="networkidle")
        inject_cursor_overlay(page)
        act_timings["act1_studio"] = 1.0
        time.sleep(2)

        # Smooth hover over presets
        presets = page.locator(".studio-preset-pill")
        preset_count = presets.count()
        print(f"    Found {preset_count} preset pills.")
        if preset_count > 1:
            print("    Switching preset to High-Converting Social Ad...")
            smooth_move_and_click(page, presets.nth(1), wait_after=2.0)
            if preset_count > 2:
                print("    Switching preset to Product Launch...")
                smooth_move_and_click(page, presets.nth(2), wait_after=2.0)
            print("    Switching back to Flagship Brand Explainer (Burmese default)...")
            smooth_move_and_click(page, presets.nth(0), wait_after=2.0)

        # Explore Aspect Ratio Controls (16:9, 1:1, 9:16)
        print("    Demonstrating Aspect Ratio switcher (16:9, 1:1, 9:16)...")
        ratios = page.locator("button[role='radio']")
        if ratios.count() >= 3:
            smooth_move_and_click(page, ratios.nth(1), wait_after=2.0) # 16:9
            smooth_move_and_click(page, ratios.nth(2), wait_after=2.0) # 1:1
            smooth_move_and_click(page, ratios.nth(0), wait_after=2.0) # 9:16

        # Scroll smoothly to show Brief & Story Lock
        page.evaluate("window.scrollBy({ top: 300, behavior: 'smooth' })")
        time.sleep(2.0)
        page.evaluate("window.scrollTo({ top: 0, behavior: 'smooth' })")
        time.sleep(1.0)

        # ==========================================
        # ACT 2: Chat + Canvas Studio Workspace (/project/88663779)
        # ==========================================
        print("  ▶ [Act 2] Navigating to Chat + Canvas Studio (/project/88663779)...")
        page.goto("http://localhost:3001/project/88663779", wait_until="networkidle")
        inject_cursor_overlay(page)
        try:
            page.wait_for_selector(".scene-card, [data-testid^='scene-']", timeout=12000)
        except Exception as e:
            print(f"    Wait for scene cards: {e}")
        act_timings["act2_canvas"] = round(time.time() - t_start, 2)
        time.sleep(1.5)

        # Select Scene 1 on the storyboard canvas
        scene_card = page.locator(".scene-card, [data-testid='scene-select-s1']").first
        if scene_card.is_visible():
            print("    Selecting Scene 1 on Storyboard Canvas...")
            smooth_move_and_click(page, scene_card, wait_after=2.0)

        # Focus note to the director in the Chat Panel
        chat_input = page.locator("textarea[placeholder*='rewrite the narration'], .chat-panel textarea").first
        if chat_input.is_visible():
            print("    Demonstrating AI Creative Director Chat Panel...")
            chat_input.fill("Emphasize the business risk of uncontrolled AI decisions")
            time.sleep(3.0)

        # Smooth hover over granular controls
        apply_btn = page.locator("button:has-text('Apply narration to canvas')").first
        if apply_btn.is_visible():
            apply_btn.hover()
            time.sleep(2.0)

        # ==========================================
        # ACT 3: Approved Video Library & Full Playback
        # ==========================================
        print("  ▶ [Act 3] Navigating to Approved Video Library (/library)...")
        page.goto("http://localhost:3001/library", wait_until="networkidle")
        inject_cursor_overlay(page)
        try:
            page.wait_for_selector("li.library-item", timeout=12000)
        except Exception as e:
            print(f"    Wait for library items: {e}")
        time.sleep(1.5)

        # Select the flagship approved video
        print("    Selecting Flagship Video card in Library...")
        video_items = page.locator("li.library-item")
        if video_items.count() > 0:
            top_video = video_items.first
            smooth_move_and_click(page, top_video, wait_after=2.0)

        # Inspect player
        player_video = page.locator("video.library-player__video")
        act_timings["act3_playback"] = round(time.time() - t_start, 2)
        if player_video.is_visible():
            print("    Playing approved video in player (showing Remotion visuals + voice)...")
            page.evaluate("""() => {
                const v = document.querySelector('video.library-player__video');
                if (v) {
                    v.muted = false;
                    v.play();
                }
            }""")
            # Let the video play for 14 seconds to capture animated visual evidence
            time.sleep(14)
        else:
            print("    Player video element not visible; continuing...")
            time.sleep(4)

        # ==========================================
        # ACT 4: 20-Gate QA Verification Modal
        # ==========================================
        try:
            meta_btn = page.locator(".library-player-actions button:has-text('Metadata'), button:has-text('Metadata')").first
            if meta_btn.count() > 0 and meta_btn.is_visible():
                print("    Opening QA Verification Metadata modal...")
                smooth_move_and_click(page, meta_btn, wait_after=1.5)
                act_timings["act4_qa"] = round(time.time() - t_start, 2)
                try:
                    page.wait_for_selector(".modal-check-pill", timeout=10000)
                except Exception:
                    pass
                
                # Inspect QA checks visibly
                check_pills = page.locator(".modal-check-pill")
                pill_count = check_pills.count()
                if pill_count > 0:
                    for idx in range(min(pill_count, 4)):
                        try:
                            check_pills.nth(idx).hover()
                            time.sleep(0.8)
                        except Exception:
                            pass
                else:
                    time.sleep(4.0)
                
                time.sleep(1.5)
                # Close modal
                close_btn = page.locator("button:has-text('Close'), button[aria-label='Close']").first
                if close_btn.count() > 0 and close_btn.is_visible():
                    smooth_move_and_click(page, close_btn, wait_after=1.5)
        except Exception as e:
            print(f"    Metadata modal interaction note: {e}")

        # ==========================================
        # ACT 5: ClickHouse Telemetry, Queries & Data Officer
        # ==========================================
        print("  ▶ [Act 5] Navigating to ClickHouse Telemetry (/telemetry)...")
        telemetry_nav = page.locator("nav.studio-nav a[href='/telemetry']")
        if telemetry_nav.is_visible():
            smooth_move_and_click(page, telemetry_nav, wait_after=2.0)
        else:
            page.goto("http://localhost:3001/telemetry", wait_until="networkidle")
            time.sleep(2)
        inject_cursor_overlay(page)
        try:
            page.wait_for_selector(".production-list", timeout=12000)
        except Exception as e:
            print(f"    Wait for production list: {e}")
        act_timings["act5_telemetry"] = round(time.time() - t_start, 2)
        time.sleep(2.0)

        # 1. Inspect Productions list and scroll to show completed productions
        print("    Inspecting Productions ledger and scrolling to show completed jobs...")
        page.evaluate("""() => {
            const list = document.querySelector('.production-list');
            if (list) list.scrollBy({ top: 180, behavior: 'smooth' });
        }""")
        time.sleep(2.0)

        # 2. Select the flagship completed production (b188ca9f / Burmese title)
        target_prod = page.locator(".production-list button:has-text('b188ca9f'), .production-list button:has-text('လုပ်ငန်းသုံး')").first
        if target_prod.count() > 0:
            print("    Selecting flagship production (b188ca9f)...")
            smooth_move_and_click(page, target_prod, wait_after=2.5)

        # 3. Scroll down smoothly to show Performance timeline and Provider calls
        print("    Displaying performance timeline and Vertex provider calls...")
        page.evaluate("window.scrollBy({ top: 400, behavior: 'smooth' })")
        time.sleep(2.5)

        # 4. Expand Advanced Telemetry disclosure (ClickHouse Query Console & Data Officer)
        print("    Expanding Advanced Telemetry section...")
        disclosure = page.locator("summary:has-text('Advanced telemetry'), details summary").first
        if disclosure.count() > 0:
            smooth_move_and_click(page, disclosure, wait_after=2.0)

        # 5. Run ClickHouse Query Preset - Cost Intelligence (Fast & concise 4 rows)
        query_btn = page.locator("button.query-preset:has-text('Cost Intelligence'), button:has-text('Cost Intelligence')").first
        if query_btn.count() > 0:
            print("    Executing ClickHouse Query preset: Cost Intelligence...")
            smooth_move_and_click(page, query_btn, wait_after=2.0)
            try:
                page.wait_for_selector(".query-result table", timeout=8000)
            except Exception:
                pass
            time.sleep(1.5)

        # 6. Scroll down to Ask the Data Officer and ensure it is centered in the viewport
        print("    Displaying Google ADK Data Officer via ClickHouse MCP...")
        act_timings["act5_officer"] = round(time.time() - t_start, 2)
        page.evaluate("""() => {
            const officerSection = document.querySelector('section.advanced-panel[aria-labelledby="data-officer-title"]');
            if (officerSection) {
                officerSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else {
                window.scrollBy({ top: 380, behavior: 'smooth' });
            }
        }""")
        time.sleep(2.0)

        officer_input = page.locator("#officer-question, input[placeholder*='question']").first
        ask_button = page.locator("form.officer-form button[type='submit'], button:has-text('Ask')").first
        if officer_input.count() > 0 and ask_button.count() > 0:
            print("    Submitting natural language query to Data Officer...")
            smooth_move_and_click(page, officer_input, wait_after=0.5)
            officer_input.fill("How many video jobs are recorded in total, and what are their statuses?")
            time.sleep(0.8)
            smooth_move_and_click(page, ask_button, wait_after=1.0)
            try:
                page.wait_for_selector(".officer-answer", timeout=20000)
                print("    Data Officer answered successfully!")
                page.evaluate("""() => {
                    const ans = document.querySelector('.officer-answer');
                    if (ans) ans.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }""")
            except Exception as e:
                print(f"    Waiting for officer answer: {e}")
            time.sleep(7.0)

        print("  ✓ Concluding Demo Walkthrough...")
        page.wait_for_timeout(1500)

        # Save actual recorded act timings to JSON for perfect audio sync
        timings_file = OUTPUT_DIR / "demo_act_timings.json"
        with open(timings_file, "w") as f:
            json.dump(act_timings, f, indent=2)
        print(f"  Saved recorded act timings: {act_timings}")

        # Close page to flush video stream
        video_path = page.video.path() if page.video else None
        page.close()
        context.close()
        browser.close()

    if not video_path or not Path(video_path).is_file():
        # Fallback: search REC_DIR for latest webm
        webms = sorted(REC_DIR.glob("*.webm"), key=os.path.getmtime, reverse=True)
        if webms:
            video_path = str(webms[0])
        else:
            print("❌ Error: No recorded video file found!", file=sys.stderr)
            return 1

    print(f"🎬 [4/5] Transcoding recorded video ({video_path}) to 1080p MP4...")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(FINAL_MP4)
    ]
    subprocess.run(ffmpeg_cmd, check=True)

    file_size_mb = FINAL_MP4.stat().st_size / (1024 * 1024)
    print(f"🎉 [5/5] Successfully generated demo video:")
    print(f"    File: {FINAL_MP4}")
    print(f"    Size: {file_size_mb:.2f} MB")
    return 0

if __name__ == "__main__":
    sys.exit(main())
