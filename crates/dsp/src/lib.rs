//! Stateful per-source speech segmentation and optional microphone DSP.
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
pub mod silero;
pub mod speex;
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VadConfig {
    pub threshold: f32,
    pub pre_roll_ms: u32,
    pub post_roll_ms: u32,
    pub minimum_speech_ms: u32,
    pub maximum_utterance_ms: u32,
}
impl Default for VadConfig {
    fn default() -> Self {
        Self {
            threshold: 0.5,
            pre_roll_ms: 300,
            post_roll_ms: 400,
            minimum_speech_ms: 250,
            maximum_utterance_ms: 12000,
        }
    }
}
#[derive(Debug)]
pub struct Utterance {
    pub start_sample: u64,
    pub samples: Vec<f32>,
    pub true_speech_samples: usize,
}
pub trait Probability: Send {
    fn predict(&mut self, samples: &[f32; 512]) -> Result<f32>;
    fn reset(&mut self);
    fn mode(&self) -> &str;
}
pub struct EnergyGate;
impl Probability for EnergyGate {
    fn predict(&mut self, s: &[f32; 512]) -> Result<f32> {
        Ok(if rms(s) >= 0.01 { 1. } else { 0. })
    }
    fn reset(&mut self) {}
    fn mode(&self) -> &str {
        "energy_fallback_rms_0.01"
    }
}
pub fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        0.
    } else {
        (samples.iter().map(|x| x * x).sum::<f32>() / samples.len() as f32).sqrt()
    }
}
pub struct Segmenter {
    config: VadConfig,
    detector: Box<dyn Probability>,
    accumulator: VecDeque<f32>,
    pre: VecDeque<f32>,
    active: Vec<f32>,
    position: u64,
    start: u64,
    speech: usize,
    silence: usize,
    pub fallback_reason: Option<String>,
}
impl Segmenter {
    pub fn new(config: VadConfig, detector: Box<dyn Probability>) -> Result<Self> {
        ensure!(
            (0.0..=1.0).contains(&config.threshold) && config.maximum_utterance_ms >= 1000,
            "invalid VAD configuration"
        );
        Ok(Self {
            config,
            detector,
            accumulator: VecDeque::with_capacity(1024),
            pre: VecDeque::with_capacity(4800),
            active: Vec::with_capacity(192000),
            position: 0,
            start: 0,
            speech: 0,
            silence: 0,
            fallback_reason: None,
        })
    }
    pub fn mode(&self) -> &str {
        self.detector.mode()
    }
    pub fn push(&mut self, input: &[f32]) -> Vec<Utterance> {
        self.accumulator.extend(input);
        let mut utterances = vec![];
        while self.accumulator.len() >= 512 {
            let mut window = [0.; 512];
            for x in &mut window {
                *x = self.accumulator.pop_front().unwrap();
            }
            let probability = match self.detector.predict(&window) {
                Ok(p) => p,
                Err(e) => {
                    self.fallback_reason = Some(e.to_string());
                    self.detector = Box::new(EnergyGate);
                    self.detector.predict(&window).unwrap_or(0.)
                }
            };
            self.consume(
                &window,
                probability >= self.config.threshold,
                &mut utterances,
            );
        }
        utterances
    }
    fn consume(&mut self, window: &[f32], speech: bool, out: &mut Vec<Utterance>) {
        if self.active.is_empty() && speech {
            self.start = self.position.saturating_sub(self.pre.len() as u64);
            self.active.extend(self.pre.drain(..));
        }
        if !self.active.is_empty() || speech {
            self.active.extend_from_slice(window);
            if speech {
                self.speech += window.len();
                self.silence = 0
            } else {
                self.silence += window.len();
            }
            // A long turn closes at its first short pause (40% of post-roll, ~160 ms) once it
            // passes half the limit: continuous speech then reaches the transcript every few
            // seconds, cut between words, instead of only at the hard limit.
            let long = self.active.len() >= self.config.maximum_utterance_ms as usize * 8;
            if self.silence >= self.config.post_roll_ms as usize * 16
                || (long && self.silence >= self.config.post_roll_ms as usize * 16 * 2 / 5)
                || self.active.len() >= self.config.maximum_utterance_ms as usize * 16
            {
                if let Some(u) = self.finish() {
                    out.push(u)
                }
            }
        } else {
            self.pre.extend(window);
            let cap = self.config.pre_roll_ms as usize * 16;
            while self.pre.len() > cap {
                self.pre.pop_front();
            }
        }
        self.position += window.len() as u64;
    }
    fn finish(&mut self) -> Option<Utterance> {
        let valid = self.speech >= self.config.minimum_speech_ms as usize * 16;
        let samples = std::mem::replace(&mut self.active, Vec::with_capacity(192000));
        let speech = std::mem::take(&mut self.speech);
        self.silence = 0;
        self.pre.clear();
        if valid {
            Some(Utterance {
                start_sample: self.start,
                samples,
                true_speech_samples: speech,
            })
        } else {
            None
        }
    }
    /// Pause/Stop share this path, including a genuine incomplete final window.
    pub fn flush(&mut self) -> Vec<Utterance> {
        let mut out = vec![];
        if !self.accumulator.is_empty() {
            let tail: Vec<_> = self.accumulator.drain(..).collect();
            let speaking = rms(&tail) >= 0.01;
            self.consume(&tail, speaking, &mut out);
        }
        if let Some(u) = self.finish() {
            out.push(u)
        }
        self.pre.clear();
        self.detector.reset();
        out
    }
    /// Next input sample index: every pushed sample, including those not yet windowed.
    pub fn cursor(&self) -> u64 {
        self.position + self.accumulator.len() as u64
    }
    pub fn reset(&mut self, start_sample: u64) {
        self.accumulator.clear();
        self.pre.clear();
        self.active.clear();
        self.speech = 0;
        self.silence = 0;
        self.position = start_sample;
        self.detector.reset();
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Calibration {
    pub delay_samples: usize,
    pub correlation: f32,
    pub drift_ppm: f32,
    pub passed: bool,
    pub reason: Option<String>,
}
pub fn calibrate(mic: &[f32], reference: &[f32], drift_ppm: f32) -> Calibration {
    let mut best = (0, 0.0f32);
    let n = reference.len().min(48000);
    let ref_energy = reference[..n].iter().map(|x| x * x).sum::<f32>();
    if n >= 4800 && ref_energy > 0.001 {
        for lag in (0..=9600).step_by(16) {
            if mic.len() < n + lag {
                break;
            }
            let mut dot = 0.;
            let mut energy = 0.;
            for i in (0..n).step_by(8) {
                dot += reference[i] * mic[i + lag];
                energy += mic[i + lag] * mic[i + lag];
            }
            let corr = dot / (ref_energy / 8. * energy).sqrt().max(1e-12);
            if corr > best.1 {
                best = (lag, corr)
            }
        }
    }
    let passed = best.1 >= 0.65 && drift_ppm.abs() <= 100.;
    Calibration {
        delay_samples: best.0,
        correlation: best.1,
        drift_ppm,
        passed,
        reason: if passed {
            None
        } else {
            Some("AEC disabled: synchronized reference, delay or drift could not be calibrated; use headphones".into())
        },
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn continuous_splits_have_no_duplicate_samples() {
        let c = VadConfig {
            minimum_speech_ms: 1,
            maximum_utterance_ms: 1000,
            ..Default::default()
        };
        let mut s = Segmenter::new(c, Box::new(EnergyGate)).unwrap();
        let samples: Vec<_> = (0..48000).map(|i| 0.02 + i as f32 / 1000000.).collect();
        let mut out = s.push(&samples);
        out.extend(s.flush());
        let flattened: Vec<_> = out.into_iter().flat_map(|u| u.samples).collect();
        assert_eq!(flattened, samples);
    }
    #[test]
    fn long_turn_closes_at_first_short_pause() {
        let c = VadConfig {
            minimum_speech_ms: 1,
            ..Default::default()
        };
        let mut s = Segmenter::new(c, Box::new(EnergyGate)).unwrap();
        let talk = |ms: usize| vec![0.05f32; ms * 16];
        // A 200 ms pause 2 s in is too short to close the turn; the one 7 s in closes it.
        let samples = [
            talk(2000),
            vec![0.; 3200],
            talk(5000),
            vec![0.; 3200],
            talk(2000),
        ]
        .concat();
        let mut out = s.push(&samples);
        let first = out.first().map(|u| u.samples.len()).unwrap_or(0);
        out.extend(s.flush());
        assert_eq!(out.len(), 2);
        assert!(
            (7000 * 16..7400 * 16).contains(&first),
            "first utterance {first} samples"
        );
    }
}
