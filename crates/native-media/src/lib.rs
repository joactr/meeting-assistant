//! Sole writer of durable audio bytes. Engine catalogs only committed metadata.
use anyhow::{ensure, Result};
use audiopus::{
    coder::{Decoder, Encoder},
    Application, Bitrate, Channels, SampleRate,
};
use ogg::{
    reading::PacketReader,
    writing::{PacketWriteEndInfo, PacketWriter},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};
pub mod playback;
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Commit {
    pub media_id: String,
    pub meeting_id: String,
    pub source_id: String,
    #[serde(default)]
    pub source_kind: Option<String>,
    pub relative_path: String,
    pub sha256: String,
    pub bytes: u64,
    pub start_ns: u64,
    pub frame_count: u64,
    pub sample_rate: u32,
    pub pre_skip: u16,
    pub codec: String,
}
pub struct SegmentWriter {
    root: PathBuf,
    meeting: String,
    source: String,
    pub source_kind: Option<String>,
    start_ns: u64,
    samples: Vec<f32>,
    pub cap_bytes: u64,
    pub retained_bytes: u64,
    pub capped: bool,
}
impl SegmentWriter {
    pub fn new(
        root: PathBuf,
        meeting: String,
        source: String,
        start_ns: u64,
        cap_bytes: u64,
    ) -> Result<Self> {
        uuid::Uuid::parse_str(&meeting)?;
        uuid::Uuid::parse_str(&source)?;
        fs::create_dir_all(&root)?;
        let retained_bytes = fs::read_dir(&root)?
            .filter_map(|e| e.ok())
            .filter_map(|e| e.metadata().ok())
            .filter(|m| m.is_file())
            .map(|m| m.len())
            .sum();
        Ok(Self {
            root,
            meeting,
            source,
            source_kind: None,
            start_ns,
            samples: Vec::with_capacity(48000 * 30),
            cap_bytes,
            retained_bytes,
            capped: false,
        })
    }
    pub fn push(&mut self, input: &[f32]) -> Result<Vec<Commit>> {
        let mut commits = vec![];
        if self.capped {
            return Ok(commits);
        }
        for chunk in input.chunks(480) {
            self.samples.extend_from_slice(chunk);
            if self.samples.len() >= 48000 * 30 {
                if let Some(c) = self.flush()? {
                    commits.push(c)
                }
            }
        }
        Ok(commits)
    }
    pub fn position(&mut self, start_ns: u64) {
        if self.samples.is_empty() {
            self.start_ns = start_ns;
        }
    }
    pub fn flush(&mut self) -> Result<Option<Commit>> {
        if self.samples.is_empty() || self.capped {
            return Ok(None);
        }
        static TRANSACTION: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _transaction = TRANSACTION
            .lock()
            .map_err(|_| anyhow::anyhow!("media transaction state unavailable"))?;
        self.retained_bytes =
            fs::read_dir(&self.root)?.try_fold(0u64, |bytes, entry| -> Result<u64> {
                let metadata = entry?.metadata()?;
                Ok(bytes
                    + if metadata.is_file() {
                        metadata.len()
                    } else {
                        0
                    })
            })?;
        // Reserve worst-case 64 kbit/s including container overhead, before writing.
        if self
            .retained_bytes
            .saturating_add((self.samples.len() as u64 / 48000 + 1) * 8192)
            > self.cap_bytes
        {
            self.capped = true;
            self.samples.clear();
            return Ok(None);
        }
        let id = uuid::Uuid::new_v4().to_string();
        let part = self.root.join(format!("{id}.opus.part"));
        let final_path = self.root.join(format!("{id}.opus"));
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&part)?;
        let mut writer = PacketWriter::new(file);
        let mut encoder = Encoder::new(SampleRate::Hz48000, Channels::Mono, Application::Audio)?;
        encoder.set_bitrate(Bitrate::BitsPerSecond(32000))?;
        let skip = encoder.lookahead()? as u16;
        let mut head = b"OpusHead".to_vec();
        head.extend_from_slice(&[1, 1]);
        head.extend_from_slice(&skip.to_le_bytes());
        head.extend_from_slice(&48000u32.to_le_bytes());
        head.extend_from_slice(&0i16.to_le_bytes());
        head.push(0);
        writer.write_packet(head, 1, PacketWriteEndInfo::EndPage, 0)?;
        let vendor = b"Meeting Assistant native-media";
        let mut tags = b"OpusTags".to_vec();
        tags.extend_from_slice(&(vendor.len() as u32).to_le_bytes());
        tags.extend_from_slice(vendor);
        tags.extend_from_slice(&0u32.to_le_bytes());
        writer.write_packet(tags, 1, PacketWriteEndInfo::EndPage, 0)?;
        let frames = self.samples.len();
        let padded = (frames + skip as usize).div_ceil(960) * 960;
        let mut pcm = [0f32; 960];
        let mut packet = [0u8; 4000];
        for offset in (0..padded).step_by(960) {
            pcm.fill(0.);
            let available = frames.saturating_sub(offset).min(960);
            if available > 0 {
                pcm[..available].copy_from_slice(&self.samples[offset..offset + available]);
            }
            let n = encoder.encode_float(&pcm, &mut packet)?;
            let last = offset + 960 == padded;
            let granule = if last {
                frames as u64 + skip as u64
            } else {
                (offset + 960) as u64
            };
            writer.write_packet(
                packet[..n].to_vec(),
                1,
                if last {
                    PacketWriteEndInfo::EndStream
                } else {
                    PacketWriteEndInfo::NormalPacket
                },
                granule,
            )?;
        }
        let file = writer.into_inner();
        file.sync_all()?;
        drop(file);
        let (hash, bytes) = hash_file(&part)?;
        durable_rename(&part, &final_path)?;
        let c = Commit {
            media_id: id.clone(),
            meeting_id: self.meeting.clone(),
            source_id: self.source.clone(),
            source_kind: self.source_kind.clone(),
            relative_path: format!("{id}.opus"),
            sha256: hash,
            bytes,
            start_ns: self.start_ns,
            frame_count: frames as u64,
            sample_rate: 48000,
            pre_skip: skip,
            codec: "ogg_opus".into(),
        };
        // Sidecar is native recovery metadata; never an SQL/business catalog.
        let sidepart = self.root.join(format!("{id}.commit.part"));
        let mut f = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&sidepart)?;
        f.write_all(&serde_json::to_vec(&c)?)?;
        f.sync_all()?;
        drop(f);
        durable_rename(&sidepart, &self.root.join(format!("{id}.commit")))?;
        self.start_ns += frames as u64 * 1_000_000_000 / 48000;
        self.retained_bytes += bytes;
        self.samples.clear();
        Ok(Some(c))
    }
}
pub fn durable_rename(source: &Path, destination: &Path) -> Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn MoveFileExW(source: *const u16, destination: *const u16, flags: u32) -> i32;
        }
        let source: Vec<_> = source.as_os_str().encode_wide().chain(Some(0)).collect();
        let destination: Vec<_> = destination
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect();
        if unsafe { MoveFileExW(source.as_ptr(), destination.as_ptr(), 0x9) } == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(unix)]
    {
        fs::rename(source, destination)?;
        if let Some(parent) = destination.parent() {
            sync_directory(parent)?;
        }
    }
    Ok(())
}
pub fn hash_file(path: &Path) -> Result<(String, u64)> {
    let mut f = File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut len = 0;
    loop {
        let n = f.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
        len += n as u64;
    }
    Ok((hex::encode(hash.finalize()), len))
}
pub fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(windows)]
    {
        let _ = path;
    }
    Ok(())
}
pub fn scoped_path(root: &Path, relative: &str) -> Result<PathBuf> {
    let p = Path::new(relative);
    ensure!(
        p.components().count() == 1
            && !relative.contains(['\\', '/'])
            && relative.ends_with(".opus"),
        "invalid media handle"
    );
    let path = root.join(p);
    ensure!(
        path.canonicalize()?.starts_with(root.canonicalize()?),
        "media escaped workspace"
    );
    Ok(path)
}
pub fn reconcile(root: &Path) -> Result<Vec<Commit>> {
    let mut commits = vec![];
    if !root.exists() {
        return Ok(commits);
    }
    for entry in fs::read_dir(root)? {
        let e = entry?;
        if e.path().extension().and_then(|e| e.to_str()) == Some("commit") {
            let bytes = fs::read(e.path())?;
            ensure!(bytes.len() <= 16384, "oversize media metadata");
            let c: Commit = serde_json::from_slice(&bytes)?;
            let p = scoped_path(root, &c.relative_path)?;
            let (hash, len) = hash_file(&p)?;
            ensure!(
                hash == c.sha256 && len == c.bytes,
                "committed media hash mismatch"
            );
            let decoded = decode_opus(File::open(p)?)?;
            ensure!(
                decoded.len() == c.frame_count as usize,
                "committed media timeline mismatch"
            );
            commits.push(c);
        } else if e.path().extension().and_then(|e| e.to_str()) == Some("part")
            && e.metadata()?
                .modified()?
                .elapsed()
                .unwrap_or_default()
                .as_secs()
                > 86400
        {
            fs::remove_file(e.path())?;
        }
    }
    Ok(commits)
}
pub fn decode_opus(file: File) -> Result<Vec<f32>> {
    ensure!(
        file.metadata()?.len() < 16 * 1024 * 1024,
        "segment exceeds size bound"
    );
    let mut reader = PacketReader::new(file);
    let head = reader
        .read_packet()?
        .ok_or_else(|| anyhow::anyhow!("missing OpusHead"))?;
    ensure!(
        head.data.len() == 19
            && &head.data[..8] == b"OpusHead"
            && head.data[9] == 1
            && head.data[18] == 0,
        "unsupported Opus header"
    );
    let skip = u16::from_le_bytes(head.data[10..12].try_into().unwrap()) as usize;
    let tags = reader
        .read_packet()?
        .ok_or_else(|| anyhow::anyhow!("missing OpusTags"))?;
    ensure!(tags.data.starts_with(b"OpusTags"), "invalid OpusTags");
    let mut decoder = Decoder::new(SampleRate::Hz48000, Channels::Mono)?;
    let mut out = Vec::with_capacity(48000 * 31);
    let mut pcm = [0f32; 5760];
    let mut final_granule = None;
    while let Some(packet) = reader.read_packet()? {
        let input = audiopus::packet::Packet::try_from(packet.data.as_slice())?;
        let signals = audiopus::MutSignals::try_from(pcm.as_mut_slice())?;
        let n = decoder.decode_float(Some(input), signals, false)?;
        out.extend_from_slice(&pcm[..n]);
        ensure!(
            out.len() <= 48000 * 31,
            "decoded segment exceeds duration bound"
        );
        if packet.last_in_stream() {
            final_granule = Some(packet.absgp_page());
        }
    }
    let final_granule =
        final_granule.ok_or_else(|| anyhow::anyhow!("uncommitted Opus stream"))? as usize;
    ensure!(
        final_granule >= skip && final_granule <= out.len(),
        "invalid final granule"
    );
    Ok(out[skip..final_granule].to_vec())
}
