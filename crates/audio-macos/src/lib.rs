//! Experimental, untested macOS 15+ CoreAudio/ScreenCaptureKit helper backend.
use anyhow::{ensure, Result};
use audio_core::*;
use std::{path::PathBuf, process::Command, sync::Arc};
pub struct MacBackend {
    helper: PathBuf,
}
impl MacBackend {
    pub fn new(helper: PathBuf) -> Self {
        Self { helper }
    }
}
impl CaptureBackend for MacBackend {
    fn devices(&self) -> Result<Vec<Device>> {
        let out = Command::new(&self.helper).arg("devices").output()?;
        ensure!(
            out.status.success(),
            "CoreAudio enumeration/permission failed"
        );
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
