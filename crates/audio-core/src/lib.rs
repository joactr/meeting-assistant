//! Native capture port. Callback code only pushes into a fixed-capacity ring.
use anyhow::{ensure, Result};
use crossbeam_queue::ArrayQueue;
use serde::{Deserialize, Serialize};
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc,
};
pub const RATE: u32 = 48_000;
pub const BLOCK: usize = 480;
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SourceKind {
    Mic,
    System,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Device {
    pub key: String,
    pub name: String,
    pub kind: SourceKind,
    pub is_default: bool,
    pub available: bool,
    pub experimental: bool,
}
#[derive(Clone)]
pub struct AudioBlock {
    pub samples: [f32; BLOCK],
    pub frames: usize,
    pub hardware_position: u64,
    pub monotonic_ns: u64,
    pub discontinuity: bool,
}
pub struct Ring {
    pub queue: ArrayQueue<AudioBlock>,
    pub overflow: AtomicBool,
    pub dropped_frames: AtomicU64,
}
impl Ring {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            queue: ArrayQueue::new(200),
            overflow: AtomicBool::new(false),
            dropped_frames: AtomicU64::new(0),
        })
    }
    pub fn push(&self, block: AudioBlock) {
        if let Err(block) = self.queue.push(block) {
            self.overflow.store(true, Ordering::Release);
            self.dropped_frames
                .fetch_add(block.frames as u64, Ordering::Relaxed);
        }
    }
}
pub trait CaptureStream: Send {
    fn stop(&mut self) -> Result<()>;
}
pub trait CaptureBackend: Send + Sync {
    fn devices(&self) -> Result<Vec<Device>>;
    fn start(&self, key: &str, kind: SourceKind, ring: Arc<Ring>)
        -> Result<Box<dyn CaptureStream>>;
}
/// Bounded clock PLL. A reset is a visible discontinuity, never hidden overlap.
pub struct ClockMap {
    rate: u32,
    origin_frame: u64,
    origin_ns: u64,
    last_frame: u64,
    last_ns: u64,
    pub ratio: f64,
    pub resets: u64,
}
impl ClockMap {
    pub fn new(rate: u32) -> Self {
        Self {
            rate,
            origin_frame: 0,
            origin_ns: 0,
            last_frame: 0,
            last_ns: 0,
            ratio: 1.,
            resets: 0,
        }
    }
    pub fn observe(&mut self, frame: u64, ns: u64) -> (u64, bool) {
        let first = self.origin_ns == 0;
        let expected = self.origin_ns.saturating_add(
            frame.saturating_sub(self.origin_frame) * 1_000_000_000 / self.rate as u64,
        );
        let reset = !first
            && (frame < self.last_frame
                || ns < self.last_ns
                || ns.abs_diff(expected) > 250_000_000);
        if first || reset {
            self.origin_frame = frame;
            self.origin_ns = ns;
            if reset {
                self.resets += 1;
            }
        }
        if frame > self.last_frame && ns > self.last_ns {
            let measured = (ns - self.last_ns) as f64 * self.rate as f64
                / ((frame - self.last_frame) as f64 * 1e9);
            self.ratio = (0.995 * self.ratio + 0.005 * measured).clamp(0.999, 1.001);
        }
        self.last_frame = frame;
        self.last_ns = ns;
        (
            self.origin_ns.saturating_add(
                ((frame.saturating_sub(self.origin_frame)) as f64 * 1e9 / self.rate as f64
                    * self.ratio) as u64,
            ),
            reset,
        )
    }
}
/// Stateful mono resampler: one phase across packets, no duplicated boundary sample.
pub struct Resampler {
    step: f64,
    phase: f64,
    previous: f32,
    primed: bool,
}
impl Resampler {
    pub fn new(input: u32, output: u32) -> Result<Self> {
        ensure!(input > 0 && output > 0, "invalid rate");
        Ok(Self {
            step: input as f64 / output as f64,
            phase: 0.,
            previous: 0.,
            primed: false,
        })
    }
    pub fn ratio(&mut self, input_per_output: f64) {
        self.step = input_per_output;
    }
    pub fn process(&mut self, input: &[f32], out: &mut Vec<f32>) {
        if input.is_empty() {
            return;
        }
        let offset = if self.primed { 1 } else { 0 };
        let len = input.len() + offset;
        let at = |i: usize| {
            if offset == 1 && i == 0 {
                self.previous
            } else {
                input[i - offset]
            }
        };
        while self.phase + 1. < len as f64 {
            let i = self.phase as usize;
            let f = (self.phase - i as f64) as f32;
            out.push(at(i) + (at(i + 1) - at(i)) * f);
            self.phase += self.step;
        }
        self.phase -= len.saturating_sub(1) as f64;
        self.previous = *input.last().unwrap();
        self.primed = true;
    }
    pub fn finish(&mut self, out: &mut Vec<f32>) {
        if self.primed {
            while self.phase < 1. {
                out.push(self.previous);
                self.phase += self.step;
            }
            self.primed = false;
            self.phase = 0.;
        }
    }
}
pub mod decoder;
/// OS monotonic nanoseconds, sharing the WASAPI QPC timestamp domain on Windows.
pub fn monotonic_ns() -> u64 {
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn QueryPerformanceCounter(value: *mut i64) -> i32;
            fn QueryPerformanceFrequency(value: *mut i64) -> i32;
        }
        static FREQUENCY: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
        let frequency = *FREQUENCY.get_or_init(|| {
            let mut n = 0;
            unsafe {
                QueryPerformanceFrequency(&mut n);
            }
            n
        });
        let mut count = 0;
        unsafe {
            QueryPerformanceCounter(&mut count);
        }
        (count as u128 * 1_000_000_000 / frequency as u128) as u64
    }
    #[cfg(unix)]
    {
        let mut time = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        unsafe {
            libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut time);
        }
        time.tv_sec as u64 * 1_000_000_000 + time.tv_nsec as u64
    }
}
pub mod helper;
