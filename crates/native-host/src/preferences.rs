//! One native preference file. Apply OS changes before atomic persistence.
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::{fs::OpenOptions, io::Write, path::PathBuf};
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Window {
    pub width: f64,
    pub height: f64,
    pub x: Option<f64>,
    pub y: Option<f64>,
}
impl Default for Window {
    fn default() -> Self {
        Self {
            width: 1280.,
            height: 800.,
            x: None,
            y: None,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Updates {
    pub consent: bool,
    pub channel: String,
    pub privacy_mode: bool,
}
impl Default for Updates {
    fn default() -> Self {
        Self {
            consent: false,
            channel: "stable".into(),
            privacy_mode: false,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Preferences {
    pub schema_version: u32,
    pub tray_enabled: bool,
    pub always_on_top: bool,
    pub theme: String,
    pub sidebar_collapsed: bool,
    pub selected_tabs: serde_json::Value,
    pub window: Window,
    pub updates: Updates,
}
impl Default for Preferences {
    fn default() -> Self {
        Self {
            schema_version: 1,
            tray_enabled: false,
            always_on_top: true,
            theme: "system".into(),
            sidebar_collapsed: false,
            selected_tabs: serde_json::json!({}),
            window: Window::default(),
            updates: Updates::default(),
        }
    }
}
pub struct Store {
    path: PathBuf,
    pub value: Preferences,
}
impl Store {
    pub fn open(path: PathBuf) -> Result<Self> {
        let value = if path.exists() {
            serde_json::from_slice(&std::fs::read(&path)?)?
        } else {
            Preferences::default()
        };
        Ok(Self { path, value })
    }
    pub fn persist(&mut self, value: Preferences) -> Result<()> {
        ensure!(
            value.schema_version == 1
                && ["system", "light", "dark"].contains(&value.theme.as_str())
                && ["stable", "beta"].contains(&value.updates.channel.as_str()),
            "unsupported preferences"
        );
        ensure!(
            value.window.width >= 640. && value.window.height >= 400.,
            "window below accessible minimum"
        );
        let part = self.path.with_extension("json.part");
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&part)?;
        file.write_all(&serde_json::to_vec_pretty(&value)?)?;
        file.sync_all()?;
        drop(file);
        std::fs::rename(part, &self.path)?;
        if let Some(parent) = self.path.parent() {
            native_media::sync_directory(parent)?;
        }
        self.value = value;
        Ok(())
    }
}
