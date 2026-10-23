//! Explicit, RAM-only, real endpoint AEC calibration. Quiet or unreliable tests fail.
use anyhow::{ensure, Result};
use audio_core::{CaptureBackend, Ring, SourceKind};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
pub struct Session {
    pub mic_key: String,
    pub system_key: String,
    pub measured_at: Instant,
    pub value: dsp::Calibration,
}
struct Probe {
    origin: Option<u64>,
    samples: Vec<f32>,
    points: Vec<(u64, u64)>,
}
impl Probe {
    fn new() -> Self {
        Self {
            origin: None,
            samples: Vec::with_capacity(48000 * 10),
            points: Vec::with_capacity(1000),
        }
    }
    fn drain(&mut self, ring: &Ring) {
        while let Some(block) = ring.queue.pop() {
            let origin = *self.origin.get_or_insert(block.monotonic_ns);
            let offset = block.monotonic_ns.saturating_sub(origin) * 48000 / 1_000_000_000;
            let offset = offset as usize;
            if offset > 48000 * 10 {
                continue;
            }
            if offset > self.samples.len() {
                self.samples.resize(offset, 0.);
            }
            let end = (offset + block.frames).min(48000 * 10);
            if self.samples.len() < end {
                self.samples.resize(end, 0.);
            }
            self.samples[offset..end].copy_from_slice(&block.samples[..end - offset]);
            self.points
                .push((block.hardware_position, block.monotonic_ns));
        }
    }
    fn rate(&self) -> Option<f64> {
        let first = self.points.first()?;
        let last = self.points.last()?;
        let duration = last.1.checked_sub(first.1)?;
        if duration < 2_000_000_000 {
            return None;
        }
        Some(last.0.checked_sub(first.0)? as f64 * 1_000_000_000. / duration as f64)
    }
}
pub fn measure(
    backend: Arc<dyn CaptureBackend>,
    mic_key: String,
    system_key: String,
    seconds: u64,
) -> Result<Session> {
    ensure!(
        (3..=8).contains(&seconds),
        "calibration duration must be 3–8 seconds"
    );
    let devices = backend.devices()?;
    ensure!(
        devices
            .iter()
            .any(|d| d.key == mic_key && d.kind == SourceKind::Mic)
            && devices
                .iter()
                .any(|d| d.key == system_key && d.kind == SourceKind::System),
        "select explicit available mic and system endpoints"
    );
    let mic_ring = Ring::new();
    let system_ring = Ring::new();
    let mut system = backend.start(&system_key, SourceKind::System, system_ring.clone())?;
    let mut mic = backend.start(&mic_key, SourceKind::Mic, mic_ring.clone())?;
    let mut microphone = Probe::new();
    let mut reference = Probe::new();
    let began = Instant::now();
    while began.elapsed() < Duration::from_secs(seconds) {
        microphone.drain(&mic_ring);
        reference.drain(&system_ring);
        std::thread::sleep(Duration::from_millis(2));
    }
    mic.stop()?;
    system.stop()?;
    microphone.drain(&mic_ring);
    reference.drain(&system_ring);
    ensure!(
        !mic_ring.overflow.load(std::sync::atomic::Ordering::Acquire)
            && !system_ring
                .overflow
                .load(std::sync::atomic::Ordering::Acquire),
        "calibration endpoint changed or capture overflowed"
    );
    let mic_rate = microphone
        .rate()
        .ok_or_else(|| anyhow::anyhow!("microphone did not deliver enough timed audio"))?;
    let system_rate = reference.rate().ok_or_else(|| {
        anyhow::anyhow!(
            "system endpoint did not deliver enough timed audio; play actual system audio"
        )
    })?;
    let origin = microphone.origin.unwrap().max(reference.origin.unwrap());
    let mic_offset = (origin - microphone.origin.unwrap()) * 48000 / 1_000_000_000;
    let system_offset = (origin - reference.origin.unwrap()) * 48000 / 1_000_000_000;
    let mic_audio = microphone.samples.get(mic_offset as usize..).unwrap_or(&[]);
    let system_audio = reference
        .samples
        .get(system_offset as usize..)
        .unwrap_or(&[]);
    let drift = ((mic_rate / system_rate - 1.) * 1_000_000.) as f32;
    let value = dsp::calibrate(mic_audio, system_audio, drift);
    Ok(Session {
        mic_key,
        system_key,
        measured_at: Instant::now(),
        value,
    })
}
