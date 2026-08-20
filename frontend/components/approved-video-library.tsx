"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  API_URL,
  isRecentApprovedVideo,
  mergeRecentVideos,
  voiceProviderLabel,
  type RecentApprovedVideo,
} from "../lib/video-ui";

type LibraryState = "loading" | "ready" | "error";

export default function ApprovedVideoLibrary() {
  const [videos, setVideos] = useState<RecentApprovedVideo[]>([]);
  const [selectedVideo, setSelectedVideo] = useState<RecentApprovedVideo | null>(null);
  const [state, setState] = useState<LibraryState>("loading");
  const [error, setError] = useState<string | null>(null);

  const loadVideos = useCallback(async (signal?: AbortSignal) => {
    setState("loading");
    setError(null);
    try {
      const response = await fetch(`${API_URL}/api/jobs/recent`, { signal });
      const data: unknown = await response.json();
      if (!response.ok || !Array.isArray(data)) {
        throw new Error("The approved library could not be loaded.");
      }
      const validVideos = data.filter(isRecentApprovedVideo);
      setVideos(current => mergeRecentVideos(current, validVideos));
      setSelectedVideo(current => current && validVideos.some(video => video.job_id === current.job_id)
        ? validVideos.find(video => video.job_id === current.job_id) || current
        : current);
      setState("ready");
    } catch (caught) {
      if ((caught as Error).name === "AbortError") return;
      setState("error");
      setError(caught instanceof Error ? caught.message : "The approved library could not be loaded.");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void loadVideos(controller.signal), 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadVideos]);

  return (
    <div className="library-workspace">
      <div className="page-intro page-intro--library">
        <div>
          <p className="eyebrow">Approved library</p>
          <h1>Finished videos, ready to reuse.</h1>
          <p className="page-intro__lede">Watch or download an existing result without starting a new render.</p>
        </div>
        <p className="library-count" aria-live="polite">{videos.length} approved {videos.length === 1 ? "video" : "videos"}</p>
      </div>

      {state === "loading" && (
        <p className="status-block" role="status">Loading approved videos…</p>
      )}

      {state === "error" && (
        <div className="status-block status-block--error" role="alert">
          <p>{error}</p>
          <button type="button" className="button button--secondary" onClick={() => void loadVideos()}>
            Try again
          </button>
        </div>
      )}

      {state === "ready" && videos.length === 0 && (
        <div className="empty-state">
          <h2>No approved videos yet.</h2>
          <p>Create and approve a video from the Create workspace, then it will appear here.</p>
          <Link className="button button--primary" href="/">Go to Create</Link>
        </div>
      )}

      {state === "ready" && videos.length > 0 && (
        <div className="library-grid">
          <section className="library-list" aria-labelledby="library-list-title">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Recent results</p>
                <h2 id="library-list-title">Select a video to watch</h2>
              </div>
            </div>
            <ul className="library-list__items">
              {videos.map(video => {
                const isSelected = selectedVideo?.job_id === video.job_id;
                return (
                  <li key={video.job_id} className={`library-item${isSelected ? " library-item--selected" : ""}`}>
                    <button
                      type="button"
                      className="library-item__select"
                      aria-pressed={isSelected}
                      onClick={() => setSelectedVideo(video)}
                    >
                      <span className="library-item__title" title={video.title}>{video.title}</span>
                      <span className="library-item__meta">{voiceProviderLabel(video.voice_provider)} · {new Date(video.updated_at).toLocaleString()}</span>
                    </button>
                    <a className="library-item__download" href={`${API_URL}${video.video_url}`} download>
                      Download MP4
                    </a>
                  </li>
                );
              })}
            </ul>
          </section>

          <section className="library-player" aria-labelledby="library-player-title">
            <div className="section-heading section-heading--compact">
              <div>
                <p className="eyebrow">Preview</p>
                <h2 id="library-player-title">{selectedVideo?.title || "Choose an approved video"}</h2>
              </div>
              {selectedVideo && (
                <a className="text-action" href={`${API_URL}${selectedVideo.video_url}`} download>
                  Download MP4
                </a>
              )}
            </div>
            <div className="library-player__surface">
              {selectedVideo ? (
                <video
                  key={selectedVideo.job_id}
                  controls
                  playsInline
                  className="library-player__video"
                  src={`${API_URL}${selectedVideo.video_url}`}
                />
              ) : (
                <p className="preview-empty">Select a video from the list to play it here.</p>
              )}
            </div>
            {selectedVideo && (
              <p className="library-player__meta">
                {voiceProviderLabel(selectedVideo.voice_provider)} · {new Date(selectedVideo.updated_at).toLocaleString()}
              </p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
