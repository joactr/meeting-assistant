//! Debug source checks do not require previously packaged artifacts. Debug runtime
//! uses the documented explicit developer engine/resource paths. Release builds
//! always retain the canonical resource mapping and require the real engine.
fn main() {
    let desktop = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let manifest = desktop.join("../../../release.toml");
    println!("cargo:rerun-if-changed={}", manifest.display());
    let text = std::fs::read_to_string(&manifest).expect("root release.toml");
    let release: toml::Value = toml::from_str(&text).expect("valid release identity");
    let version = release
        .get("version")
        .and_then(toml::Value::as_str)
        .expect("release version");
    let repository = release
        .get("publisher")
        .and_then(|p| p.get("repository"))
        .and_then(toml::Value::as_str)
        .unwrap_or("")
        .trim()
        .trim_end_matches('/')
        .trim_start_matches("https://github.com/");
    let mut parts = repository.split('/');
    let owner = parts.next().unwrap_or("");
    let repo = parts.next().unwrap_or("");
    assert!(
        parts.next().is_none(),
        "publisher.repository must be owner/repo or its public GitHub URL"
    );
    println!("cargo:rustc-env=MA_GITHUB_OWNER={owner}");
    println!("cargo:rustc-env=MA_GITHUB_REPO={repo}");
    let key = release
        .get("updates")
        .and_then(|p| p.get("public_key"))
        .and_then(toml::Value::as_str)
        .unwrap_or("");
    println!("cargo:rustc-env=MA_UPDATER_KEY={key}");
    println!("cargo:rustc-env=MA_BUILD_ID={version}");
    println!("cargo:rerun-if-env-changed=WINDOWS_CERTIFICATE_THUMBPRINT");
    configure_resources(desktop);
    tauri_build::build();
}

fn configure_resources(desktop: &std::path::Path) {
    let target = std::env::var("CARGO_CFG_TARGET_OS").expect("Cargo target operating system");
    let engine_dir = desktop.join("../../../dist/meeting-assistant-engine");
    let executable = engine_dir.join(if target == "windows" {
        "meeting-assistant-engine.exe"
    } else {
        "meeting-assistant-engine"
    });
    println!("cargo:rerun-if-changed={}", engine_dir.display());
    let mut overrides: serde_json::Value = std::env::var("TAURI_CONFIG")
        .map(|value| serde_json::from_str(&value).expect("valid inherited TAURI_CONFIG"))
        .unwrap_or_else(|_| serde_json::json!({}));
    if std::env::var("PROFILE").as_deref() == Ok("debug") {
        let bundle = overrides
            .as_object_mut()
            .expect("TAURI_CONFIG must be an object")
            .entry("bundle")
            .or_insert_with(|| serde_json::json!({}));
        // An array replaces the canonical map under JSON Merge Patch. An empty map
        // would merge with it and still require absent artifacts on a clean checkout.
        bundle
            .as_object_mut()
            .expect("TAURI_CONFIG.bundle must be an object")
            .insert("resources".into(), serde_json::json!([]));
        let value = serde_json::to_string(&overrides).expect("serializable debug configuration");
        // This build script is single-threaded; mutate only its own process before
        // invoking tauri-build, then forward the same override to generate_context!.
        std::env::set_var("TAURI_CONFIG", &value);
        println!("cargo:rustc-env=TAURI_CONFIG={value}");
    } else {
        assert!(executable.is_file(),"Release packaging requires the real bundled engine at {}. Build the frozen engine first; a source-only debug check does not create a substitute.",executable.display());
        if let Some(resources) = overrides
            .get("bundle")
            .and_then(|bundle| bundle.get("resources"))
        {
            let canonical: serde_json::Value = serde_json::from_slice(
                &std::fs::read(desktop.join("tauri.conf.json"))
                    .expect("canonical Tauri configuration"),
            )
            .expect("valid canonical Tauri configuration");
            assert_eq!(resources,&canonical["bundle"]["resources"],"Release overrides must retain the canonical bundled engine, DSP and native resources");
        }
    }
}
