#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod secure_prompt;
use anyhow::{Context, Result};
use native_host::{
    broker::Broker,
    preferences::{Preferences, Store},
    supervisor::{Engine, Launch},
    ApplicationCommand, NativeError,
};
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_updater::UpdaterExt;
use tokio::sync::Mutex;
macro_rules! ensure{($condition:expr,$($message:tt)*)=>{if !$condition{return Err(anyhow::anyhow!($($message)*).into());}};}
struct Host {
    broker: Broker,
    prefs: Mutex<Store>,
    pending_update: Mutex<Option<tauri_plugin_updater::Update>>,
    reservation: Mutex<Option<Value>>,
    safe_exit: AtomicBool,
    last_update: Mutex<Option<Instant>>,
    geometry_revision: AtomicU64,
}
#[tauri::command]
async fn engine_request(
    host: tauri::State<'_, Host>,
    command: ApplicationCommand,
) -> Result<Value, NativeError> {
    host.broker
        .engine()
        .await?
        .request(&command)
        .await
        .map_err(Into::into)
}
#[tauri::command]
async fn audio_devices(host: tauri::State<'_, Host>) -> Result<Value, NativeError> {
    Ok(json!({"items":host.broker.capture.lock().await.devices()?}))
}
#[tauri::command]
async fn audio_test_start(
    host: tauri::State<'_, Host>,
    device_key: String,
) -> Result<Value, NativeError> {
    host.broker
        .capture
        .lock()
        .await
        .test_start(&device_key)
        .map_err(Into::into)
}
#[tauri::command]
async fn audio_calibrate(
    host: tauri::State<'_, Host>,
    request: Value,
) -> Result<Value, NativeError> {
    let mic = request["mic_key"]
        .as_str()
        .context("explicit mic_key required")?
        .to_owned();
    let system = request["system_key"]
        .as_str()
        .context("explicit system_key required")?
        .to_owned();
    host.broker
        .capture
        .lock()
        .await
        .calibrate(mic, system, request["seconds"].as_u64().unwrap_or(5))
        .await
        .map_err(Into::into)
}
#[tauri::command]
async fn audio_test_stop(host: tauri::State<'_, Host>) -> Result<Value, NativeError> {
    host.broker
        .capture
        .lock()
        .await
        .test_stop()
        .map_err(Into::into)
}
#[tauri::command]
async fn native_preferences_get(host: tauri::State<'_, Host>) -> Result<Value, NativeError> {
    Ok(serde_json::to_value(&host.prefs.lock().await.value).map_err(anyhow::Error::from)?)
}
fn apply_preferences(app: &tauri::AppHandle, prefs: &Preferences) -> Result<()> {
    let window = app
        .get_webview_window("main")
        .context("main window missing")?;
    window.set_always_on_top(prefs.always_on_top)?;
    window.set_size(tauri::LogicalSize::new(
        prefs.window.width,
        prefs.window.height,
    ))?;
    // Position is stored in physical desktop pixels: logical coordinates are per-monitor on
    // mixed-DPI setups and land off-screen when the window starts on a different monitor.
    if let (Some(x), Some(y)) = (prefs.window.x, prefs.window.y) {
        let (x, y) = (x as i32, y as i32);
        // Title bar must be on a connected monitor, else keep the default centred placement.
        let visible = window.available_monitors()?.iter().any(|monitor| {
            let (origin, size) = (monitor.position(), monitor.size());
            let (grab_x, grab_y) = (x + 100, y + 16);
            grab_x >= origin.x
                && grab_y >= origin.y
                && grab_x < origin.x + size.width as i32
                && grab_y < origin.y + size.height as i32
        });
        if visible {
            window.set_position(tauri::PhysicalPosition::new(x, y))?;
        }
    }
    window.set_theme(match prefs.theme.as_str() {
        "light" => Some(tauri::Theme::Light),
        "dark" => Some(tauri::Theme::Dark),
        _ => None,
    })?;
    if let Some(tray) = app.tray_by_id("status") {
        tray.set_visible(prefs.tray_enabled)?;
    }
    Ok(())
}
#[tauri::command]
async fn native_preferences_set(
    app: tauri::AppHandle,
    host: tauri::State<'_, Host>,
    preferences: Preferences,
) -> Result<Value, NativeError> {
    let mut store = host.prefs.lock().await;
    let old = store.value.clone();
    if let Err(error) =
        apply_preferences(&app, &preferences).and_then(|_| store.persist(preferences.clone()))
    {
        let _ = apply_preferences(&app, &old);
        return Err(error.into());
    }
    Ok(serde_json::to_value(preferences).map_err(anyhow::Error::from)?)
}
#[tauri::command]
async fn media_playback(
    host: tauri::State<'_, Host>,
    request: Value,
) -> Result<Value, NativeError> {
    host.broker
        .media_playback(request)
        .await
        .map_err(Into::into)
}
#[tauri::command]
async fn file_grant(host: tauri::State<'_, Host>, purpose: String) -> Result<Value, NativeError> {
    let dialog = rfd::AsyncFileDialog::new().set_title("Meeting Assistant — explicit file access");
    let file = match purpose.as_str() {
        "import_audio" => {
            dialog
                .add_filter(
                    "Audio",
                    &["wav", "flac", "mp3", "m4a", "ogg", "opus", "aac", "aiff"],
                )
                .pick_file()
                .await
        }
        "import_model" => {
            dialog
                .add_filter("Model archive", &["zip", "tar", "gz", "tgz", "xz"])
                .pick_file()
                .await
        }
        "restore" | "legacy_import" => dialog.pick_file().await,
        "backup" | "export" => dialog.save_file().await,
        _ => return Err(anyhow::anyhow!("Invalid file purpose").into()),
    };
    let Some(file) = file else {
        return Ok(json!({"cancelled":true}));
    };
    host.broker
        .grants
        .lock()
        .await
        .add(file.path().to_path_buf(), &purpose)
        .map_err(Into::into)
}
#[tauri::command]
async fn oauth_begin(host: tauri::State<'_, Host>, request: Value) -> Result<Value, NativeError> {
    if host.prefs.lock().await.value.updates.privacy_mode {
        return Err(anyhow::anyhow!("Privacy mode blocks OAuth network access").into());
    }
    host.broker
        .request("oauth.begin", request)
        .await
        .map_err(Into::into)
}
#[tauri::command]
async fn oauth_cancel(host: tauri::State<'_, Host>) -> Result<Value, NativeError> {
    host.broker.oauth.cancel(None).await.map_err(Into::into)
}
#[tauri::command]
async fn vault_prompt(
    host: tauri::State<'_, Host>,
    secret_ref: String,
    label: String,
) -> Result<Value, NativeError> {
    let value = tauri::async_runtime::spawn_blocking(move || secure_prompt::prompt(&label))
        .await
        .map_err(anyhow::Error::from)??;
    if let Some(value) = value {
        host.broker.vault.store(&secret_ref, value.as_str())?;
        Ok(json!({"secret_ref":secret_ref,"stored":true}))
    } else {
        Ok(json!({"cancelled":true}))
    }
}
#[tauri::command]
async fn vault_delete(
    host: tauri::State<'_, Host>,
    secret_ref: String,
) -> Result<Value, NativeError> {
    host.broker.vault.delete(&secret_ref)?;
    Ok(json!({"deleted":true}))
}
#[tauri::command]
async fn vault_status(
    host: tauri::State<'_, Host>,
    secret_ref: String,
) -> Result<Value, NativeError> {
    match host.broker.vault.resolve(&secret_ref) {
        Ok(_) => Ok(json!({"configured":true})),
        Err(_) => Ok(json!({"configured":false})),
    }
}
#[tauri::command]
fn external_open(url: String) -> Result<Value, NativeError> {
    let parsed = native_host::validate_external_https(&url)?;
    let host = parsed.host_str().context("URL host missing")?;
    ensure!(
        !host.eq_ignore_ascii_case("localhost")
            && !host.ends_with(".localhost")
            && host.contains('.'),
        "local/private browser link rejected"
    );
    webbrowser::open(parsed.as_str()).map_err(anyhow::Error::from)?;
    Ok(json!({"opened":true}))
}
#[tauri::command]
fn clipboard_share(app: tauri::AppHandle, request: Value) -> Result<Value, NativeError> {
    let text = request["text"].as_str().context("share text required")?;
    ensure!(
        text.len() <= 4 * 1024 * 1024,
        "clipboard text exceeds bound"
    );
    app.clipboard()
        .write_text(text)
        .map_err(anyhow::Error::from)?;
    if request["operation"] == "mailto" {
        let recipients = request["recipients"]
            .as_array()
            .map(|a| a.iter().filter_map(Value::as_str).collect::<Vec<_>>())
            .unwrap_or_default();
        ensure!(
            recipients
                .iter()
                .all(|r| !r.contains(['\r', '\n', '?', '#', ':', '/']) && r.contains('@')),
            "invalid mail recipients"
        );
        let subject = request["subject"].as_str().unwrap_or("Meeting minutes");
        ensure!(!subject.contains(['\r', '\n']), "invalid mail subject");
        let mut body = text.to_owned();
        let mut draft;
        loop {
            let query = url::form_urlencoded::Serializer::new(String::new())
                .append_pair("subject", subject)
                .append_pair("body", &body)
                .finish();
            draft = format!("mailto:{}?{}", recipients.join(","), query);
            if draft.len() <= 1800 || body.is_empty() {
                break;
            }
            body.pop();
        }
        ensure!(
            draft.len() <= 1800,
            "recipient/subject URL exceeds conservative mail limit"
        );
        webbrowser::open(&draft).map_err(anyhow::Error::from)?;
        Ok(
            json!({"copied":true,"opened":true,"shortened":body!=text,"warning":"Full minutes are on the clipboard; mail clients have different draft limits."}),
        )
    } else {
        ensure!(
            request["operation"] == "copy",
            "invalid clipboard operation"
        );
        Ok(json!({"copied":true}))
    }
}
async fn reserve(host: &Host) -> Result<Value> {
    let engine = host.broker.engine().await?;
    let result = engine
        .internal(
            "shutdown.prepare",
            &json!({"idempotency_key":uuid::Uuid::new_v4().to_string()}),
        )
        .await?;
    *host.reservation.lock().await = Some(result.clone());
    Ok(result)
}
async fn commit_shutdown(host: &Host) -> Result<Value> {
    let reservation = host
        .reservation
        .lock()
        .await
        .clone()
        .context("shutdown reservation required")?;
    ensure!(
        reservation["blockers"]
            .as_array()
            .map(|a| a.is_empty())
            .unwrap_or(false),
        "shutdown blocked: stop-and-preserve active capture before continuing"
    );
    host.broker.capture.lock().await.test_stop()?;
    host.broker.capture.lock().await.stop()?;
    if let Some(player) = host.broker.playback.lock().await.as_ref() {
        player.close()?;
    }
    let engine = host.broker.engine().await?;
    let result=engine.internal("shutdown.commit",&json!({"reservation_id":reservation["reservation_id"],"idempotency_key":uuid::Uuid::new_v4().to_string()})).await?;
    ensure!(
        result["safe_to_exit"].as_bool() == Some(true),
        "engine preservation acknowledgement missing"
    );
    engine.verify_exit().await?;
    host.safe_exit.store(true, Ordering::Release);
    Ok(result)
}
#[tauri::command]
async fn prepare_exit(
    app: tauri::AppHandle,
    host: tauri::State<'_, Host>,
    cancel: bool,
) -> Result<Value, NativeError> {
    if cancel {
        if let Some(reservation) = host.reservation.lock().await.take() {
            return host
                .broker
                .engine()
                .await?
                .internal(
                    "shutdown.cancel",
                    &json!({"reservation_id":reservation["reservation_id"]}),
                )
                .await
                .map_err(Into::into);
        }
        return Ok(json!({"cancelled":true}));
    }
    let value = reserve(&host).await?;
    if value["blockers"]
        .as_array()
        .map(|a| a.is_empty())
        .unwrap_or(false)
    {
        let result = commit_shutdown(&host).await?;
        app.exit(0);
        return Ok(result);
    }
    Ok(value)
}
fn update_endpoint() -> Result<url::Url> {
    let owner = env!("MA_GITHUB_OWNER");
    let repo = env!("MA_GITHUB_REPO");
    ensure!(!owner.is_empty()&&!repo.is_empty()&&!env!("MA_UPDATER_KEY").is_empty(),"Updates disabled: configure a real public GitHub repository and updater public-key contents in release.toml");
    ensure!(
        owner.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
            && repo
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c)),
        "invalid publisher repository"
    );
    // GitHub's "latest" release (pre-releases excluded) carries the signed latest.json.
    Ok(url::Url::parse(&format!(
        "https://github.com/{owner}/{repo}/releases/latest/download/latest.json"
    ))?)
}
#[tauri::command]
async fn update_check(
    app: tauri::AppHandle,
    host: tauri::State<'_, Host>,
) -> Result<Value, NativeError> {
    let prefs = host.prefs.lock().await.value.clone();
    ensure!(
        !prefs.updates.privacy_mode,
        "Privacy mode blocks update checks"
    );
    ensure!(
        prefs.updates.consent,
        "Update checks require separate onboarding consent"
    );
    let endpoint = update_endpoint()?;
    let updater = app
        .updater_builder()
        .pubkey(env!("MA_UPDATER_KEY"))
        .endpoints(vec![endpoint])
        .map_err(anyhow::Error::from)?
        .build()
        .map_err(anyhow::Error::from)?;
    let update = updater.check().await.map_err(anyhow::Error::from)?;
    *host.last_update.lock().await = Some(Instant::now());
    let value = if let Some(update) = &update {
        ensure!(
            update.download_url.scheme() == "https"
                && update.download_url.host_str() == Some("github.com")
                && update.download_url.path().starts_with(&format!(
                    "/{}/{}/releases/download/",
                    env!("MA_GITHUB_OWNER"),
                    env!("MA_GITHUB_REPO")
                )),
            "updater artifact is outside public configured GitHub releases"
        );
        json!({"state":"available","version":update.version,"current_version":update.current_version,"notes":update.body})
    } else {
        json!({"state":"current"})
    };
    *host.pending_update.lock().await = update;
    app.emit(
        "engine-event",
        json!({"protocol_version":1,"type":"native.update","data":value}),
    )
    .map_err(anyhow::Error::from)?;
    Ok(value)
}
#[tauri::command]
async fn update_install(
    app: tauri::AppHandle,
    host: tauri::State<'_, Host>,
) -> Result<Value, NativeError> {
    ensure!(
        !host.prefs.lock().await.value.updates.privacy_mode,
        "Privacy mode blocks update downloads"
    );
    let update = host
        .pending_update
        .lock()
        .await
        .clone()
        .context("Check and review an update before installing")?;
    let emit = app.clone();
    let bytes=update.download(move|chunk,total|{let _=emit.emit("engine-event",json!({"protocol_version":1,"type":"native.update.progress","data":{"chunk_bytes":chunk,"total_bytes":total}}));},||{}).await.map_err(anyhow::Error::from)?;
    let reservation = reserve(&host).await?;
    if !reservation["blockers"]
        .as_array()
        .map(|a| a.is_empty())
        .unwrap_or(false)
    {
        return Ok(json!({"state":"blocked","reservation":reservation}));
    }
    commit_shutdown(&host).await?;
    update.install(bytes).map_err(anyhow::Error::from)?;
    Ok(json!({"state":"installing"}))
}
fn tray_icon() -> tauri::image::Image<'static> {
    let mut pixels = vec![0u8; 32 * 32 * 4];
    for x in 3..29 {
        let height = 3 + ((x * 7) % 11);
        for y in 16 - height..16 + height {
            let offset = (y * 32 + x) * 4;
            pixels[offset..offset + 4].copy_from_slice(&[52, 140, 220, 255]);
        }
    }
    tauri::image::Image::new_owned(pixels, 32, 32)
}
fn install_tray(app: &tauri::App) -> Result<()> {
    use tauri::menu::{Menu, MenuItem};
    let show = MenuItem::with_id(app, "show", "Show Meeting Assistant", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit — preserve meeting", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;
    tauri::tray::TrayIconBuilder::with_id("status").icon(tray_icon()).tooltip("Meeting Assistant — not recording").menu(&menu).on_menu_event(|app,event|{if event.id.as_ref()=="show"{if let Some(window)=app.get_webview_window("main"){let _=window.show();let _=window.set_focus();}}else if event.id.as_ref()=="quit"{let app=app.clone();tauri::async_runtime::spawn(async move{let host=app.state::<Host>();match reserve(&host).await{Ok(reservation)=>{if reservation["blockers"].as_array().map(|a|a.is_empty()).unwrap_or(false){if commit_shutdown(&host).await.is_ok(){app.exit(0)}}else{if let Some(window)=app.get_webview_window("main"){let _=window.show();}let _=app.emit("engine-event",json!({"protocol_version":1,"type":"native.shutdown.blocked","data":reservation}));}},Err(e)=>{let _=app.emit("engine-event",json!({"protocol_version":1,"type":"native.error","data":{"message":e.to_string()}}));}}});}}).build(app)?;
    Ok(())
}
fn configure(app: &mut tauri::App) -> Result<()> {
    let data =
        developer_path("MEETING_ASSISTANT_WORKSPACE")?.unwrap_or(app.path().app_local_data_dir()?);
    native_host::private_directory(&data)?;
    let media = data.join("media");
    native_host::private_directory(&media)?;
    let resources =
        developer_path("MEETING_ASSISTANT_RESOURCES")?.unwrap_or(app.path().resource_dir()?);
    ensure!(resources.is_dir(), "native resources directory is missing");
    let prefs = Store::open(data.join("native-preferences.json"))?;
    let apphandle = app.handle().clone();
    let ephemeral = Arc::new(AtomicU64::new(0));
    let events: native_host::Events = Arc::new(move |kind, data| {
        let seq = ephemeral.fetch_add(1, Ordering::Relaxed) + 1;
        if kind == "native.notification" {
            let _ = apphandle
                .notification()
                .builder()
                .title(data["title"].as_str().unwrap_or("Meeting Assistant"))
                .body(data["body"].as_str().unwrap_or(""))
                .show();
        }
        if let Some(tray) = apphandle.tray_by_id("status") {
            if kind.starts_with("native.capture") {
                let _ = tray.set_tooltip(Some(format!("Meeting Assistant — {kind}")));
            }
        }
        let _=apphandle.emit("engine-event",json!({"protocol_version":1,"seq":0,"stream_seq":seq,"ephemeral":true,"event_id":uuid::Uuid::new_v4().to_string(),"type":kind,"meeting_id":data["meeting_id"],"entity_id":null,"revision":0,"occurred_at_ms":std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis()as u64,"data":data}));
    });
    let token = Engine::fresh_token();
    let broker = Broker::new(
        native_host::capture::Capture::new(
            native_host::backend(&resources),
            media.clone(),
            resources.clone(),
            events.clone(),
        ),
        native_host::vault::Vault::new(app.config().identifier.clone()),
        media,
        zeroize::Zeroizing::new(token.to_string()),
        events.clone(),
    )?;
    app.manage(Host {
        broker: broker.clone(),
        prefs: Mutex::new(prefs),
        pending_update: Mutex::new(None),
        reservation: Mutex::new(None),
        safe_exit: AtomicBool::new(false),
        last_update: Mutex::new(None),
        geometry_revision: AtomicU64::new(0),
    });
    install_tray(app)?;
    let initial = tauri::async_runtime::block_on(async {
        app.state::<Host>().prefs.lock().await.value.clone()
    });
    apply_preferences(app.handle(), &initial)?;
    spawn_update_checks(app.handle().clone());
    let apphandle = app.handle().clone();
    tauri::async_runtime::spawn(async move {
        let result = async {
            let host_port = broker.listen().await?;
            let (executable, arguments) = engine_executable(&resources)?;
            let launch = Launch { executable, arguments, workspace: data.join("workspace.sqlite3"), build_id: env!("MA_BUILD_ID").into(), host_port };
            let engine = Engine::launch(&launch, zeroize::Zeroizing::new(token.to_string())).await?;
            *broker.engine.write().await = Some(engine);
            (events)("native.engine.ready", json!({"build_id": launch.build_id}));
            relay_events(broker.clone(), apphandle.clone());
            let mut last = Instant::now();
            let mut restarted = false;
            loop {
                tokio::time::sleep(Duration::from_secs(2)).await;
                if apphandle.state::<Host>().safe_exit.load(Ordering::Acquire) { break; }
                let engine = broker.engine().await?;
                if engine.heartbeat().await { last = Instant::now(); continue; }
                if last.elapsed() < Duration::from_secs(10) { continue; }
                let _ = broker.capture.lock().await.stop();
                (events)("native.engine.interrupted", json!({"message":"Engine heartbeat lost: capture stopped; preserved data can be recovered","automatic_restart_used":restarted}));
                let _ = engine.terminate().await;
                if restarted { break; }
                restarted = true;
                let replacement = Engine::launch(&launch, zeroize::Zeroizing::new(token.to_string())).await?;
                *broker.engine.write().await = Some(replacement);
                last = Instant::now();
            }
            Ok::<_, anyhow::Error>(())
        }.await;
        if let Err(error) = result {
            (events)(
                "native.engine.error",
                json!({"message":error.to_string(),"retryable":true}),
            );
        }
    });
    Ok(())
}
fn developer_path(name: &str) -> Result<Option<PathBuf>> {
    let Some(raw) = std::env::var_os(name) else {
        return Ok(None);
    };
    ensure!(
        cfg!(debug_assertions) || env!("MA_GITHUB_OWNER").is_empty(),
        "developer path overrides are disabled in publisher-configured release builds"
    );
    let path = PathBuf::from(raw);
    ensure!(
        path.is_absolute(),
        "{name} must be an absolute developer path"
    );
    Ok(Some(path))
}
fn engine_executable(resources: &std::path::Path) -> Result<(PathBuf, Vec<String>)> {
    if let Some(path) = developer_path("MEETING_ASSISTANT_ENGINE")? {
        return Ok((
            path,
            if std::env::var_os("MEETING_ASSISTANT_ENGINE_PYTHON").is_some() {
                vec!["-m".into(), "meeting_assistant".into()]
            } else {
                vec![]
            },
        ));
    }
    Ok((
        resources.join("engine").join(if cfg!(windows) {
            "meeting-assistant-engine.exe"
        } else {
            "meeting-assistant-engine"
        }),
        vec![],
    ))
}
fn relay_events(broker: Broker, app: tauri::AppHandle) {
    use futures_util::StreamExt;
    use tokio_tungstenite::tungstenite::{client::IntoClientRequest, http::HeaderValue, Message};
    tauri::async_runtime::spawn(async move {
        let mut after = 0u64;
        loop {
            if app.state::<Host>().safe_exit.load(Ordering::Acquire) {
                break;
            }
            let Ok(engine) = broker.engine().await else {
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            };
            let uri = format!(
                "ws://127.0.0.1:{}/api/v1/events?after_seq={after}",
                engine.ready.http_port
            );
            let Ok(mut request) = uri.into_client_request() else {
                break;
            };
            let Ok(auth) = HeaderValue::from_str(&format!("Bearer {}", engine.token())) else {
                break;
            };
            request.headers_mut().insert("Authorization", auth);
            if let Ok((mut socket, _)) = tokio_tungstenite::connect_async(request).await {
                while let Some(Ok(message)) = socket.next().await {
                    if let Message::Text(text) = message {
                        if text.len() > 1_048_576 {
                            break;
                        }
                        let Ok(event) = serde_json::from_str::<Value>(&text) else {
                            break;
                        };
                        if event["protocol_version"] != 1 || !event["type"].is_string() {
                            break;
                        }
                        if let Some(seq) = event["seq"].as_u64() {
                            if seq > 0 {
                                after = after.max(seq);
                            }
                        }
                        let _ = app.emit("engine-event", event);
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    });
}
async fn automatic_update_check(app: tauri::AppHandle, focus: bool) {
    if env!("MA_UPDATER_KEY").is_empty() || env!("MA_GITHUB_OWNER").is_empty() {
        return;
    }
    let host = app.state::<Host>();
    let allowed = {
        let prefs = host.prefs.lock().await;
        prefs.value.updates.consent && !prefs.value.updates.privacy_mode
    };
    if !allowed || host.safe_exit.load(Ordering::Acquire) {
        return;
    }
    let due = {
        let mut last = host.last_update.lock().await;
        let interval = if focus {
            Duration::from_secs(3600)
        } else {
            Duration::from_secs(86400)
        };
        if last
            .map(|value| value.elapsed() < interval)
            .unwrap_or(false)
        {
            false
        } else {
            *last = Some(Instant::now());
            true
        }
    };
    if due {
        if let Err(error) = update_check(app.clone(), app.state::<Host>()).await {
            let _ = app.emit(
                "engine-event",
                json!({"protocol_version":1,"type":"native.update.error","data":error}),
            );
        }
    }
}
fn spawn_update_checks(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        // Give the webview time to subscribe before the first check emits its prompt event.
        tokio::time::sleep(Duration::from_secs(10)).await;
        loop {
            if app.state::<Host>().safe_exit.load(Ordering::Acquire) {
                break;
            }
            automatic_update_check(app.clone(), false).await;
            tokio::time::sleep(Duration::from_secs(60)).await;
        }
    });
}
fn persist_geometry(app: tauri::AppHandle) {
    let revision = app
        .state::<Host>()
        .geometry_revision
        .fetch_add(1, Ordering::AcqRel)
        + 1;
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(250)).await;
        let host = app.state::<Host>();
        if host.geometry_revision.load(Ordering::Acquire) != revision
            || host.safe_exit.load(Ordering::Acquire)
        {
            return;
        }
        let Some(window) = app.get_webview_window("main") else {
            return;
        };
        if window.is_minimized().unwrap_or(true) || window.is_maximized().unwrap_or(true) {
            return;
        }
        let Ok(scale) = window.scale_factor() else {
            return;
        };
        let (Ok(size), Ok(position)) = (window.inner_size(), window.outer_position()) else {
            return;
        };
        let size = size.to_logical::<f64>(scale);
        // Physical on purpose; see apply_preferences.
        let position = position.cast::<f64>();
        if size.width < 640. || size.height < 400. {
            return;
        }
        let mut store = host.prefs.lock().await;
        let mut value = store.value.clone();
        value.window = native_host::preferences::Window {
            width: size.width,
            height: size.height,
            x: Some(position.x),
            y: Some(position.y),
        };
        if let Err(error) = store.persist(value) {
            let _=app.emit("engine-event",json!({"protocol_version":1,"type":"native.preferences.error","data":{"message":error.to_string()}}));
        }
    });
}
/// Window close / quit entry point. The UI owns the sequence (flush drafts, reserve preservation,
/// exit or show blockers via `prepare_exit` / `force_exit`), so the native side only requests it.
/// A hidden window is shown first so the request is never invisible.
async fn request_quit(app: tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
    let _ = app.emit(
        "engine-event",
        json!({"protocol_version":1,"type":"native.shutdown.requested","data":{}}),
    );
}
/// Explicit user override when the engine cannot confirm preservation (for example it is no longer running).
/// Capture and playback are still stopped first; unsaved capture audio may be lost.
#[tauri::command]
async fn force_exit(
    app: tauri::AppHandle,
    host: tauri::State<'_, Host>,
) -> Result<Value, NativeError> {
    if let Some(reservation) = host.reservation.lock().await.take() {
        if let Ok(engine) = host.broker.engine().await {
            let _ = engine
                .internal(
                    "shutdown.cancel",
                    &json!({"reservation_id":reservation["reservation_id"]}),
                )
                .await;
        }
    }
    let _ = host.broker.capture.lock().await.test_stop();
    let _ = host.broker.capture.lock().await.stop();
    host.safe_exit.store(true, Ordering::Release);
    app.exit(0);
    Ok(json!({"exiting":true}))
}
fn main() {
    let args: Vec<String> = std::env::args().collect();
    #[cfg(unix)]
    if args.get(1).map(String::as_str) == Some("--engine-supervisor") {
        if let Err(error) = native_host::supervisor::unix_watchdog() {
            eprintln!("{error}");
            std::process::exit(1)
        }
        return;
    }
    if args.get(1).map(String::as_str) == Some("--native-acceptance") {
        let result = native_host::acceptance::run(&args[2..]);
        match result {
            Ok(value) => {
                println!("{value}");
                return;
            }
            Err(e) => {
                eprintln!("{e}");
                std::process::exit(1)
            }
        }
    }
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .invoke_handler(tauri::generate_handler![
            engine_request,
            audio_devices,
            audio_test_start,
            audio_test_stop,
            audio_calibrate,
            native_preferences_get,
            native_preferences_set,
            media_playback,
            file_grant,
            oauth_begin,
            oauth_cancel,
            vault_prompt,
            vault_delete,
            vault_status,
            external_open,
            clipboard_share,
            prepare_exit,
            force_exit,
            update_check,
            update_install
        ])
        .setup(|app| configure(app).map_err(Into::into))
        .on_window_event(|window, event| {
            let app = window.app_handle();
            if matches!(
                event,
                tauri::WindowEvent::Moved(_)
                    | tauri::WindowEvent::Resized(_)
                    | tauri::WindowEvent::ScaleFactorChanged { .. }
            ) {
                persist_geometry(app.clone());
            }
            if matches!(event, tauri::WindowEvent::Focused(true)) {
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    automatic_update_check(app, true).await;
                });
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let tray = app.state::<Host>().prefs.lock().await.value.tray_enabled;
                    if tray {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.hide();
                        }
                    } else {
                        request_quit(app).await;
                    }
                });
            }
        })
        .build(tauri::generate_context!())
        .expect("native application setup failed");
    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !app.state::<Host>().safe_exit.load(Ordering::Acquire) {
                api.prevent_exit();
                let app = app.clone();
                tauri::async_runtime::spawn(request_quit(app));
            }
        }
    });
}
