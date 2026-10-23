//! Granted local audio decoding only: no URL, demuxer subprocess or playlist access.
use anyhow::{ensure, Result};
use serde_json::{json, Value};
use sha2::Digest;
use std::{
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};
fn stage(root: &Path, relative: &str) -> Result<PathBuf> {
    let parts: Vec<_> = Path::new(relative).components().collect();
    ensure!(
        parts.len() == 2 && parts[0].as_os_str() == "staging",
        "decode output must be private staging"
    );
    let name = parts[1]
        .as_os_str()
        .to_str()
        .ok_or_else(|| anyhow::anyhow!("invalid decode filename"))?;
    uuid::Uuid::parse_str(
        name.strip_suffix(".f32")
            .ok_or_else(|| anyhow::anyhow!("decode output must be UUID.f32"))?,
    )?;
    let directory = root.join("staging");
    crate::private_directory(&directory)?;
    Ok(directory.join(name))
}
fn input(file: File, mut consume: impl FnMut(u32, &[f32]) -> Result<()>) -> Result<String> {
    let mut probe = file;
    let mut magic = [0u8; 4];
    probe.read_exact(&mut magic)?;
    probe.seek(SeekFrom::Start(0))?;
    if &magic == b"OggS" {
        let mut reader = ogg::reading::PacketReader::new(probe);
        let first = reader
            .read_packet()?
            .ok_or_else(|| anyhow::anyhow!("empty Ogg media"))?;
        let mut file = reader.into_inner();
        file.seek(SeekFrom::Start(0))?;
        if first.data.starts_with(b"OpusHead") {
            decode_opus(file, &mut consume)?;
            return Ok("ogg_opus".into());
        }
        audio_core::decoder::decode(file, consume)?;
        return Ok("ogg_vorbis".into());
    }
    audio_core::decoder::decode(probe, consume)?;
    Ok(match &magic {
        b"RIFF" => "wav",
        b"fLaC" => "flac",
        _ => "probed_audio",
    }
    .into())
}
fn decode_opus(file: File, consume: &mut impl FnMut(u32, &[f32]) -> Result<()>) -> Result<()> {
    use audiopus::{coder::Decoder, Channels, SampleRate};
    let mut reader = ogg::reading::PacketReader::new(file);
    let head = reader
        .read_packet()?
        .ok_or_else(|| anyhow::anyhow!("missing OpusHead"))?;
    ensure!(
        head.data.len() >= 19 && head.data[9] >= 1 && head.data[9] <= 2 && head.data[18] == 0,
        "unsupported Opus channel mapping"
    );
    let channels = head.data[9] as usize;
    let mut skip = u16::from_le_bytes(head.data[10..12].try_into().unwrap()) as usize;
    let tags = reader
        .read_packet()?
        .ok_or_else(|| anyhow::anyhow!("missing OpusTags"))?;
    ensure!(tags.data.starts_with(b"OpusTags"), "invalid Opus metadata");
    let mut decoder = Decoder::new(
        SampleRate::Hz48000,
        if channels == 1 {
            Channels::Mono
        } else {
            Channels::Stereo
        },
    )?;
    let mut pcm = vec![0f32; 5760 * channels];
    let mut mono = Vec::with_capacity(5760);
    let mut decoded = 0usize;
    let mut finalized = false;
    while let Some(packet) = reader.read_packet()? {
        let data = audiopus::packet::Packet::try_from(packet.data.as_slice())?;
        let n = decoder.decode_float(
            Some(data),
            audiopus::MutSignals::try_from(pcm.as_mut_slice())?,
            false,
        )?;
        let end = if packet.last_in_stream() {
            finalized = true;
            ensure!(
                packet.absgp_page() >= decoded as u64
                    && packet.absgp_page() <= decoded as u64 + n as u64,
                "invalid Opus end granule"
            );
            (packet.absgp_page() as usize)
                .saturating_sub(decoded)
                .min(n)
        } else {
            n
        };
        let begin = skip.min(end);
        skip -= begin;
        mono.clear();
        for frame in pcm[begin * channels..end * channels].chunks_exact(channels) {
            mono.push(frame.iter().sum::<f32>() / channels as f32);
        }
        consume(48000, &mono)?;
        decoded += n;
        ensure!(
            decoded <= 48000 * 60 * 60 * 32,
            "media duration exceeds 32-hour bound"
        );
    }
    ensure!(finalized, "truncated Opus stream has no final granule");
    Ok(())
}
pub fn decode(
    root: &Path,
    file: File,
    original_name: String,
    relative: String,
    cancel: Arc<AtomicBool>,
) -> Result<Value> {
    let output = stage(root, &relative)?;
    let original_bytes = file.metadata()?.len();
    let mut hasher = sha2::Sha256::new();
    let mut hashfile = file.try_clone()?;
    let mut buffer = [0u8; 65536];
    loop {
        ensure!(!cancel.load(Ordering::Acquire), "media decode cancelled");
        let n = hashfile.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hasher.update(&buffer[..n]);
    }
    hashfile.seek(SeekFrom::Start(0))?;
    let hash = hex::encode(hasher.finalize());
    let file_out = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&output)?;
    let mut out = std::io::BufWriter::with_capacity(65536, file_out);
    let result = (|| {
        let mut resampler = None;
        let mut native_rate = 0;
        let mut converted = Vec::with_capacity(65536);
        let mut frames = 0u64;
        let codec = input(file, |rate, samples| {
            ensure!(!cancel.load(Ordering::Acquire), "media decode cancelled");
            if resampler.is_none() {
                resampler = Some(audio_core::Resampler::new(rate, 16000)?);
                native_rate = rate;
            }
            ensure!(
                native_rate == rate,
                "midstream sample-rate changes require separate import"
            );
            converted.clear();
            resampler.as_mut().unwrap().process(samples, &mut converted);
            frames += converted.len() as u64;
            ensure!(frames <= 16000 * 60 * 60 * 8, "import exceeds 8-hour bound");
            for sample in &converted {
                out.write_all(&sample.to_le_bytes())?;
            }
            Ok(())
        })?;
        if let Some(resampler) = resampler.as_mut() {
            converted.clear();
            resampler.finish(&mut converted);
            frames += converted.len() as u64;
            for sample in converted {
                out.write_all(&sample.to_le_bytes())?;
            }
        }
        ensure!(
            frames > 0 && frames <= 16000 * 60 * 60 * 8,
            "invalid import duration"
        );
        ensure!(!cancel.load(Ordering::Acquire), "media decode cancelled");
        out.flush()?;
        out.get_ref().sync_all()?;
        Ok::<_, anyhow::Error>(
            json!({"original_name":original_name,"hash":hash,"duration_ms":frames*1000/16000,"sample_rate":16000,"channels":1,"frames":frames,"decoded_relative_path":relative,"codec":codec,"bytes":frames*4,"original_bytes":original_bytes}),
        )
    })();
    drop(out);
    if result.is_err() {
        let _ = std::fs::remove_file(&output);
    }
    result
}
pub fn retain(
    root: &Path,
    media_root: &Path,
    file: Option<File>,
    relative: Option<&str>,
    meeting: String,
    source: String,
    cap: u64,
) -> Result<Value> {
    let mut writer =
        native_media::SegmentWriter::new(media_root.to_owned(), meeting, source, 0, cap)?;
    let mut commits = vec![];
    let mut resampler = None;
    let mut rate = 0;
    let mut converted = Vec::with_capacity(65536);
    let mut consume = |input_rate: u32, samples: &[f32]| -> Result<()> {
        if resampler.is_none() {
            rate = input_rate;
            resampler = Some(audio_core::Resampler::new(rate, 48000)?);
        }
        ensure!(input_rate == rate, "retained import sample rate changed");
        converted.clear();
        resampler.as_mut().unwrap().process(samples, &mut converted);
        commits.extend(writer.push(&converted)?);
        Ok(())
    };
    if let Some(file) = file {
        input(file, &mut consume)?;
    } else {
        let path = stage(
            root,
            relative.ok_or_else(|| anyhow::anyhow!("retention input is missing"))?,
        )?;
        ensure!(
            path.canonicalize()?
                .starts_with(root.join("staging").canonicalize()?),
            "staging escaped workspace"
        );
        let mut file = File::open(path)?;
        let mut bytes = [0u8; 65536];
        let mut samples = Vec::with_capacity(16384);
        loop {
            let n = file.read(&mut bytes)?;
            if n == 0 {
                break;
            }
            ensure!(n % 4 == 0, "invalid staged PCM");
            samples.clear();
            for b in bytes[..n].chunks_exact(4) {
                let value = f32::from_le_bytes(b.try_into().unwrap());
                ensure!(value.is_finite(), "invalid staged PCM");
                samples.push(value);
            }
            consume(16000, &samples)?;
        }
    }
    if let Some(resampler) = resampler.as_mut() {
        converted.clear();
        resampler.finish(&mut converted);
        commits.extend(writer.push(&converted)?);
    }
    if let Some(commit) = writer.flush()? {
        commits.push(commit)
    }
    Ok(json!({"committed_media":commits,"retention_capped":writer.capped}))
}
