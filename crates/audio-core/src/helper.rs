//! Experimental helper stdout is an inherited, bounded PCM/hardware-clock pipe.
use crate::*;
use std::{
    io::Read,
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    thread::{self, JoinHandle},
};
pub struct HelperStream {
    child: Child,
    reader: Option<JoinHandle<Result<()>>>,
    expected_exit: Arc<AtomicBool>,
}
impl CaptureStream for HelperStream {
    fn stop(&mut self) -> Result<()> {
        self.expected_exit.store(true, Ordering::Release);
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
        Ok(())
    }
}
impl Drop for HelperStream {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}
pub fn start(
    executable: &std::path::Path,
    key: &str,
    kind: SourceKind,
    ring: Arc<Ring>,
) -> Result<Box<dyn CaptureStream>> {
    let mut child = Command::new(executable)
        .args([
            "capture",
            key,
            if kind == SourceKind::Mic {
                "mic"
            } else {
                "system"
            },
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()?;
    let mut stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("native capture pipe missing"))?;
    let expected_exit = Arc::new(AtomicBool::new(false));
    let expected = expected_exit.clone();
    let (ready_tx, ready_rx) = std::sync::mpsc::sync_channel(1);
    let reader = thread::spawn(move || {
        let result = (|| -> Result<()> {
            let mut bytes = [0u8; BLOCK * 4];
            let mut previous_end = None;
            let mut ready = Some(ready_tx);
            loop {
                let mut header = [0u8; 24];
                if let Err(error) = stdout.read_exact(&mut header) {
                    if expected.load(Ordering::Acquire) {
                        return Ok(());
                    }
                    return Err(anyhow::anyhow!(
                        "Native capture helper stopped or permission was revoked: {error}"
                    ));
                }
                let n = u32::from_le_bytes(header[..4].try_into().unwrap()) as usize;
                let position = u64::from_le_bytes(header[4..12].try_into().unwrap());
                let clock = u64::from_le_bytes(header[12..20].try_into().unwrap());
                let flags = u32::from_le_bytes(header[20..24].try_into().unwrap());
                ensure!(
                    n <= BLOCK && clock > 0 && flags <= 1,
                    "Invalid native hardware frame"
                );
                if n == 0 {
                    if let Some(sender) = ready.take() {
                        let _ = sender.send(());
                    }
                    continue;
                }
                stdout.read_exact(&mut bytes[..n * 4])?;
                let discontinuity = flags != 0
                    || previous_end
                        .map(|previous: u64| position.abs_diff(previous) > 2)
                        .unwrap_or(false);
                let mut block = AudioBlock {
                    samples: [0.; BLOCK],
                    frames: n,
                    hardware_position: position,
                    monotonic_ns: clock,
                    discontinuity,
                };
                for (i, chunk) in bytes[..n * 4].chunks_exact(4).enumerate() {
                    let sample = f32::from_le_bytes(chunk.try_into().unwrap());
                    ensure!(sample.is_finite(), "Native helper emitted nonfinite audio");
                    block.samples[i] = sample;
                }
                previous_end = Some(position + n as u64);
                ring.push(block);
                if let Some(sender) = ready.take() {
                    let _ = sender.send(());
                }
            }
        })();
        if result.is_err() {
            ring.overflow.store(true, Ordering::Release);
        }
        result
    });
    match ready_rx.recv_timeout(std::time::Duration::from_secs(15)) {
        Ok(()) => Ok(Box::new(HelperStream {
            child,
            reader: Some(reader),
            expected_exit,
        })),
        Err(error) => {
            expected_exit.store(true, Ordering::Release);
            let _ = child.kill();
            let _ = child.wait();
            let failure = reader
                .join()
                .ok()
                .and_then(Result::err)
                .map(|e| e.to_string())
                .unwrap_or_else(|| error.to_string());
            Err(anyhow::anyhow!(
                "Native permission/device startup failed: {failure}"
            ))
        }
    }
}
