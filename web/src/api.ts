export interface Satellite { norad_id: number; name: string; tle_epoch: string | null; enabled: boolean }
export interface SatelliteRefresh extends Satellite { changed: boolean; previous_epoch: string | null }
export interface Pass {
  id: number; norad_id: number | null; satellite_name: string; kind: "pass" | "event"; event_id: number | null;
  aos: string; tca: string; los: string; rec_start: string | null; rec_end: string | null;
  max_elevation_deg: number; status: string;
}
export interface Recording {
  id: number; norad_id: number | null; path: string; sample_format: string;
  center_freq_hz: number; sample_rate_hz: number; started_at: string; ended_at: string | null;
  size_bytes: number; deleted: boolean; orbit_meta_path: string | null; source?: string | null; label?: string | null;
}
export interface RecorderStatus {
  state: "idle" | "recording" | "stopping"; recording_id: number | null;
  duration_s: number; elapsed_s: number; last_error: string | null;
}
export interface StartRecording {
  center_freq_hz: number; sample_rate_hz: number; duration_s: number; gain_db?: number | null; bandwidth_hz?: number | null;
}
export interface StartFmRecording {
  center_freq_hz: number; duration_s: number; mode: string; bandwidth_hz: number; gain_db?: number | null; squelch: number; label?: string | null;
}
export interface Profile {
  id: number; name: string; center_freq_hz: number; bandwidth_hz: number; sample_rate_hz: number;
  gain_db: number | null; modulation_hint: string; doppler_correction: boolean; antenna_id: number | null;
}
export type ProfileIn = Omit<Profile, "id">;
export interface PipelineStep { processor: string; params?: Record<string, unknown> }
export interface Pipeline { id: number; name: string; mode: string; keep_iq: boolean; steps: PipelineStep[] }
export interface PipelineIn { name: string; mode: string; keep_iq: boolean; steps: PipelineStep[] }
export interface Processor { name: string; title: string; params_schema: Record<string, any> }
export interface RuleIn {
  norad_id: number; valid_from: string; valid_to: string; min_elevation_deg: number | null;
  priority: number; profile_id: number; pipeline_id: number; allow_partial: boolean; enabled: boolean;
}
export interface Rule extends RuleIn { id: number; satellite_name: string }
export interface TimedEventIn {
  name: string; enabled: boolean; start_at: string; duration_s: number; recurrence: "none" | "daily" | "weekly";
  weekdays: number[]; repeat_until: string | null; timezone: string; profile_id: number; pipeline_id: number;
  priority: number; allow_partial: boolean; notes: string;
}
export interface TimedEvent extends TimedEventIn {
  id: number; next_occurrences: string[]; estimated_bytes: number | null; profile_name: string; pipeline_name: string;
}
export interface TimedEventPreview { next_occurrences: string[]; weekdays: number[]; estimated_bytes: number }
export interface HealthComponent {
  id: string; status: "ok" | "warning" | "error"; code: string; message: string; details: Record<string, any>;
}
export interface Health { status: "ok" | "warning" | "error"; components: HealthComponent[]; time: string }
export interface AppEvent {
  id: number; ts: string; level: "info" | "warning" | "error"; source: string; code: string; message: string;
  details: Record<string, any>; pass_id: number | null; job_id: number | null; recording_id: number | null;
  acknowledged: boolean;
}
export interface EventSummary { error: number; warning: number; info: number }
export interface Artifact {
  id: number; job_id: number | null; recording_id: number | null; norad_id: number | null;
  satellite_name: string | null; kind: string; has_thumbnail: boolean; meta: Record<string, any> & Partial<WaterfallMeta>; created_at: string;
}
export interface Job {
  id: number; kind: "recording" | "processing"; status: string; pass_id: number | null; recording_id: number | null;
  pipeline_id: number | null; pipeline_name: string | null; satellite_name: string | null;
  started_at: string | null; finished_at: string | null; error: string | null;
}
export interface JobStep {
  idx: number; processor: string; params: Record<string, any>; status: string;
  started_at: string | null; finished_at: string | null; error: string | null; log: string;
}
export interface JobDetail extends Job { steps: JobStep[]; artifacts: Artifact[] }
export interface WaterfallMeta {
  fft_size: number; rows: number; row_s: number; bin_hz: number; colormap: string; db_min: number; db_max: number;
  ref_center_hz: number; f_lo_hz: number; f_hi_hz: number; t0_s: number; t1_s: number; doppler_overlay: boolean;
  plot: { x: number; y: number; w: number; h: number }; width: number; height: number;
}
export interface RecordingDetailData {
  recording: {
    id: number; norad_id: number | null; satellite_name: string | null; path: string; sample_format: string;
    center_freq_hz: number; sample_rate_hz: number; started_at: string; ended_at: string | null; size_bytes: number;
    deleted: boolean; protected: boolean; source?: string | null;
  };
  iq_available: boolean; storage_available: boolean;
  info: {
    format: string; sample_rate_hz: number; center_freq_hz: number; num_samples: number; duration_s: number;
    retunes: { t_s: number; freq_hz: number }[];
  } | null;
  orbit: {
    satellite: { norad_id: number | null; name: string | null; tle: { epoch: string | null } | null } | null;
    pass: { aos: string; los: string; max_elevation_deg: number | null } | null;
    has_track: boolean; track_points: number; max_elevation_deg: number | null;
  } | null;
  artifacts: Artifact[];
  jobs: Job[];
}
export interface ToolsInfo {
  satdump_path: string | null; configured_from: string;
  satdump: { found: boolean; path: string | null; source: string };
  rtl_sdr_path: string | null; rtl_fm_path: string | null;
  rtl_sdr: { found: boolean; path: string | null }; rtl_fm: { found: boolean; path: string | null };
}
export type ToolPaths = { satdump_path: string | null; rtl_sdr_path: string | null; rtl_fm_path: string | null };
export interface StorageRootInfo {
  id: number; kind: "recordings" | "artifacts"; path: string; label: string; active: boolean; available: boolean;
  used_bytes: number; free_bytes: number | null;
}
export interface MoveStatus {
  state: "idle" | "running" | "done" | "failed" | "cancelled"; kind: string | null; total_items: number; done_items: number;
  total_bytes: number; done_bytes: number; current: string | null; skipped: { type: string; id: number; reason: string }[];
  errors: { type: string; id: number; error: string }[]; error: string | null;
}
export interface StorageOverview {
  roots: StorageRootInfo[]; recordings_bytes: number; artifacts_bytes: number; total_bytes: number; recordings_count: number;
  artifacts_count: number; quota_bytes: number; quota_used_fraction: number | null; growth_7d_bytes: number;
  days_until_disk_full: number | null; retention: { iq_retention_days: number; artifact_retention_days: number; quota_gb: number }; move: MoveStatus;
  checks?: Record<string, ValidationResult>;
}
export interface ValidationResult {
  ok: boolean; errors: string[]; warnings: string[]; path: string; exists: boolean; writable: boolean;
  free_bytes: number | null; non_empty: boolean; existing_root_id: number | null;
}
export interface StorageItem {
  id: number; started_at: string; norad_id: number | null; satellite_name: string | null; center_freq_hz: number;
  sample_rate_hz: number; iq_bytes: number; iq_available: boolean; artifact_count: number; artifact_bytes: number;
  total_bytes: number; protected: boolean; processed: boolean; root_id: number | null; storage_available: boolean; has_orbit: boolean;
  source?: string | null;
}
export interface StorageList { total: number; total_bytes: number; iq_bytes: number; artifact_bytes: number; items: StorageItem[] }
export interface DeletePlan {
  items: { type: string; id: number; action: string; bytes: number; skipped: string | null }[];
  freed_bytes: number; recordings: number; artifacts: number; skipped: number;
}
export interface RetentionResult {
  aged_out: number; over_quota: number; unprocessed_deleted: number; artifacts_deleted: number; freed_bytes: number; recording_ids: number[];
  notes: string[]; dry_run: boolean;
}
export interface ExportJob {
  id: string; state: "queued" | "running" | "done" | "failed" | "cancelled"; mode: "zip" | "directory"; total_bytes: number;
  done_bytes: number; files: number; current: string | null; error: string | null; result: string | null;
  moved: { recordings: number; artifacts: number } | null;
}
export interface SdrStatus {
  backend: string; simulated: boolean; recorder_state: string;
  probe: { backend: string; status: string; message: string; devices: { index: number; serial: string; label?: string }[]; simulated: boolean } | null;
}
export interface Observer { lat: number; lon: number; alt_m: number }

function detailText(d: any): string {
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x?.msg ?? JSON.stringify(x)).join("; ");   // FastAPI-ellenőrzési hibák
  if (d && typeof d === "object") {
    if (d.message) return String(d.message);
    if (d.reasons) return `${d.code}: ${d.reasons.join(", ")}`;
    if (d.checks) return Object.entries(d.checks).map(([k, c]) => `${k}: ${(c as any).errors.join(", ")}`).join("; ");
    return JSON.stringify(d);
  }
  return "";
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = localStorage.getItem("token");
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401) {
    const entered = window.prompt("API token");
    if (entered) {
      localStorage.setItem("token", entered);
      return request<T>(method, url, body);
    }
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detailText(detail.detail) || `HTTP ${res.status}`);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export const api = {
  satellites: () => request<Satellite[]>("GET", "/api/satellites"),
  addSatellite: (norad_id: number) => request<Satellite>("POST", "/api/satellites", { norad_id }),
  patchSatellite: (id: number, enabled: boolean) => request<Satellite>("PATCH", `/api/satellites/${id}`, { enabled }),
  deleteSatellite: (id: number) => request<void>("DELETE", `/api/satellites/${id}`),
  refreshTle: (id: number) => request<SatelliteRefresh>("POST", `/api/satellites/${id}/refresh-tle`),
  passes: (hours = 48) => request<Pass[]>("GET", `/api/passes?hours=${hours}`),
  recorderStatus: () => request<RecorderStatus>("GET", "/api/recorder/status"),
  startRecording: (b: StartRecording) => request<Recording>("POST", "/api/recorder/start", b),
  startFmRecording: (b: StartFmRecording) => request<{ job_id: number }>("POST", "/api/recorder/start-fm", b),
  stopRecording: () => request<void>("POST", "/api/recorder/stop"),
  recordings: () => request<Recording[]>("GET", "/api/recordings"),
  deleteRecording: (id: number) => request<void>("DELETE", `/api/recordings/${id}`),
  profiles: () => request<Profile[]>("GET", "/api/profiles"),
  createProfile: (p: ProfileIn) => request<Profile>("POST", "/api/profiles", p),
  deleteProfile: (id: number) => request<void>("DELETE", `/api/profiles/${id}`),
  pipelines: () => request<Pipeline[]>("GET", "/api/pipelines"),
  processors: () => request<Processor[]>("GET", "/api/processors"),
  createPipeline: (p: PipelineIn) => request<Pipeline>("POST", "/api/pipelines", p),
  updatePipeline: (id: number, p: PipelineIn) => request<Pipeline>("PUT", `/api/pipelines/${id}`, p),
  deletePipeline: (id: number) => request<void>("DELETE", `/api/pipelines/${id}`),
  rules: () => request<Rule[]>("GET", "/api/rules"),
  createRule: (r: RuleIn) => request<Rule>("POST", "/api/rules", r),
  updateRule: (id: number, r: RuleIn) => request<Rule>("PUT", `/api/rules/${id}`, r),
  deleteRule: (id: number) => request<void>("DELETE", `/api/rules/${id}`),
  timedEvents: () => request<TimedEvent[]>("GET", "/api/timed-events"),
  previewTimedEvent: (b: TimedEventIn) => request<TimedEventPreview>("POST", "/api/timed-events/preview", b),
  createTimedEvent: (b: TimedEventIn) => request<TimedEvent>("POST", "/api/timed-events", b),
  updateTimedEvent: (id: number, b: TimedEventIn) => request<TimedEvent>("PUT", `/api/timed-events/${id}`, b),
  deleteTimedEvent: (id: number) => request<void>("DELETE", `/api/timed-events/${id}`),
  health: () => request<Health>("GET", "/api/health/detailed"),
  events: (q: { level?: string; unacked?: boolean; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (q.level) p.set("level", q.level);
    if (q.unacked) p.set("unacked", "true");
    p.set("limit", String(q.limit ?? 100));
    return request<AppEvent[]>("GET", `/api/events?${p}`);
  },
  eventSummary: () => request<EventSummary>("GET", "/api/events/summary"),
  ackEvent: (id: number) => request<void>("POST", `/api/events/${id}/ack`),
  ackAll: () => request<{ acknowledged: number }>("POST", "/api/events/ack-all"),
  jobs: (limit = 30) => request<Job[]>("GET", `/api/jobs?limit=${limit}`),
  job: (id: number) => request<JobDetail>("GET", `/api/jobs/${id}`),
  retryJob: (id: number) => request<{ job_id: number }>("POST", `/api/jobs/${id}/retry`),
  processRecording: (id: number, pipeline_id: number) =>
    request<{ job_id: number }>("POST", `/api/recordings/${id}/process`, { pipeline_id }),
  artifacts: (kind?: string) => request<Artifact[]>("GET", `/api/artifacts?limit=200${kind ? `&kind=${kind}` : ""}`),
  deleteArtifact: (id: number) => request<void>("DELETE", `/api/artifacts/${id}`),
  recordingDetail: (id: number) => request<RecordingDetailData>("GET", `/api/recordings/${id}`),
  runSteps: (id: number, steps: { processor: string; params: Record<string, unknown> }[]) =>
    request<{ job_id: number }>("POST", `/api/recordings/${id}/run`, { steps }),
  tools: () => request<ToolsInfo>("GET", "/api/settings/tools"),
  setTools: (paths: Partial<ToolPaths>) => request<ToolsInfo>("PUT", "/api/settings/tools", paths),
  storage: () => request<StorageOverview>("GET", "/api/storage"),
  validatePath: (path: string) => request<ValidationResult>("POST", "/api/storage/validate", { path }),
  setRoots: (b: { recordings_path?: string; artifacts_path?: string }) => request<StorageOverview>("PUT", "/api/storage/roots", b),
  startMove: (kind: "recordings" | "artifacts") => request<MoveStatus>("POST", "/api/storage/move", { kind }),
  moveStatus: () => request<MoveStatus>("GET", "/api/storage/move"),
  cancelMove: () => request<void>("POST", "/api/storage/move/cancel"),
  storageRecordings: (f: { state: string; sort: string; order: string }) =>
    request<StorageList>("GET", `/api/storage/recordings?state=${f.state}&sort=${f.sort}&order=${f.order}&limit=500`),
  deleteData: (b: { recording_ids?: number[]; artifact_ids?: number[]; scope?: "iq_only" | "all"; dry_run?: boolean }) =>
    request<DeletePlan & { result?: { freed_bytes: number } }>("POST", "/api/storage/delete", b),
  retention: (dry_run: boolean) => request<RetentionResult>("POST", "/api/storage/retention", { dry_run }),
  protectRecording: (id: number, protected_: boolean) => request<{ id: number; protected: boolean }>("PATCH", `/api/recordings/${id}`, { protected: protected_ }),
  createExport: (b: { recording_ids?: number[]; artifact_ids?: number[]; mode: "zip" | "directory"; directory?: string | null; include_iq?: boolean; move?: boolean }) =>
    request<ExportJob>("POST", "/api/exports", b),
  exportStatus: (id: string) => request<ExportJob>("GET", `/api/exports/${id}`),
  cancelExport: (id: string) => request<void>("POST", `/api/exports/${id}/cancel`),
  sdr: (refresh = false) => request<SdrStatus>("GET", `/api/sdr${refresh ? "?refresh=true" : ""}`),
  observer: () => request<Observer | null>("GET", "/api/settings/observer"),
  setObserver: (o: Observer) => request<Observer>("PUT", "/api/settings/observer", o),
};

/** Közvetlenül a böngésző által töltött fájlok (<audio>, <img>): a token query paraméterként megy. */
export function artifactUrl(id: number, thumbnail = false): string {
  const token = localStorage.getItem("token");
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  return `/api/artifacts/${id}/${thumbnail ? "thumbnail" : "file"}${q}`;
}

/** Letöltési hivatkozás (<a href>): a böngésző nem tud fejlécet küldeni, ezért a token query paraméterként megy. */
export function authUrl(path: string): string {
  const token = localStorage.getItem("token");
  return token ? `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : path;
}
