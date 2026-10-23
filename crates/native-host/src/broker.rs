//! Authenticated native-only RPC. No UI URL, SQL or shell parameters are accepted.
use anyhow::{bail, ensure, Result};
use axum::{
    extract::{DefaultBodyLimit, State},
    http::{HeaderMap, StatusCode},
    routing::post,
    Json, Router,
};
use serde_json::{json, Value};
use std::{path::PathBuf, sync::Arc};
use subtle::ConstantTimeEq;
use tokio::sync::{Mutex, RwLock};
#[derive(Clone)]
pub struct Broker {
    pub engine: Arc<RwLock<Option<Arc<crate::supervisor::Engine>>>>,
    pub capture: Arc<Mutex<crate::capture::Capture>>,
    pub vault: crate::vault::Vault,
    pub oauth: crate::oauth::OAuth,
    pub grants: Arc<Mutex<crate::grants::Grants>>,
    pub media_root: PathBuf,
    pub events: crate::Events,
    token: Arc<zeroize::Zeroizing<String>>,
    pub playback: Arc<Mutex<Option<native_media::playback::Playback>>>,
    decode_jobs: Arc<Mutex<std::collections::HashMap<String, Arc<std::sync::atomic::AtomicBool>>>>,
}
impl Broker {
    pub fn new(
        capture: crate::capture::Capture,
        vault: crate::vault::Vault,
        media_root: PathBuf,
        token: zeroize::Zeroizing<String>,
        events: crate::Events,
    ) -> Result<Self> {
        Ok(Self {
            engine: Arc::new(RwLock::new(None)),
            capture: Arc::new(Mutex::new(capture)),
            oauth: crate::oauth::OAuth::new(vault.clone(), events.clone())?,
            vault,
            grants: Arc::new(Mutex::new(crate::grants::Grants::new())),
            media_root,
            events,
            token: Arc::new(token),
            playback: Arc::new(Mutex::new(None)),
            decode_jobs: Arc::new(Mutex::new(std::collections::HashMap::new())),
        })
    }
    pub async fn listen(&self) -> Result<u16> {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await?;
        let port = listener.local_addr()?.port();
        let app = Router::new()
            .route("/rpc", post(rpc))
            .layer(DefaultBodyLimit::max(1_048_576))
            .with_state(self.clone());
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        Ok(port)
    }
    pub async fn engine(&self) -> Result<Arc<crate::supervisor::Engine>> {
        self.engine
            .read()
            .await
            .clone()
            .ok_or_else(|| anyhow::anyhow!("engine is not ready"))
    }
    pub async fn request(&self, operation: &str, payload: Value) -> Result<Value> {
        match operation {
            "vault.resolve" => {
                let reference = required(&payload, "secret_ref")?;
                let value = self.vault.resolve(reference)?;
                Ok(json!({"value":value.as_str()}))
            }
            "vault.store" => {
                self.vault.store(
                    required(&payload, "secret_ref")?,
                    required(&payload, "value")?,
                )?;
                Ok(json!({"stored":true}))
            }
            "vault.delete" => {
                self.vault.delete(required(&payload, "secret_ref")?)?;
                Ok(json!({"deleted":true}))
            }
            "oauth.authorize" => self.oauth.authorize(payload).await,
            "oauth.device" => self.oauth.device(payload).await,
            "oauth.begin" => {
                if payload["provider"] == "github" {
                    self.oauth.device(payload).await
                } else {
                    self.oauth.authorize(payload).await
                }
            }
            "oauth.poll" => self.oauth.poll(required(&payload, "flow_id")?).await,
            "oauth.cancel" => self.oauth.cancel(payload["flow_id"].as_str()).await,
            "oauth.refresh" => self.oauth.refresh(required(&payload, "secret_ref")?).await,
            "oauth.revoke" => {
                self.oauth
                    .revoke(
                        required(&payload, "secret_ref")?,
                        payload["provider"].as_str(),
                        payload["allow_remote"].as_bool().unwrap_or(true),
                    )
                    .await
            }
            "file.resolve" => {
                let path = self.grants.lock().await.resolve(
                    required(&payload, "grant_id")?,
                    required(&payload, "purpose")?,
                )?;
                Ok(json!({"path":path}))
            }
            "file.revoke" => {
                self.grants
                    .lock()
                    .await
                    .revoke(required(&payload, "grant_id")?);
                Ok(json!({"revoked":true}))
            }
            "file.write" => {
                let id = required(&payload, "grant_id")?.to_owned();
                let destination = self.grants.lock().await.write_target(&id)?;
                let root = self
                    .media_root
                    .parent()
                    .ok_or_else(|| anyhow::anyhow!("Workspace root missing"))?
                    .to_owned();
                let result = tokio::task::spawn_blocking(move || {
                    crate::export::save(&root, &destination, &payload)
                })
                .await;
                self.grants.lock().await.revoke(&id);
                result?
            }
            "backup.encrypt" | "backup.decrypt" => {
                let root = self
                    .media_root
                    .parent()
                    .ok_or_else(|| anyhow::anyhow!("workspace root unavailable"))?;
                let secret = self
                    .vault
                    .resolve(required(&payload, "passphrase_secret_ref")?)?;
                let (grant, purpose) = if operation == "backup.encrypt" {
                    ("output_grant", "backup")
                } else {
                    ("input_grant", "restore")
                };
                let id = required(&payload, grant)?;
                let path = self.grants.lock().await.resolve(id, purpose)?;
                let result = if operation == "backup.encrypt" {
                    crate::backup::encrypt(
                        root,
                        required(&payload, "input_relative_path")?,
                        &path,
                        secret.as_str(),
                    )
                } else {
                    crate::backup::decrypt(
                        root,
                        &path,
                        required(&payload, "output_relative_path")?,
                        secret.as_str(),
                    )
                };
                self.grants.lock().await.revoke(id);
                result
            }
            "backup.commit" => {
                let root = self
                    .media_root
                    .parent()
                    .ok_or_else(|| anyhow::anyhow!("workspace root unavailable"))?;
                let id = required(&payload, "output_grant")?;
                let path = self.grants.lock().await.resolve(id, "backup")?;
                let result =
                    crate::backup::commit(root, required(&payload, "input_relative_path")?, &path);
                self.grants.lock().await.revoke(id);
                result
            }
            "media.decode" | "media.decode_existing" => {
                ensure!(
                    payload["temporary_decode_consent"] == true,
                    "temporary audio decode consent is required"
                );
                let job = required(&payload, "job_id")?.to_owned();
                uuid::Uuid::parse_str(&job)?;
                let relative = required(&payload, "output_relative_path")?.to_owned();
                let root = self
                    .media_root
                    .parent()
                    .ok_or_else(|| anyhow::anyhow!("workspace root unavailable"))?
                    .to_owned();
                let (file, name) = if operation == "media.decode" {
                    let path = self
                        .grants
                        .lock()
                        .await
                        .resolve(required(&payload, "grant_id")?, "import_audio")?;
                    (
                        std::fs::File::open(&path)?,
                        path.file_name()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .into_owned(),
                    )
                } else {
                    let id = required(&payload, "media_id")?;
                    uuid::Uuid::parse_str(id)?;
                    let catalog = native_media::reconcile(&self.media_root)?;
                    let commit = catalog
                        .iter()
                        .find(|c| c.media_id == id)
                        .ok_or_else(|| anyhow::anyhow!("retained media unavailable"))?;
                    (
                        std::fs::File::open(native_media::scoped_path(
                            &self.media_root,
                            &commit.relative_path,
                        )?)?,
                        format!("{id}.opus"),
                    )
                };
                let cancel = Arc::new(std::sync::atomic::AtomicBool::new(false));
                {
                    let mut jobs = self.decode_jobs.lock().await;
                    ensure!(
                        jobs.len() < 2 && !jobs.contains_key(&job),
                        "native decoder is busy"
                    );
                    jobs.insert(job.clone(), cancel.clone());
                }
                let result = tokio::task::spawn_blocking(move || {
                    crate::decode::decode(&root, file, name, relative, cancel)
                })
                .await;
                self.decode_jobs.lock().await.remove(&job);
                result?
            }
            "media.decode_cancel" => {
                let jobs = self.decode_jobs.lock().await;
                if let Some(cancel) = jobs.get(required(&payload, "job_id")?) {
                    cancel.store(true, std::sync::atomic::Ordering::Release);
                }
                Ok(json!({"cancelled":true}))
            }
            "media.retain" => {
                let root = self
                    .media_root
                    .parent()
                    .ok_or_else(|| anyhow::anyhow!("workspace root unavailable"))?
                    .to_owned();
                let original = if let Some(id) = payload["grant_id"].as_str() {
                    Some(std::fs::File::open(
                        self.grants.lock().await.resolve(id, "import_audio")?,
                    )?)
                } else {
                    None
                };
                let relative = payload["decoded_relative_path"].as_str().map(str::to_owned);
                let meeting = required(&payload, "meeting_id")?.to_owned();
                let source = required(&payload, "source_id")?.to_owned();
                let media_root = self.media_root.clone();
                let cap = payload["media_cap_bytes"]
                    .as_u64()
                    .unwrap_or(10 * 1024 * 1024 * 1024);
                ensure!(
                    (1_048_576..=1_099_511_627_776).contains(&cap),
                    "media cap is outside supported bounds"
                );
                tokio::task::spawn_blocking(move || {
                    crate::decode::retain(
                        &root,
                        &media_root,
                        original,
                        relative.as_deref(),
                        meeting,
                        source,
                        cap,
                    )
                })
                .await?
            }
            "native.capture.start" => {
                if let Some(player) = self.playback.lock().await.as_ref() {
                    player.pause()?;
                }
                let engine = self.engine().await?;
                self.capture
                    .lock()
                    .await
                    .start(serde_json::from_value(payload)?, engine)
                    .await
            }
            "native.capture.pause" => self.capture.lock().await.pause(),
            "native.capture.resume" => self.capture.lock().await.resume(
                payload["capture_epoch"]
                    .as_u64()
                    .ok_or_else(|| anyhow::anyhow!("resume epoch required"))?,
            ),
            "native.capture.stop" => self.capture.lock().await.stop(),
            "native.capture.devices" => Ok(json!({"items":self.capture.lock().await.devices()?})),
            "native.capture.source_retry" => self.capture.lock().await.resume(
                payload["capture_epoch"]
                    .as_u64()
                    .ok_or_else(|| anyhow::anyhow!("retry capture epoch required"))?,
            ),
            "native.media.reconcile" => {
                Ok(json!({"items":native_media::reconcile(&self.media_root)?}))
            }
            "native.media.purge" => {
                if let Some(player) = self.playback.lock().await.as_ref() {
                    player.close()?;
                }
                let id = required(&payload, "media_id")?;
                uuid::Uuid::parse_str(id)?;
                let path = native_media::scoped_path(&self.media_root, &format!("{id}.opus"))?;
                std::fs::remove_file(path)?;
                let sidecar = self.media_root.join(format!("{id}.commit"));
                if sidecar.exists() {
                    std::fs::remove_file(sidecar)?;
                }
                Ok(json!({"purged":true}))
            }
            "native.notify" => {
                let title = required(&payload, "title")?;
                let body = required(&payload, "body")?;
                ensure!(
                    title.len() <= 256 && body.len() <= 4096,
                    "notification exceeds bound"
                );
                if let Some(action) = payload.get("action") {
                    ensure!(
                        !action["operation_id"]
                            .as_str()
                            .unwrap_or("")
                            .contains("start"),
                        "notification cannot start recording"
                    );
                }
                (self.events)("native.notification", payload);
                Ok(json!({"delivered":true}))
            }
            _ => bail!("native operation is not allowlisted"),
        }
    }
    pub async fn media_playback(&self, request: Value) -> Result<Value> {
        let op = required(&request, "operation")?;
        let mut guard = self.playback.lock().await;
        if guard.is_none() {
            if op == "state" || op == "close" || op == "pause" {
                return Ok(json!({"playing":false,"position_ms":0,"speed":1.}));
            }
            let events = self.events.clone();
            *guard = Some(native_media::playback::Playback::new(Arc::new(
                move |position, playing| {
                    events(
                        "native.media.playback",
                        json!({"position_ms":position,"playing":playing}),
                    );
                },
            ))?);
        }
        let player = guard.as_ref().unwrap();
        match op {
            "play" => {
                ensure!(
                    !self.capture.lock().await.active,
                    "Replay is paused during capture to prevent feedback"
                );
                let id = required(&request, "media_id")?;
                uuid::Uuid::parse_str(id)?;
                let catalog = native_media::reconcile(&self.media_root)?;
                let selected = catalog
                    .iter()
                    .find(|c| c.media_id == id)
                    .ok_or_else(|| anyhow::anyhow!("Media unavailable or expired"))?;
                let position = request["position_ms"]
                    .as_u64()
                    .unwrap_or_else(|| (selected.start_ns / 1_000_000).saturating_sub(1000));
                let meeting = selected.meeting_id.clone();
                let segments: Vec<_> = catalog
                    .into_iter()
                    .filter(|c| c.meeting_id == meeting)
                    .collect();
                let final_ms = segments
                    .iter()
                    .map(|c| c.start_ns / 1_000_000 + c.frame_count.div_ceil(48))
                    .max()
                    .unwrap_or(position);
                let end = request["end_ms"].as_u64().unwrap_or(final_ms).min(final_ms);
                ensure!(position < end, "Playback interval has no retained timeline");
                let muted: Vec<String> = request
                    .get("muted_sources")
                    .map(|v| serde_json::from_value(v.clone()))
                    .transpose()?
                    .unwrap_or_default();
                let solo = request["solo_source"].as_str().map(str::to_owned);
                player.load(
                    native_media::playback::Plan {
                        root: self.media_root.clone(),
                        segments,
                        position_ms: position,
                        end_ms: end,
                        muted_sources: muted,
                        solo_source: solo,
                    },
                    request["speed"].as_f64().unwrap_or(1.) as f32,
                )?;
            }
            "pause" => player.pause()?,
            "resume" => {
                ensure!(
                    !self.capture.lock().await.active,
                    "Replay disabled during capture"
                );
                player.resume()?
            }
            "seek" => {
                ensure!(
                    !self.capture.lock().await.active,
                    "Replay seek disabled during capture"
                );
                player.seek(
                    request["position_ms"]
                        .as_u64()
                        .ok_or_else(|| anyhow::anyhow!("Position required"))?,
                )?
            }
            "speed" => player.speed(request["speed"].as_f64().unwrap_or(1.) as f32)?,
            "sources" => {
                let muted: Vec<String> = serde_json::from_value(request["muted_sources"].clone())?;
                ensure!(
                    muted.len() <= 2
                        && muted
                            .iter()
                            .all(|s| ["mic", "system"].contains(&s.as_str())),
                    "Invalid source mute selection"
                );
                player.sources(muted, request["solo_source"].as_str().map(str::to_owned))?
            }
            "close" => player.close()?,
            "state" => {}
            _ => bail!("Unsupported playback operation"),
        }
        player.check()?;
        let value = json!({"playing":player.playing.load(std::sync::atomic::Ordering::Acquire),"position_ms":player.position.load(std::sync::atomic::Ordering::Acquire),"speed":f32::from_bits(player.speed_bits.load(std::sync::atomic::Ordering::Acquire)as u32)});
        (self.events)("native.media.playback", value.clone());
        Ok(value)
    }
}
fn required<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("{key} is required"))
}
async fn rpc(
    State(broker): State<Broker>,
    headers: HeaderMap,
    Json(command): Json<crate::ApplicationCommand>,
) -> Result<Json<Value>, (StatusCode, Json<crate::NativeError>)> {
    let authorization = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let supplied = authorization.strip_prefix("Bearer ").unwrap_or("");
    let expected = broker.token.as_str();
    let host = headers
        .get("host")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if supplied.as_bytes().ct_eq(expected.as_bytes()).unwrap_u8() != 1
        || headers.contains_key("origin")
        || !host.starts_with("127.0.0.1:")
    {
        return Err((
            StatusCode::UNAUTHORIZED,
            Json(anyhow::anyhow!("native broker authentication denied").into()),
        ));
    }
    broker
        .request(&command.operation_id, Value::Object(command.payload))
        .await
        .map(Json)
        .map_err(|e| (StatusCode::BAD_REQUEST, Json(e.into())))
}
