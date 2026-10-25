//! Capture epochs, bounded DSP queues, native media and explicit boundary acknowledgements.
use anyhow::{bail, ensure, Result};
use audio_core::{CaptureBackend, CaptureStream, ClockMap, Resampler, Ring, SourceKind};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tokio::io::AsyncWriteExt;

type EchoReferences = Arc<std::sync::Mutex<std::collections::VecDeque<(u64, [f32; 480])>>>;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub source_index: u32,
    pub source_id: String,
    pub device_key: String,
    pub kind: SourceKind,
}
#[derive(Clone, Deserialize)]
pub struct Start {
    pub meeting_id: String,
    pub capture_epoch: u64,
    pub sources: Vec<Source>,
    #[serde(default)]
    pub recording: bool,
    #[serde(default = "default_cap")]
    pub media_cap_bytes: u64,
    #[serde(default)]
    pub vad: dsp::VadConfig,
    #[serde(default)]
    pub denoise: bool,
    #[serde(default)]
    pub aec: bool,
    pub calibration: Option<dsp::Calibration>,
}
fn default_cap() -> u64 {
    10 * 1024 * 1024 * 1024
}
#[derive(Debug, Clone, Serialize)]
pub struct Boundary {
    pub source_index: u32,
    pub sequence: u64,
    pub capture_epoch: u64,
    pub committed_media: Vec<native_media::Commit>,
}
enum Control {
    Pause(mpsc::Sender<Result<Boundary>>),
    Resume(u64, mpsc::Sender<Result<Boundary>>),
    Stop(mpsc::Sender<Result<Boundary>>),
}
struct Runner {
    tx: mpsc::Sender<Control>,
    join: Option<JoinHandle<Result<Boundary>>>,
}
pub struct Capture {
    backend: Arc<dyn CaptureBackend>,
    media_root: PathBuf,
    resource_root: PathBuf,
    events: crate::Events,
    runners: HashMap<u32, Runner>,
    pub meeting_id: Option<String>,
    test: Option<Test>,
    network: Option<tokio::task::JoinHandle<Result<()>>>,
    pub active: bool,
    calibration: Option<crate::calibration::Session>,
}
struct Test {
    stream: Box<dyn CaptureStream>,
    cancel: Arc<AtomicBool>,
    join: Option<JoinHandle<()>>,
}
impl Capture {
    pub fn new(
        backend: Arc<dyn CaptureBackend>,
        media_root: PathBuf,
        resource_root: PathBuf,
        events: crate::Events,
    ) -> Self {
        Self {
            backend,
            media_root,
            resource_root,
            events,
            runners: HashMap::new(),
            meeting_id: None,
            test: None,
            network: None,
            active: false,
            calibration: None,
        }
    }
    pub fn devices(&self) -> Result<Vec<audio_core::Device>> {
        self.backend.devices()
    }
    pub fn test_start(&mut self, key: &str) -> Result<Value> {
        ensure!(
            !self.active,
            "audio test unavailable during meeting capture"
        );
        self.test_stop()?;
        let device = self
            .backend
            .devices()?
            .into_iter()
            .find(|d| d.key == key)
            .ok_or_else(|| {
                anyhow::anyhow!("selected audio test endpoint is missing; reselect explicitly")
            })?;
        let ring = Ring::new();
        let stream = self.backend.start(key, device.kind, ring.clone())?;
        let cancel = Arc::new(AtomicBool::new(false));
        let stop = cancel.clone();
        let events = self.events.clone();
        let id = uuid::Uuid::new_v4().to_string();
        let lease = id.clone();
        let join = thread::spawn(move || {
            let mut count = 0;
            while !stop.load(Ordering::Acquire) {
                while let Some(block) = ring.queue.pop() {
                    count += 1;
                    if count % 5 == 0 {
                        let s = &block.samples[..block.frames];
                        (events)(
                            "native.audio.level",
                            json!({"lease_id":lease,"rms":dsp::rms(s),"peak":s.iter().fold(0f32,|a,x|a.max(x.abs()))}),
                        );
                    }
                }
                thread::sleep(Duration::from_millis(5));
            }
        });
        self.test = Some(Test {
            stream,
            cancel,
            join: Some(join),
        });
        Ok(json!({"lease_id":id,"state":"active"}))
    }
    pub fn test_stop(&mut self) -> Result<Value> {
        if let Some(mut test) = self.test.take() {
            test.stream.stop()?;
            test.cancel.store(true, Ordering::Release);
            if let Some(join) = test.join.take() {
                let _ = join.join();
            }
        }
        Ok(json!({"state":"stopped"}))
    }
    pub async fn calibrate(
        &mut self,
        mic_key: String,
        system_key: String,
        seconds: u64,
    ) -> Result<Value> {
        ensure!(
            !self.active,
            "calibration unavailable during meeting capture"
        );
        self.test_stop()?;
        let backend = self.backend.clone();
        let session = tokio::task::spawn_blocking(move || {
            crate::calibration::measure(backend, mic_key, system_key, seconds)
        })
        .await??;
        let result = serde_json::to_value(&session.value)?;
        (self.events)("native.audio.calibration", result.clone());
        self.calibration = Some(session);
        Ok(result)
    }
    pub async fn start(
        &mut self,
        mut config: Start,
        engine: Arc<crate::supervisor::Engine>,
    ) -> Result<Value> {
        ensure!(
            !self.active && self.runners.is_empty(),
            "capture already active"
        );
        self.test_stop()?;
        ensure!(
            config.capture_epoch > 0 && !config.sources.is_empty() && config.sources.len() <= 2,
            "invalid capture source plan"
        );
        if config.aec {
            let measured = self.calibration.as_ref().ok_or_else(|| {
                anyhow::anyhow!("AEC requires a real endpoint calibration in this session")
            })?;
            ensure!(
                measured.value.passed
                    && measured.measured_at.elapsed() < Duration::from_secs(86400)
                    && config
                        .sources
                        .iter()
                        .any(|s| s.kind == SourceKind::Mic && s.device_key == measured.mic_key)
                    && config.sources.iter().any(
                        |s| s.kind == SourceKind::System && s.device_key == measured.system_key
                    ),
                "AEC calibration failed, expired or belongs to a different endpoint pair"
            );
            config.calibration = Some(measured.value.clone());
        } else {
            config.calibration = None;
        }
        ensure!(
            !config.aec || config.sources.iter().any(|s| s.kind == SourceKind::System),
            "AEC requires an actively synchronized system reference"
        );
        let references = Arc::new(std::sync::Mutex::new(std::collections::VecDeque::<(
            u64,
            [f32; 480],
        )>::with_capacity(200)));
        // Never feed native replay back into capture. The caller pauses playback first.
        let mut socket =
            tokio::net::TcpStream::connect(("127.0.0.1", engine.ready.audio_port)).await?;
        socket.set_nodelay(true)?;
        socket.write_all(&hex::decode(engine.token())?).await?;
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Vec<u8>>(1500 * config.sources.len());
        let events = self.events.clone();
        self.network = Some(tokio::spawn(async move {
            while let Some(frame) = rx.recv().await {
                match tokio::time::timeout(Duration::from_secs(2), socket.write_all(&frame)).await {
                    Ok(Ok(())) => {}
                    Ok(Err(error)) => return Err(error.into()),
                    Err(error) => {
                        (events)(
                            "native.capture.error",
                            json!({"code":"audio_transport_timeout","message":"Audio transport stalled; capture stopped with a visible gap"}),
                        );
                        return Err(error.into());
                    }
                }
            }
            socket.shutdown().await?;
            Ok(())
        }));
        let origin = Instant::now();
        let mut started = vec![];
        for source in &config.sources {
            let result = self.spawn_source(
                source.clone(),
                config.clone(),
                origin,
                tx.clone(),
                engine.clone(),
                references.clone(),
            );
            match result {
                Ok(runner) => {
                    self.runners.insert(source.source_index, runner);
                    started.push(source.source_index)
                }
                Err(error) => {
                    let _ = self.stop();
                    return Err(error);
                }
            }
        }
        drop(tx);
        self.active = true;
        self.meeting_id = Some(config.meeting_id.clone());
        Ok(json!({"state":"active","capture_epoch":config.capture_epoch,"sources":config.sources}))
    }
    fn spawn_source(
        &self,
        source: Source,
        config: Start,
        origin: Instant,
        packets: tokio::sync::mpsc::Sender<Vec<u8>>,
        engine: Arc<crate::supervisor::Engine>,
        references: EchoReferences,
    ) -> Result<Runner> {
        let backend = self.backend.clone();
        let media_root = self.media_root.clone();
        let resources = self.resource_root.clone();
        let events = self.events.clone();
        let (tx, rx) = mpsc::channel();
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let runtime = tokio::runtime::Handle::current();
        let monotonic_origin =
            audio_core::monotonic_ns().saturating_sub(origin.elapsed().as_nanos() as u64);
        let library = resources.join("dsp").join(if cfg!(windows) {
            "speexdsp.dll"
        } else if cfg!(target_os = "macos") {
            "libspeexdsp.dylib"
        } else {
            "libspeexdsp.so.1"
        });
        let mut speex = if source.kind == SourceKind::Mic && (config.denoise || config.aec) {
            Some(dsp::speex::Speex::new(
                &library,
                config.denoise,
                if config.aec {
                    config.calibration.clone()
                } else {
                    None
                },
            )?)
        } else {
            None
        };
        let join=thread::Builder::new().name(format!("audio-dsp-{}",source.source_index)).spawn(move||{
   let model=resources.join("dsp/silero_vad.onnx");let detector:Box<dyn dsp::Probability>=match dsp::silero::Silero::load(&model){Ok(s)=>Box::new(s),Err(e)=>{(events)("native.dsp.mode",json!({"source_index":source.source_index,"mode":"energy_fallback_rms_0.01","reason":e.to_string()}));Box::new(dsp::EnergyGate)}};let mut vad=dsp::Segmenter::new(config.vad.clone(),detector)?;let mut downsample=Resampler::new(48000,16000)?;let mut speech=Vec::with_capacity(512);let mut clock=ClockMap::new(48000);let mut epoch=config.capture_epoch;let mut sequence=0u64;let mut packet=Vec::<f32>::with_capacity(960);let mut packet_ns=0;let mut commits=vec![];let mut writer=if config.recording{let mut writer=native_media::SegmentWriter::new(media_root,config.meeting_id.clone(),source.source_id.clone(),0,config.media_cap_bytes)?;writer.source_kind=Some(if source.kind==SourceKind::Mic{"mic"}else{"system"}.into());Some(writer)}else{None};let mut meter_count=0;let mut paused=false;let mut pending_control=None;let mut need_vad_origin=true;let mut media_cap_notified=false;
   let ring=Ring::new();let mut stream=match backend.start(&source.device_key,source.kind.clone(),ring.clone()){Ok(s)=>{let _=ready_tx.send(Ok(()));Some(s)},Err(e)=>{let _=ready_tx.send(Err(e.to_string()));return Err(e)}};
   loop {
    if pending_control.is_none(){pending_control=rx.try_recv().ok();if matches!(pending_control,Some(Control::Pause(_))|Some(Control::Stop(_))){if let Some(mut s)=stream.take(){s.stop()?;}}}
    if matches!(pending_control,Some(Control::Resume(_,_))){if let Some(Control::Resume(new_epoch,ack))=pending_control.take(){ensure!(paused,"source is not paused");ensure!(new_epoch>epoch,"resume requires a new epoch");epoch=new_epoch;need_vad_origin=true;downsample=Resampler::new(48000,16000)?;clock=ClockMap::new(48000);stream=Some(backend.start(&source.device_key,source.kind.clone(),ring.clone())?);paused=false;let _=ack.send(Ok(Boundary{source_index:source.source_index,sequence,capture_epoch:epoch,committed_media:vec![]}));}}
    while let Some(block)=ring.queue.pop(){
     let observed=block.monotonic_ns.saturating_sub(monotonic_origin);let(start,reset)=clock.observe(block.hardware_position,observed);
     if need_vad_origin{vad.reset(start*16000/1_000_000_000);need_vad_origin=false;}
     if reset||block.discontinuity{(events)("native.capture.discontinuity",json!({"meeting_id":config.meeting_id,"source_index":source.source_index,"start_ns":start.to_string(),"reason":"clock_reset"}));}
     if source.kind==SourceKind::System&&block.frames==480{let mut reference=references.lock().map_err(|_|anyhow::anyhow!("reference queue poisoned"))?;if reference.len()==200{reference.pop_front();}reference.push_back((start,block.samples));}
     let raw=&block.samples[..block.frames];if let Some(writer)=writer.as_mut(){writer.position(start);for commit in writer.push(raw)?{let metadata=serde_json::to_value(&commit)?;let e=engine.clone();let notify=events.clone();runtime.spawn(async move{if let Err(error)=e.internal("media.commit",&metadata).await{notify("native.media.catalog_error",json!({"message":error.to_string(),"recoverable_sidecar":true}));}});commits.push(commit);}if writer.capped&&!media_cap_notified{media_cap_notified=true;(events)("native.media.cap",json!({"meeting_id":config.meeting_id,"source_index":source.source_index,"message":"Media cap reached; new audio no longer retained"}));}}
     let mut processed=block.samples;
     // Raw media above is independent from optional processed branch.
     if let Some(processor)=speex.as_mut(){if block.frames==480{
      let calibrated_delay=config.calibration.as_ref().map(|c|c.delay_samples as u64*1_000_000_000/48000).unwrap_or(0);let target=start.saturating_sub(calibrated_delay);
      let reference=if config.aec{let reference=references.lock().map_err(|_|anyhow::anyhow!("reference queue poisoned"))?;reference.iter().min_by_key(|(time,_)|time.abs_diff(target)).filter(|(time,_)|time.abs_diff(target)<15_000_000).map(|(_,samples)|*samples)}else{None};
      if let Err(error)=processor.process(&mut processed,reference.as_ref(),((clock.ratio-1.)*1_000_000.)as f32){processor.calibration=None;(events)("native.dsp.warning",json!({"source_index":source.source_index,"aec_enabled":false,"reason":error.to_string()}));processor.process(&mut processed,None,0.)?;}
     }}
     let samples=&processed[..block.frames];speech.clear();downsample.process(samples,&mut speech);for utterance in vad.push(&speech){let metadata=json!({"meeting_id":config.meeting_id,"source_index":source.source_index,"capture_epoch":epoch,"start_sample":utterance.start_sample,"start_ms":utterance.start_sample/16,"end_ms":(utterance.start_sample+utterance.samples.len()as u64)/16,"true_speech_samples":utterance.true_speech_samples,"flush_reason":"vad"});let e=engine.clone();runtime.spawn(async move{let _=e.internal("capture.utterance",&metadata).await;});}
     for(i,&sample)in samples.iter().enumerate(){if packet.is_empty(){packet_ns=start+i as u64*1_000_000_000/48000;}packet.push(sample);if packet.len()==960{sequence+=1;send_packet(&packets,&source,epoch,sequence,packet_ns,&packet)?;packet.clear();}}
     meter_count+=1;if meter_count%5==0{(events)("native.audio.level",json!({"meeting_id":config.meeting_id,"source_index":source.source_index,"source_kind":source.kind,"rms":dsp::rms(raw),"peak":raw.iter().fold(0f32,|a,x|a.max(x.abs()))}));}
    }
    if ring.overflow.swap(false,Ordering::AcqRel){if let Some(mut s)=stream.take(){s.stop()?;}(events)("native.capture.source_lost",json!({"meeting_id":config.meeting_id,"source_index":source.source_index,"state":"lost","reason":"endpoint_changed_or_bounded_ring_overflow","dropped_frames":ring.dropped_frames.load(Ordering::Relaxed).to_string()}));paused=true;}
    if let Some(control)=pending_control.take(){let(stop,ack)=match control{Control::Pause(ack)=>(false,ack),Control::Stop(ack)=>(true,ack),Control::Resume(_,_)=>unreachable!()};
     if !packet.is_empty(){sequence+=1;send_packet(&packets,&source,epoch,sequence,packet_ns,&packet)?;packet.clear();}speech.clear();downsample.finish(&mut speech);for utterance in vad.push(&speech).into_iter().chain(vad.flush()){let data=json!({"meeting_id":config.meeting_id,"source_index":source.source_index,"capture_epoch":epoch,"start_sample":utterance.start_sample,"start_ms":utterance.start_sample/16,"end_ms":(utterance.start_sample+utterance.samples.len()as u64)/16,"true_speech_samples":utterance.true_speech_samples,"flush_reason":if stop{"stop"}else{"pause"}});let e=engine.clone();let notify=events.clone();runtime.spawn(async move{if let Err(error)=e.internal("capture.utterance",&data).await{notify("native.capture.error",json!({"message":error.to_string(),"retryable":true}));}});}
     if let Some(writer)=writer.as_mut(){if let Some(commit)=writer.flush()?{let c=commit.clone();let e=engine.clone();runtime.spawn(async move{let _=e.internal("media.commit",&serde_json::to_value(c).unwrap_or(Value::Null)).await;});commits.push(commit);}}
     let boundary=Boundary{source_index:source.source_index,sequence,capture_epoch:epoch,committed_media:commits.clone()};let _=ack.send(Ok(boundary.clone()));paused=true;if stop{return Ok(boundary)}
    }
    if packets.is_closed(){if let Some(mut s)=stream.take(){s.stop()?;}bail!("engine audio transport closed; source stopped with visible interruption")}
    thread::sleep(Duration::from_millis(if paused{10}else{2}));
   }
  })?;
        match ready_rx.recv_timeout(Duration::from_secs(15)) {
            Ok(Ok(())) => Ok(Runner {
                tx,
                join: Some(join),
            }),
            Ok(Err(e)) => Err(anyhow::anyhow!(e)),
            Err(e) => Err(e.into()),
        }
    }
    pub fn pause(&mut self) -> Result<Value> {
        self.control(false, None)
    }
    pub fn resume(&mut self, epoch: u64) -> Result<Value> {
        self.control(false, Some(epoch))
    }
    fn control(&mut self, stop: bool, epoch: Option<u64>) -> Result<Value> {
        let mut boundaries = vec![];
        for runner in self.runners.values() {
            let (tx, rx) = mpsc::channel();
            runner.tx.send(if stop {
                Control::Stop(tx)
            } else if let Some(epoch) = epoch {
                Control::Resume(epoch, tx)
            } else {
                Control::Pause(tx)
            })?;
            boundaries.push(rx.recv_timeout(Duration::from_secs(15))??);
        }
        Ok(
            json!({"state":if stop{"stopped"}else if epoch.is_some(){"active"}else{"paused"},"boundaries":boundaries,"boundary_sequences":boundaries.iter().map(|b|(b.source_index.to_string(),json!(b.sequence))).collect::<serde_json::Map<String,Value>>()}),
        )
    }
    pub fn stop(&mut self) -> Result<Value> {
        if self.runners.is_empty() {
            self.active = false;
            return Ok(json!({"state":"stopped","boundaries":[]}));
        }
        let result = self.control(true, None);
        for (_, mut runner) in self.runners.drain() {
            if let Some(join) = runner.join.take() {
                let _ = join.join();
            }
        }
        self.active = false;
        self.meeting_id = None;
        result
    }
}
fn send_packet(
    tx: &tokio::sync::mpsc::Sender<Vec<u8>>,
    source: &Source,
    epoch: u64,
    sequence: u64,
    start: u64,
    samples: &[f32],
) -> Result<()> {
    let header = worker_protocol::PcmHeader {
        sequence,
        capture_epoch: epoch,
        source_index: source.source_index,
        sample_rate: 48000,
        channels: 1,
        frame_count: samples.len() as u32,
        start_monotonic_ns: start,
    };
    let mut out = Vec::with_capacity(48 + samples.len() * 4);
    header.encode(samples, &mut out)?;
    tx.try_send(out).map_err(|_| {
        anyhow::anyhow!(
            "bounded 30-second audio backlog full: source paused; explicit timeline gap required"
        )
    })?;
    Ok(())
}
