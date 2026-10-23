//! Experimental, untested PipeWire/WirePlumber native node capture.
use anyhow::{ensure, Result};
use audio_core::*;
use std::{path::PathBuf, process::Command, sync::Arc};
pub struct PipeWireBackend {
    helper: PathBuf,
}
impl PipeWireBackend {
    pub fn new(helper: PathBuf) -> Self {
        Self { helper }
    }
}
impl CaptureBackend for PipeWireBackend {
    fn devices(&self) -> Result<Vec<Device>> {
        let out = Command::new(&self.helper).arg("devices").output()?;
        ensure!(out.status.success(),"PipeWire unavailable: start PipeWire and WirePlumber; select a microphone or sink monitor explicitly");
        Ok(serde_json::from_slice(&out.stdout)?)
    }
    fn start(
        &self,
        key: &str,
        kind: SourceKind,
        ring: Arc<Ring>,
    ) -> Result<Box<dyn CaptureStream>> {
        audio_core::helper::start(&self.helper, key, kind, ring)
    }
}
