//! Deterministic native file-source harness and explicitly requested real-device smoke.
use anyhow::{bail, ensure, Context, Result};
use audio_core::{Ring, SourceKind};
use serde_json::{json, Value};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};
pub fn run(args: &[String]) -> Result<Value> {
    match args.first().map(String::as_str){Some("file-source-smoke")=>file_source(args.get(1).context("private output directory required")?),Some("devices")=>{let resources=std::env::current_exe()?.parent().context("executable directory")?.to_owned();Ok(json!({"items":crate::backend(&resources).devices()?,"platform":std::env::consts::OS,"experimental":!cfg!(windows)}))},Some("capture-smoke")=>hardware(args),_=>bail!("Usage: file-source-smoke <new-private-dir> | devices | capture-smoke <stable-key> <mic|system> <seconds> <new-private-dir>. Hardware capture writes audio only after this explicit command. macOS/Linux experimental untested.")}
}
fn file_source(directory: &str) -> Result<Value> {
    let root = PathBuf::from(directory);
    ensure!(
        !root.exists(),
        "harness refuses to overwrite an existing output directory"
    );
    crate::private_directory(&root)?;
    let media = root.join("media");
    crate::private_directory(&media)?;
    // Numbered PCM is synthetic and stays in memory when recording is off.
    let numbered: Vec<f32> = (0..48000 * 32 + 173)
        .map(|i| 0.03 + (i % 10000) as f32 / 100000.)
        .collect();
    let mut wire = Vec::with_capacity(4000);
    let mut expected_sequence = 1u64;
    let mut samples_received = 0;
    let mut exact = vec![];
    for chunk in numbered.chunks(960) {
        let header = worker_protocol::PcmHeader {
            sequence: expected_sequence,
            capture_epoch: 1,
            source_index: 0,
            sample_rate: 48000,
            channels: 1,
            frame_count: chunk.len() as u32,
            start_monotonic_ns: samples_received as u64 * 1_000_000_000 / 48000,
        };
        header.encode(chunk, &mut wire)?;
        let (decoded, payload) = worker_protocol::PcmHeader::decode(&wire)?;
        ensure!(
            decoded.sequence == expected_sequence,
            "packet sequence lost"
        );
        for bytes in payload.chunks_exact(4) {
            exact.push(f32::from_le_bytes(bytes.try_into().unwrap()));
        }
        expected_sequence += 1;
        samples_received += chunk.len();
    }
    ensure!(exact == numbered, "PCM boundary duplicate or invisible gap");
    ensure!(
        std::fs::read_dir(&media)?.next().is_none(),
        "recording-off wrote private audio"
    );
    let speech: Vec<f32> = (0..16000 * 26 + 77)
        .map(|i| 0.02 + (i % 5000) as f32 / 100000.)
        .collect();
    let mut vad = dsp::Segmenter::new(dsp::VadConfig::default(), Box::new(dsp::EnergyGate))?;
    let mut utterances = vec![];
    for chunk in speech.chunks(160) {
        utterances.extend(vad.push(chunk));
    }
    utterances.extend(vad.flush());
    let assembled: Vec<f32> = utterances
        .iter()
        .flat_map(|u| u.samples.iter().copied())
        .collect();
    ensure!(
        assembled == speech,
        "VAD maximum/stop boundary altered samples"
    );
    let utterance_count = utterances.len();
    let meeting = uuid::Uuid::new_v4().to_string();
    let source = uuid::Uuid::new_v4().to_string();
    let mut writer =
        native_media::SegmentWriter::new(media.clone(), meeting, source, 0, 10 * 1024 * 1024)?;
    let mut commits = vec![];
    for block in numbered.chunks(480) {
        commits.extend(writer.push(block)?);
    }
    if let Some(commit) = writer.flush()? {
        commits.push(commit);
    }
    ensure!(
        commits.len() == 2,
        "thirty-second independently decodable segmentation failed"
    );
    let mut decoded_frames = 0;
    for commit in &commits {
        let file = File::open(native_media::scoped_path(&media, &commit.relative_path)?)?;
        decoded_frames += native_media::decode_opus(file)?.len();
    }
    ensure!(
        decoded_frames == numbered.len(),
        "Opus pre-skip/end trim changed timeline"
    );
    let recovered = native_media::reconcile(&media)?;
    ensure!(
        recovered.len() == commits.len(),
        "orphan committed segments not recovered"
    );
    // Real WAV decoder path receives an explicit native-owned file handle.
    let wav = root.join("synthetic.wav");
    let mut file = OpenOptions::new().write(true).create_new(true).open(&wav)?;
    let n = numbered.len() as u32 * 4;
    file.write_all(b"RIFF")?;
    file.write_all(&(n + 36).to_le_bytes())?;
    file.write_all(b"WAVEfmt ")?;
    file.write_all(&16u32.to_le_bytes())?;
    file.write_all(&3u16.to_le_bytes())?;
    file.write_all(&1u16.to_le_bytes())?;
    file.write_all(&48000u32.to_le_bytes())?;
    file.write_all(&192000u32.to_le_bytes())?;
    file.write_all(&4u16.to_le_bytes())?;
    file.write_all(&32u16.to_le_bytes())?;
    file.write_all(b"data")?;
    file.write_all(&n.to_le_bytes())?;
    for sample in &numbered {
        file.write_all(&sample.to_le_bytes())?;
    }
    file.sync_all()?;
    drop(file);
    let mut imported = 0;
    audio_core::decoder::decode(File::open(wav)?, |rate, samples| {
        ensure!(rate == 48000, "import decoder rate");
        imported += samples.len();
        Ok(())
    })?;
    ensure!(imported == numbered.len(), "import decoder lost frames");
    Ok(
        json!({"scenario":"native_file_source","synthetic":true,"provider_inference_exercised":false,"hardware_exercised":false,"pcm_frames":samples_received,"packets":expected_sequence-1,"utterances":utterance_count,"committed_segments":commits,"decoded_frames":decoded_frames,"imported_frames":imported,"recording_off_no_media":true,"boundary_samples_exact":true,"opus_timeline_exact":true}),
    )
}
fn hardware(args: &[String]) -> Result<Value> {
    ensure!(
        args.len() == 5,
        "capture-smoke requires key, kind, seconds and new output directory"
    );
    let kind = match args[2].as_str() {
        "mic" => SourceKind::Mic,
        "system" => SourceKind::System,
        _ => bail!("kind must be mic/system"),
    };
    let seconds: u64 = args[3].parse()?;
    ensure!(
        (1..=60).contains(&seconds),
        "hardware smoke duration must be 1–60 seconds"
    );
    let root = PathBuf::from(&args[4]);
    ensure!(!root.exists(), "hardware smoke refuses existing output");
    crate::private_directory(&root)?;
    let resource = std::env::current_exe()?
        .parent()
        .context("executable directory")?
        .to_owned();
    let backend = crate::backend(&resource);
    let device = backend
        .devices()?
        .into_iter()
        .find(|d| d.key == args[1] && d.kind == kind)
        .context("stable endpoint missing; explicit reselection required")?;
    let ring = Ring::new();
    let mut stream = backend.start(&device.key, kind, ring.clone())?;
    let mut writer = native_media::SegmentWriter::new(
        root.clone(),
        uuid::Uuid::new_v4().to_string(),
        uuid::Uuid::new_v4().to_string(),
        0,
        10 * 1024 * 1024,
    )?;
    let start = Instant::now();
    let mut count = 0;
    let mut peak = 0f32;
    let mut commits = vec![];
    while start.elapsed() < Duration::from_secs(seconds) {
        while let Some(block) = ring.queue.pop() {
            let samples = &block.samples[..block.frames];
            count += samples.len();
            peak = peak.max(samples.iter().fold(0f32, |a, x| a.max(x.abs())));
            commits.extend(writer.push(samples)?);
        }
        thread::sleep(Duration::from_millis(2));
    }
    stream.stop()?;
    while let Some(block) = ring.queue.pop() {
        count += block.frames;
        commits.extend(writer.push(&block.samples[..block.frames])?);
    }
    if let Some(c) = writer.flush()? {
        commits.push(c)
    }
    ensure!(count > 0, "device produced no PCM");
    ensure!(
        !ring.overflow.load(std::sync::atomic::Ordering::Acquire),
        "capture overflow or endpoint invalidation"
    );
    Ok(
        json!({"scenario":"hardware_capture","device":device,"frames":count,"peak":peak,"silent":peak<0.0001,"committed_segments":commits,"experimental":!cfg!(windows),"note":"Silence can be legitimate; play a known sound/speak and inspect replay to establish real-source correctness."}),
    )
}
