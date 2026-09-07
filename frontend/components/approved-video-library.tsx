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

function existingGenerationAccessHeaders(): HeadersInit {
  if (typeof window === "undefined") return {};
  const token = window.sessionStorage.getItem("fyf-generation-access")?.trim();
  return token ? { "X-FYF-Access-Token": token } : {};
}

function verificationLabel(value: boolean | undefined): { label: string; color: string } {
  if (value === true) return { label: "✓ Passed", color: "#16856B" };
  if (value === false) return { label: "✗ Failed", color: "#9F2F2D" };
  return { label: "Unavailable", color: "var(--muted)" };
}

interface JobStatusDetails {
  job_id?: string;
  status?: string;
  updated_at?: string;
  voice_provider?: string;
  qa_report?: {
    passed?: boolean;
    metrics?: {
      voice_duration?: number;
      video_duration?: number;
    };
    checks?: Array<{
      name: string;
      passed: boolean;
    }>;
  };
  final_visual_qa?: {
    passed?: boolean;
    checks?: Array<{
      name: string;
      passed: boolean;
    }>;
  };
  [key: string]: unknown;
}

export default function ApprovedVideoLibrary() {
  const [videos, setVideos] = useState<RecentApprovedVideo[]>([]);
  const [selectedVideo, setSelectedVideo] = useState<RecentApprovedVideo | null>(null);
  const [state, setState] = useState<LibraryState>("loading");
  const [error, setError] = useState<string | null>(null);

  // Metadata modal state
  const [metadataVideo, setMetadataVideo] = useState<RecentApprovedVideo | null>(null);
  const [jobMetadata, setJobMetadata] = useState<JobStatusDetails | null>(null);
  const [metadataLoading, setMetadataLoading] = useState(false);
  const [metadataTab, setMetadataTab] = useState<"qa" | "json">("qa");

  // Archive modal state
  const [archiveConfirmVideo, setArchiveConfirmVideo] = useState<RecentApprovedVideo | null>(null);
  const [isArchiving, setIsArchiving] = useState(false);
  const [notification, setNotification] = useState<string | null>(null);

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
        : (validVideos[0] || null));
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

  // Open metadata modal and fetch status details
  async function openMetadata(video: RecentApprovedVideo) {
    setMetadataVideo(video);
    setJobMetadata(null);
    setMetadataLoading(true);
    setMetadataTab("qa");
    try {
      const res = await fetch(`${API_URL}/api/jobs/${video.job_id}/status`);
      if (res.ok) {
        const data = (await res.json()) as JobStatusDetails;
        setJobMetadata(data);
      }
    } catch (err) {
      console.warn("Could not fetch full metadata for job", video.job_id, err);
    } finally {
      setMetadataLoading(false);
    }
  }

  function closeMetadata() {
    setMetadataVideo(null);
    setJobMetadata(null);
  }

  // Handle archive confirmation
  async function handleArchiveConfirm() {
    if (!archiveConfirmVideo) return;
    setIsArchiving(true);
    const targetId = archiveConfirmVideo.job_id;
    try {
      const res = await fetch(`${API_URL}/api/jobs/${targetId}`, {
        method: "DELETE",
        headers: existingGenerationAccessHeaders(),
      });
      if (!res.ok) throw new Error("Failed to archive video");
      setVideos(prev => {
        const remaining = prev.filter(v => v.job_id !== targetId);
        if (selectedVideo?.job_id === targetId) {
          setSelectedVideo(remaining.length > 0 ? remaining[0] : null);
        }
        return remaining;
      });
      setNotification(`Video "${archiveConfirmVideo.title}" has been archived.`);
      setTimeout(() => setNotification(null), 4000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to archive video");
    } finally {
      setIsArchiving(false);
      setArchiveConfirmVideo(null);
    }
  }

  // Handle ESC key for modals
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        if (metadataVideo) closeMetadata();
        if (archiveConfirmVideo) setArchiveConfirmVideo(null);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [metadataVideo, archiveConfirmVideo]);

  return (
    <div className="library-workspace">
      <div className="page-intro page-intro--library">
        <div>
          <p className="eyebrow">Approved library</p>
          <h1>Finished videos, ready to reuse.</h1>
          <p className="page-intro__lede">Watch, inspect metadata, or download an existing result without starting a new render.</p>
        </div>
        <p className="library-count" aria-live="polite">{videos.length} approved {videos.length === 1 ? "video" : "videos"}</p>
      </div>

      {notification && (
        <div className="notice-banner notice-banner--success" role="status" style={{ marginBottom: "1rem" }}>
          <p>{notification}</p>
        </div>
      )}

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
                    <div className="library-item__actions">
                      <button
                        type="button"
                        className="button button--secondary button--compact"
                        onClick={() => void openMetadata(video)}
                        aria-label={`View metadata for ${video.title}`}
                      >
                        Metadata
                      </button>
                      <a className="button button--secondary button--compact library-item__download" href={`${API_URL}${video.video_url}`} download>
                        Download MP4
                      </a>
                      <button
                        type="button"
                        className="button button--compact library-item__archive"
                        onClick={() => setArchiveConfirmVideo(video)}
                        aria-label={`Archive ${video.title}`}
                      >
                        Archive
                      </button>
                    </div>
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
                <div className="library-player-actions" style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
                  <button
                    type="button"
                    className="button button--secondary button--compact"
                    onClick={() => void openMetadata(selectedVideo)}
                  >
                    Metadata
                  </button>
                  <a className="button button--primary button--compact" href={`${API_URL}${selectedVideo.video_url}`} download>
                    Download MP4
                  </a>
                  <button
                    type="button"
                    className="button button--compact"
                    style={{ background: "#FEE2E2", color: "#991B1B", border: "1px solid #FCA5A5" }}
                    onClick={() => setArchiveConfirmVideo(selectedVideo)}
                  >
                    Archive
                  </button>
                </div>
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
                  aria-label={`Video player for ${selectedVideo.title}`}
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

      {/* Metadata Modal */}
      {metadataVideo && (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="modal-title"
          onClick={(e) => { if (e.target === e.currentTarget) closeMetadata(); }}
        >
          <div className="modal-dialog">
            <div className="modal-header">
              <div>
                <span className="modal-tag">Video Metadata</span>
                <h3 id="modal-title">{metadataVideo.title}</h3>
                <p className="modal-sub">Job ID: <code>{metadataVideo.job_id}</code></p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={closeMetadata}
                aria-label="Close modal"
              >
                ×
              </button>
            </div>

            <div className="modal-tabs">
              <button
                type="button"
                className={`modal-tab ${metadataTab === "qa" ? "modal-tab--active" : ""}`}
                onClick={() => setMetadataTab("qa")}
              >
                QA Verification
              </button>
              <button
                type="button"
                className={`modal-tab ${metadataTab === "json" ? "modal-tab--active" : ""}`}
                onClick={() => setMetadataTab("json")}
              >
                Technical JSON
              </button>
            </div>

            <div className="modal-body">
              {metadataLoading && <p className="status-block">Loading verification records…</p>}

              {!metadataLoading && metadataTab === "qa" && (
                <div className="modal-qa-details">
                  <div className="modal-stat-grid">
                    <div className="modal-stat-card">
                      <span className="modal-stat-label">Output QA</span>
                      <span className="modal-stat-value" style={{ color: verificationLabel(jobMetadata?.qa_report?.passed).color }}>
                        {verificationLabel(jobMetadata?.qa_report?.passed).label}
                      </span>
                    </div>
                    <div className="modal-stat-card">
                      <span className="modal-stat-label">Visual Evidence QA</span>
                      <span className="modal-stat-value" style={{ color: verificationLabel(jobMetadata?.final_visual_qa?.passed).color }}>
                        {verificationLabel(jobMetadata?.final_visual_qa?.passed).label}
                      </span>
                    </div>
                    <div className="modal-stat-card">
                      <span className="modal-stat-label">Duration</span>
                      <span className="modal-stat-value">
                        {typeof jobMetadata?.qa_report?.metrics?.video_duration === "number"
                          && Number.isFinite(jobMetadata.qa_report.metrics.video_duration)
                          && jobMetadata.qa_report.metrics.video_duration > 0
                          ? `${jobMetadata.qa_report.metrics.video_duration.toFixed(1)}s`
                          : "Unavailable"}
                      </span>
                    </div>
                    <div className="modal-stat-card">
                      <span className="modal-stat-label">Voice Mode</span>
                      <span className="modal-stat-value">
                        {voiceProviderLabel(metadataVideo.voice_provider)}
                      </span>
                    </div>
                  </div>

                  <h4 style={{ margin: "16px 0 8px", fontSize: "0.85rem", fontWeight: 700 }}>Verification Checks</h4>
                  <ul className="modal-check-list">
                    {(jobMetadata?.qa_report?.checks || jobMetadata?.final_visual_qa?.checks || []).map((check, idx) => (
                      <li key={idx} className="modal-check-item">
                        <span className="modal-check-mark" style={{ color: check.passed ? "#16856B" : "#9F2F2D" }}>
                          {check.passed ? "✓" : "✗"}
                        </span>
                        <code>{check.name}</code>
                      </li>
                    ))}
                    {!jobMetadata?.qa_report?.checks?.length && !jobMetadata?.final_visual_qa?.checks?.length && (
                      <li className="modal-check-item">
                        <span className="modal-check-mark" style={{ color: "var(--muted)" }}>—</span>
                        <span>Verification checks unavailable.</span>
                      </li>
                    )}
                  </ul>
                </div>
              )}

              {!metadataLoading && metadataTab === "json" && (
                <pre className="modal-json-viewer">
                  {JSON.stringify(jobMetadata || metadataVideo, null, 2)}
                </pre>
              )}
            </div>

            <div className="modal-footer">
              <a
                className="button button--primary button--compact"
                href={`${API_URL}${metadataVideo.video_url}`}
                download
              >
                Download MP4
              </a>
              <button
                type="button"
                className="button button--secondary button--compact"
                onClick={closeMetadata}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Archive Confirmation Dialog */}
      {archiveConfirmVideo && (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="archive-dialog-title"
          onClick={(e) => { if (e.target === e.currentTarget) setArchiveConfirmVideo(null); }}
        >
          <div className="modal-dialog modal-dialog--compact">
            <div className="modal-header">
              <h3 id="archive-dialog-title">Archive Video?</h3>
              <button
                type="button"
                className="modal-close"
                onClick={() => setArchiveConfirmVideo(null)}
                aria-label="Close archive dialog"
              >
                ×
              </button>
            </div>
            <div className="modal-body">
              <p>Are you sure you want to archive <strong>{archiveConfirmVideo.title}</strong>?</p>
              <p className="helper-text" style={{ marginTop: "8px" }}>
                This removes the video from your active library display.
              </p>
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="button button--secondary button--compact"
                onClick={() => setArchiveConfirmVideo(null)}
                disabled={isArchiving}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button button--compact"
                style={{ background: "#9F2F2D", color: "#FFFFFF", borderColor: "#9F2F2D" }}
                onClick={() => void handleArchiveConfirm()}
                disabled={isArchiving}
              >
                {isArchiving ? "Archiving…" : "Confirm Archive"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
