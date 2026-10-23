//! Windows shared-mode WASAPI capture. Endpoint identity is never a friendly name.
#[cfg(windows)]
mod windows_backend {
    use anyhow::Result;
    use audio_core::*;
    use std::{
        sync::{
            atomic::{AtomicBool, Ordering},
            mpsc, Arc,
        },
        thread::{self, JoinHandle},
        time::Duration,
    };
    use wasapi::*;
    pub struct WasapiBackend;
    struct Stream {
        stop: Arc<AtomicBool>,
        join: Option<JoinHandle<Result<()>>>,
    }
    impl CaptureStream for Stream {
        fn stop(&mut self) -> Result<()> {
            self.stop.store(true, Ordering::Release);
            if let Some(t) = self.join.take() {
                t.join()
                    .map_err(|_| anyhow::anyhow!("WASAPI capture thread panicked"))??;
            }
            Ok(())
        }
    }
    impl Drop for Stream {
        fn drop(&mut self) {
            let _ = self.stop();
        }
    }
    impl CaptureBackend for WasapiBackend {
        fn devices(&self) -> Result<Vec<audio_core::Device>> {
            thread::spawn(|| {
                initialize_mta().ok()?;
                let enumerator = DeviceEnumerator::new()?;
                let mut devices = vec![];
                for (direction, kind) in [
                    (Direction::Capture, SourceKind::Mic),
                    (Direction::Render, SourceKind::System),
                ] {
                    let default = enumerator
                        .get_default_device(&direction)
                        .and_then(|d| d.get_id())
                        .ok();
                    let collection = enumerator.get_device_collection(&direction)?;
                    for item in &collection {
                        let item = item?;
                        let id = item.get_id()?;
                        devices.push(audio_core::Device {
                            key: format!("wasapi:{id}"),
                            name: item.get_friendlyname()?,
                            kind: kind.clone(),
                            is_default: default.as_ref() == Some(&id),
                            available: true,
                            experimental: false,
                        });
                    }
                }
                Ok::<_, anyhow::Error>(devices)
            })
            .join()
            .map_err(|_| anyhow::anyhow!("endpoint enumeration panicked"))?
        }
        fn start(
            &self,
            key: &str,
            kind: SourceKind,
            ring: Arc<Ring>,
        ) -> Result<Box<dyn CaptureStream>> {
            let key = key.to_owned();
            let stop = Arc::new(AtomicBool::new(false));
            let stopping = stop.clone();
            let (ready_tx, ready_rx) = mpsc::sync_channel(1);
            let join = thread::Builder::new()
                .name("wasapi-source".into())
                .spawn(move || {
                    let setup = (|| {
                        initialize_mta().ok()?;
                        let enumerator = DeviceEnumerator::new()?;
                        let direction = if kind == SourceKind::Mic {
                            Direction::Capture
                        } else {
                            Direction::Render
                        };
                        let device =
                            if key == "default" {
                                enumerator.get_default_device(&direction)?
                            } else {
                                enumerator.get_device(key.strip_prefix("wasapi:").ok_or_else(
                                    || anyhow::anyhow!("invalid WASAPI endpoint key"),
                                )?)?
                            };
                        let mut client = device.get_iaudioclient()?;
                        let native = client.get_mixformat()?;
                        let channels = native.get_nchannels();
                        let rate = native.get_samplespersec();
                        let format = WaveFormat::new(
                            32,
                            32,
                            &SampleType::Float,
                            rate as usize,
                            channels as usize,
                            None,
                        );
                        client.initialize_client(
                            &format,
                            &Direction::Capture,
                            &StreamMode::EventsShared {
                                autoconvert: true,
                                buffer_duration_hns: 200_000,
                            },
                        )?;
                        let event = client.set_get_eventhandle()?;
                        let capture = client.get_audiocaptureclient()?;
                        let capacity = client.get_buffer_size()? as usize;
                        let mut callbacks = DeviceEventCallbacks::new();
                        let changed = Arc::new(AtomicBool::new(false));
                        let c = changed.clone();
                        let selected = device.get_id()?;
                        callbacks.set_device_removed_callback(move |id| {
                            if id == selected {
                                c.store(true, Ordering::Release);
                            }
                        });
                        let c = changed.clone();
                        callbacks.set_default_device_callback(move |_, _, _| {
                            c.store(true, Ordering::Release);
                        });
                        let registration = enumerator.register_notification_callback(callbacks)?;
                        client.start_stream()?;
                        Ok::<_, anyhow::Error>((
                            client,
                            capture,
                            event,
                            capacity,
                            channels,
                            rate,
                            registration,
                            changed,
                        ))
                    })();
                    let (client, capture, event, capacity, channels, rate, _registration, changed) =
                        match setup {
                            Ok(x) => {
                                let _ = ready_tx.send(Ok(()));
                                x
                            }
                            Err(e) => {
                                let _ = ready_tx.send(Err(e.to_string()));
                                return Err(e);
                            }
                        };
                    let mut bytes = vec![0u8; capacity * channels as usize * 4];
                    let mut mono = Vec::with_capacity(capacity);
                    let mut resampled = Vec::with_capacity(capacity * 4);
                    let mut resampler = Resampler::new(rate, RATE)?;
                    let mut canonical_position = 0;
                    let mut pending = AudioBlock {
                        samples: [0.; BLOCK],
                        frames: 0,
                        hardware_position: 0,
                        monotonic_ns: 0,
                        discontinuity: false,
                    };
                    let mut drift_anchor = None;
                    let mut rate_factor = 1.0f64;
                    while !stopping.load(Ordering::Acquire) {
                        if changed.swap(false, Ordering::AcqRel) {
                            ring.overflow.store(true, Ordering::Release);
                        }
                        while capture.get_next_packet_size()?.unwrap_or(0) > 0 {
                            let (frames, info) = capture.read_from_device(&mut bytes)?;
                            mono.clear();
                            resampled.clear();
                            if info.flags.silent {
                                mono.resize(frames as usize, 0.);
                            } else {
                                for frame in bytes[..frames as usize * channels as usize * 4]
                                    .chunks_exact(channels as usize * 4)
                                {
                                    let sample = frame
                                        .chunks_exact(4)
                                        .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
                                        .sum::<f32>()
                                        / channels as f32;
                                    mono.push(if sample.is_finite() { sample } else { 0. });
                                }
                            }
                            let anchor = *drift_anchor.get_or_insert((info.index, info.timestamp));
                            if info.index >= anchor.0 && info.timestamp > anchor.1 + 10_000_000 {
                                let observed = (info.index - anchor.0) as f64 * 10_000_000.
                                    / (info.timestamp - anchor.1) as f64
                                    / rate as f64;
                                if (observed - 1.).abs() < 0.001 {
                                    rate_factor =
                                        rate_factor * 0.98 + observed.clamp(0.9997, 1.0003) * 0.02;
                                    resampler.ratio(rate as f64 / RATE as f64 * rate_factor);
                                } else {
                                    pending.discontinuity = true;
                                    drift_anchor = Some((info.index, info.timestamp));
                                }
                            }
                            resampler.process(&mono, &mut resampled);
                            for (offset, &sample) in resampled.iter().enumerate() {
                                if pending.frames == 0 {
                                    pending.hardware_position = canonical_position;
                                    pending.monotonic_ns = info.timestamp * 100
                                        + offset as u64 * 1_000_000_000 / 48000;
                                    pending.discontinuity =
                                        info.flags.data_discontinuity || info.flags.timestamp_error;
                                }
                                pending.samples[pending.frames] = sample;
                                pending.frames += 1;
                                canonical_position += 1;
                                if pending.frames == BLOCK {
                                    ring.push(pending.clone());
                                    pending.frames = 0;
                                }
                            }
                        }
                        if event.wait_for_event(100).is_err() {
                            thread::sleep(Duration::from_millis(1));
                        }
                    }
                    client.stop_stream()?;
                    resampled.clear();
                    resampler.finish(&mut resampled);
                    for sample in resampled {
                        if pending.frames == 0 {
                            pending.hardware_position = canonical_position;
                            pending.monotonic_ns = audio_core::monotonic_ns();
                        }
                        pending.samples[pending.frames] = sample;
                        pending.frames += 1;
                        canonical_position += 1;
                        if pending.frames == BLOCK {
                            ring.push(pending.clone());
                            pending.frames = 0;
                        }
                    }
                    // The final partial DSP block is preserved; the consumer flushes without padding.
                    if pending.frames > 0 {
                        ring.push(pending);
                    }
                    Ok(())
                })?;
            match ready_rx.recv_timeout(Duration::from_secs(10)) {
                Ok(Ok(())) => Ok(Box::new(Stream {
                    stop,
                    join: Some(join),
                })),
                Ok(Err(e)) => {
                    stop.store(true, Ordering::Release);
                    let _ = join.join();
                    Err(anyhow::anyhow!("Audio permission/device initialization failed: {e}. Check Windows microphone privacy and endpoint availability."))
                }
                Err(e) => {
                    stop.store(true, Ordering::Release);
                    Err(e.into())
                }
            }
        }
    }
}
#[cfg(windows)]
pub use windows_backend::WasapiBackend;
