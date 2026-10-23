//! Bounded framing shared by native capture and isolated workers.
use anyhow::{bail, ensure, Result};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
pub const VERSION: u16 = 1;
pub const MAX_FRAME: usize = 1_048_576;
pub const HEADER_BYTES: usize = 48;
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct PcmHeader {
    pub sequence: u64,
    pub capture_epoch: u64,
    pub source_index: u32,
    pub sample_rate: u32,
    pub channels: u16,
    pub frame_count: u32,
    pub start_monotonic_ns: u64,
}
impl PcmHeader {
    pub fn encode(&self, samples: &[f32], out: &mut Vec<u8>) -> Result<()> {
        ensure!(
            matches!(self.sample_rate, 16000 | 48000),
            "unsupported sample rate"
        );
        ensure!(
            self.channels == 1 && self.capture_epoch > 0,
            "invalid source dimensions or epoch"
        );
        ensure!(
            samples.len() == self.frame_count as usize * self.channels as usize,
            "PCM dimensions"
        );
        let length = 44 + samples.len() * 4;
        ensure!(
            length <= MAX_FRAME && samples.iter().all(|x| x.is_finite()),
            "invalid PCM"
        );
        out.clear();
        out.extend_from_slice(&(length as u32).to_le_bytes());
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&[1, 0]);
        out.extend_from_slice(&self.sequence.to_le_bytes());
        out.extend_from_slice(&self.capture_epoch.to_le_bytes());
        out.extend_from_slice(&self.source_index.to_le_bytes());
        out.extend_from_slice(&self.sample_rate.to_le_bytes());
        out.extend_from_slice(&self.channels.to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&self.frame_count.to_le_bytes());
        out.extend_from_slice(&self.start_monotonic_ns.to_le_bytes());
        for sample in samples {
            out.extend_from_slice(&sample.to_le_bytes());
        }
        Ok(())
    }
    pub fn decode(bytes: &[u8]) -> Result<(Self, &[u8])> {
        ensure!(
            bytes.len() >= HEADER_BYTES && bytes.len() <= MAX_FRAME + 4,
            "frame size"
        );
        let u16_at = |n| u16::from_le_bytes(bytes[n..n + 2].try_into().unwrap());
        let u32_at = |n| u32::from_le_bytes(bytes[n..n + 4].try_into().unwrap());
        let u64_at = |n| u64::from_le_bytes(bytes[n..n + 8].try_into().unwrap());
        ensure!(u32_at(0) as usize + 4 == bytes.len(), "length mismatch");
        ensure!(
            u16_at(4) == VERSION && bytes[6] == 1 && bytes[7] == 0 && u16_at(34) == 1,
            "protocol mismatch"
        );
        let h = Self {
            sequence: u64_at(8),
            capture_epoch: u64_at(16),
            source_index: u32_at(24),
            sample_rate: u32_at(28),
            channels: u16_at(32),
            frame_count: u32_at(36),
            start_monotonic_ns: u64_at(40),
        };
        ensure!(
            h.channels == 1 && matches!(h.sample_rate, 16000 | 48000) && h.capture_epoch > 0,
            "invalid PCM dimensions"
        );
        ensure!(
            h.frame_count as usize * 4 == bytes.len() - HEADER_BYTES,
            "inconsistent frames"
        );
        Ok((h, &bytes[HEADER_BYTES..]))
    }
}
pub async fn write_json<W: AsyncWrite + Unpin, T: Serialize>(
    writer: &mut W,
    value: &T,
) -> Result<()> {
    let bytes = serde_json::to_vec(value)?;
    ensure!(bytes.len() <= MAX_FRAME, "control envelope exceeds limit");
    writer.write_u32_le(bytes.len() as u32).await?;
    writer.write_all(&bytes).await?;
    writer.flush().await?;
    Ok(())
}
pub async fn read_json<R: AsyncRead + Unpin, T: serde::de::DeserializeOwned>(
    reader: &mut R,
) -> Result<T> {
    let len = reader.read_u32_le().await? as usize;
    if len == 0 || len > MAX_FRAME {
        bail!("control envelope exceeds limit")
    }
    let mut bytes = vec![0; len];
    reader.read_exact(&mut bytes).await?;
    Ok(serde_json::from_slice(&bytes)?)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_pcm_wire_and_rejection() {
        let h = PcmHeader {
            sequence: 3,
            capture_epoch: 1,
            source_index: 0,
            sample_rate: 48000,
            channels: 1,
            frame_count: 960,
            start_monotonic_ns: 100,
        };
        let mut b = vec![];
        h.encode(&[0.25; 960], &mut b).unwrap();
        assert_eq!(b.len(), 3888);
        assert_eq!(PcmHeader::decode(&b).unwrap().0.sequence, 3);
        b[32] = 2;
        assert!(PcmHeader::decode(&b).is_err());
    }
}
