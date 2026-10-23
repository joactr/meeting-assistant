//! Real device playback. Decode/mixing/control stays off the realtime callback.
use anyhow::{ensure, Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc, Mutex,
    },
    thread,
    time::Duration,
};
#[derive(Clone)]
pub struct Plan {
    pub root: PathBuf,
    pub segments: Vec<crate::Commit>,
    pub position_ms: u64,
    pub end_ms: u64,
    pub muted_sources: Vec<String>,
    pub solo_source: Option<String>,
}
impl Plan {
    fn audible(&self, c: &crate::Commit) -> bool {
        !self
            .muted_sources
            .iter()
            .any(|s| s == &c.source_id || c.source_kind.as_ref() == Some(s))
            && self
                .solo_source
                .as_ref()
                .map(|s| s == &c.source_id || c.source_kind.as_ref() == Some(s))
                .unwrap_or(true)
    }
    fn chunk(&self, start_ms: u64, output: &mut Vec<f32>) -> Result<u64> {
        let end_ms = (start_ms + 30_000).min(self.end_ms);
        ensure!(end_ms >= start_ms, "invalid replay range");
        output.clear();
        output.resize(((end_ms - start_ms) * 48) as usize, 0.);
        let begin = start_ms * 48;
        let end = end_ms * 48;
        let sources: std::collections::HashSet<_> = self
            .segments
            .iter()
            .filter(|c| self.audible(c))
            .map(|c| &c.source_id)
            .collect();
        let gain = 1. / sources.len().max(1) as f32;
        for c in self.segments.iter().filter(|c| self.audible(c)) {
            let segment_begin = (c.start_ns as u128 * 48000 / 1_000_000_000) as u64;
            let segment_end = segment_begin + c.frame_count;
            if segment_begin >= end || segment_end <= begin {
                continue;
            }
            let file = std::fs::File::open(crate::scoped_path(&self.root, &c.relative_path)?)?;
            let decoded = crate::decode_opus(file)?;
            let overlap_begin = begin.max(segment_begin);
            let overlap_end = end.min(segment_end);
            for position in overlap_begin..overlap_end {
                output[(position - begin) as usize] +=
                    decoded[(position - segment_begin) as usize] * gain;
            }
        }
        Ok(end_ms)
    }
}
#[derive(Clone, Copy)]
struct Sample {
    value: f32,
    position_ms: u64,
}
#[derive(Clone)]
pub struct Playback {
    tx: mpsc::Sender<(Control, mpsc::SyncSender<std::result::Result<(), String>>)>,
    pub position: Arc<AtomicU64>,
    pub playing: Arc<AtomicBool>,
    pub speed_bits: Arc<AtomicU64>,
    failure: Arc<Mutex<Option<String>>>,
}
enum Control {
    Load(Plan, f32),
    Pause,
    Resume,
    Seek(u64),
    Speed(f32),
    Sources(Vec<String>, Option<String>),
    Close,
}
fn stream<T: cpal::SizedSample + cpal::FromSample<f32>>(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    queue: Arc<crossbeam_queue::ArrayQueue<Sample>>,
    on: Arc<AtomicBool>,
    position: Arc<AtomicU64>,
    failure: Arc<Mutex<Option<String>>>,
) -> Result<cpal::Stream> {
    let channels = config.channels as usize;
    let stopped = on.clone();
    Ok(device.build_output_stream(
        config,
        move |data: &mut [T], _| {
            for frame in data.chunks_mut(channels) {
                let value = if on.load(Ordering::Relaxed) {
                    if let Some(sample) = queue.pop() {
                        position.store(sample.position_ms, Ordering::Relaxed);
                        sample.value
                    } else {
                        0.
                    }
                } else {
                    0.
                };
                frame.fill(T::from_sample(value));
            }
        },
        move |error| {
            stopped.store(false, Ordering::Release);
            if let Ok(mut failure) = failure.lock() {
                *failure = Some(error.to_string());
            }
        },
        None,
    )?)
}
impl Playback {
    pub fn new(progress: Arc<dyn Fn(u64, bool) + Send + Sync>) -> Result<Self> {
        let (tx, rx) =
            mpsc::channel::<(Control, mpsc::SyncSender<std::result::Result<(), String>>)>();
        let position = Arc::new(AtomicU64::new(0));
        let playing = Arc::new(AtomicBool::new(false));
        let speed_bits = Arc::new(AtomicU64::new(1f32.to_bits() as u64));
        let failure = Arc::new(Mutex::new(None));
        let p = position.clone();
        let play = playing.clone();
        let speed_state = speed_bits.clone();
        let failed = failure.clone();
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        thread::Builder::new()
            .name("native-media-playback".into())
            .spawn(move || {
                let result = (|| -> Result<()> {
                    let host = cpal::default_host();
                    let device = host
                        .default_output_device()
                        .context("No native audio playback device is available")?;
                    let supported = device.default_output_config()?;
                    let sample_format = supported.sample_format();
                    let config: cpal::StreamConfig = supported.into();
                    let rate = config.sample_rate.0;
                    let ring = Arc::new(crossbeam_queue::ArrayQueue::new(rate as usize * 2));
                    let device_stream = match sample_format {
                        cpal::SampleFormat::F32 => stream::<f32>(
                            &device,
                            &config,
                            ring.clone(),
                            play.clone(),
                            p.clone(),
                            failed.clone(),
                        )?,
                        cpal::SampleFormat::I16 => stream::<i16>(
                            &device,
                            &config,
                            ring.clone(),
                            play.clone(),
                            p.clone(),
                            failed.clone(),
                        )?,
                        cpal::SampleFormat::U16 => stream::<u16>(
                            &device,
                            &config,
                            ring.clone(),
                            play.clone(),
                            p.clone(),
                            failed.clone(),
                        )?,
                        other => {
                            return Err(anyhow::anyhow!(
                                "Playback sample format {other:?} is unsupported"
                            ))
                        }
                    };
                    device_stream.play()?;
                    let _ = ready_tx.send(Ok::<_, String>(()));
                    let mut plan: Option<Plan> = None;
                    let mut samples = Vec::with_capacity(48000 * 30);
                    let mut chunk_start = 0u64;
                    let mut chunk_end = 0u64;
                    let mut cursor = 0f64;
                    let mut speed = 1f32;
                    let mut last_report = std::time::Instant::now();
                    loop {
                        loop {
                            let (command, ack) = match rx.try_recv() {
                                Ok(value) => value,
                                Err(mpsc::TryRecvError::Empty) => break,
                                Err(mpsc::TryRecvError::Disconnected) => return Ok(()),
                            };
                            let handled = (|| -> Result<()> {
                                match command {
                                    Control::Load(next, next_speed) => {
                                        valid_speed(next_speed)?;
                                        play.store(false, Ordering::Release);
                                        while ring.pop().is_some() {}
                                        chunk_start = next.position_ms;
                                        chunk_end = next.chunk(chunk_start, &mut samples)?;
                                        cursor = 0.;
                                        speed = next_speed;
                                        speed_state
                                            .store(speed.to_bits() as u64, Ordering::Release);
                                        p.store(chunk_start, Ordering::Release);
                                        plan = Some(next);
                                        play.store(true, Ordering::Release)
                                    }
                                    Control::Pause => play.store(false, Ordering::Release),
                                    Control::Resume => {
                                        ensure!(
                                            plan.is_some(),
                                            "Select real retained audio before resuming playback"
                                        );
                                        play.store(true, Ordering::Release)
                                    }
                                    Control::Seek(position) => {
                                        let current = plan
                                            .as_ref()
                                            .context("Select retained audio before seeking")?;
                                        ensure!(
                                            position <= current.end_ms,
                                            "Seek exceeds retained playback range"
                                        );
                                        while ring.pop().is_some() {}
                                        chunk_start = position;
                                        chunk_end = current.chunk(position, &mut samples)?;
                                        cursor = 0.;
                                        p.store(position, Ordering::Release)
                                    }
                                    Control::Speed(value) => {
                                        valid_speed(value)?;
                                        speed = value;
                                        speed_state.store(speed.to_bits() as u64, Ordering::Release)
                                    }
                                    Control::Sources(muted, solo) => {
                                        let current = plan.as_mut().context(
                                            "Select retained audio before source controls",
                                        )?;
                                        current.muted_sources = muted;
                                        current.solo_source = solo;
                                        while ring.pop().is_some() {}
                                        chunk_start = p.load(Ordering::Acquire);
                                        chunk_end = current.chunk(chunk_start, &mut samples)?;
                                        cursor = 0.;
                                    }
                                    Control::Close => {
                                        play.store(false, Ordering::Release);
                                        while ring.pop().is_some() {}
                                        samples.fill(0.);
                                        samples.clear();
                                        plan = None;
                                        cursor = 0.;
                                    }
                                }
                                Ok(())
                            })();
                            let _ = ack.send(handled.map_err(|e| e.to_string()));
                        }
                        if play.load(Ordering::Acquire) {
                            while ring.len() < (rate / 5) as usize {
                                if cursor >= samples.len() as f64 {
                                    if let Some(current) = plan.as_ref() {
                                        if chunk_end < current.end_ms {
                                            cursor -= samples.len() as f64;
                                            chunk_start = chunk_end;
                                            chunk_end = current.chunk(chunk_start, &mut samples)?;
                                            continue;
                                        }
                                    }
                                    if ring.is_empty() {
                                        play.store(false, Ordering::Release);
                                    }
                                    break;
                                }
                                let i = cursor as usize;
                                let fraction = (cursor - i as f64) as f32;
                                let a = samples[i];
                                let b = *samples.get(i + 1).unwrap_or(&a);
                                if ring
                                    .push(Sample {
                                        value: (a + (b - a) * fraction).clamp(-1., 1.),
                                        position_ms: chunk_start + (cursor / 48.) as u64,
                                    })
                                    .is_err()
                                {
                                    break;
                                }
                                cursor += 48000. / rate as f64 * speed as f64;
                            }
                        }
                        if last_report.elapsed() >= Duration::from_millis(100) {
                            progress(p.load(Ordering::Acquire), play.load(Ordering::Acquire));
                            last_report = std::time::Instant::now();
                        }
                        thread::sleep(Duration::from_millis(3));
                    }
                })();
                if let Err(error) = result {
                    play.store(false, Ordering::Release);
                    let message = error.to_string();
                    if let Ok(mut failure) = failed.lock() {
                        *failure = Some(message.clone());
                    }
                    let _ = ready_tx.send(Err(message));
                    progress(p.load(Ordering::Acquire), false);
                }
            })?;
        ready_rx
            .recv_timeout(Duration::from_secs(10))?
            .map_err(anyhow::Error::msg)?;
        Ok(Self {
            tx,
            position,
            playing,
            speed_bits,
            failure,
        })
    }
    fn send(&self, command: Control) -> Result<()> {
        self.check()?;
        let (tx, rx) = mpsc::sync_channel(1);
        self.tx.send((command, tx))?;
        rx.recv_timeout(Duration::from_secs(10))?
            .map_err(anyhow::Error::msg)
    }
    pub fn check(&self) -> Result<()> {
        if let Some(error) = self
            .failure
            .lock()
            .map_err(|_| anyhow::anyhow!("playback failure state unavailable"))?
            .as_ref()
        {
            return Err(anyhow::anyhow!("Native playback unavailable: {error}"));
        }
        Ok(())
    }
    pub fn load(&self, plan: Plan, speed: f32) -> Result<()> {
        valid_speed(speed)?;
        self.send(Control::Load(plan, speed))
    }
    pub fn pause(&self) -> Result<()> {
        self.send(Control::Pause)
    }
    pub fn resume(&self) -> Result<()> {
        self.send(Control::Resume)
    }
    pub fn seek(&self, position: u64) -> Result<()> {
        self.send(Control::Seek(position))
    }
    pub fn speed(&self, speed: f32) -> Result<()> {
        valid_speed(speed)?;
        self.send(Control::Speed(speed))
    }
    pub fn sources(&self, muted: Vec<String>, solo: Option<String>) -> Result<()> {
        self.send(Control::Sources(muted, solo))
    }
    pub fn close(&self) -> Result<()> {
        self.send(Control::Close)
    }
}
pub fn valid_speed(speed: f32) -> Result<()> {
    ensure!(
        [0.75, 1., 1.25, 1.5, 2.].contains(&speed),
        "Unsupported playback speed"
    );
    Ok(())
}
