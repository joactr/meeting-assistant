//! Streaming import decode. A caller must supply an already granted file handle.
use anyhow::{bail, ensure, Result};
use std::fs::File;
use symphonia::core::{
    audio::SampleBuffer, codecs::DecoderOptions, errors::Error, formats::FormatOptions,
    io::MediaSourceStream, meta::MetadataOptions, probe::Hint,
};
/// Largest media file accepted for import, in bytes.
pub const MAX_IMPORT_BYTES: u64 = 20 * 1024 * 1024 * 1024;

pub fn ensure_import_size(bytes: u64) -> Result<()> {
    ensure!(bytes <= MAX_IMPORT_BYTES, "media exceeds import bound");
    Ok(())
}

pub fn decode(file: File, mut consume: impl FnMut(u32, &[f32]) -> Result<()>) -> Result<()> {
    ensure_import_size(file.metadata()?.len())?;
    let stream = MediaSourceStream::new(Box::new(file), Default::default());
    let mut format = symphonia::default::get_probe()
        .format(
            &Hint::new(),
            stream,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )?
        .format;
    let track = format
        .default_track()
        .ok_or_else(|| anyhow::anyhow!("no audio track"))?;
    let id = track.id;
    let mut decoder =
        symphonia::default::get_codecs().make(&track.codec_params, &DecoderOptions::default())?;
    let mut mono = Vec::with_capacity(65536);
    loop {
        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(Error::IoError(e)) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
            Err(e) => return Err(e.into()),
        };
        if packet.track_id() != id {
            continue;
        }
        let data = decoder.decode(&packet)?;
        let spec = *data.spec();
        let channels = spec.channels.count();
        ensure!(
            (1..=32).contains(&channels) && spec.rate <= 384000,
            "unsafe audio dimensions"
        );
        let mut samples = SampleBuffer::<f32>::new(data.capacity() as u64, spec);
        samples.copy_interleaved_ref(data);
        mono.clear();
        for frame in samples.samples().chunks_exact(channels) {
            let sample = frame.iter().sum::<f32>() / channels as f32;
            if !sample.is_finite() {
                bail!("nonfinite decoded sample")
            }
            mono.push(sample);
        }
        consume(spec.rate, &mono)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::Write, path::PathBuf};

    fn scratch(name: &str, bytes: &[u8]) -> File {
        let path: PathBuf = std::env::temp_dir().join(format!(
            "ma-decoder-{}-{}-{name}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        File::create(&path).unwrap().write_all(bytes).unwrap();
        let file = File::open(&path).unwrap();
        // The open handle keeps the content readable; unlinking is best effort on Windows.
        let _ = std::fs::remove_file(&path);
        file
    }

    fn wav(rate: u32, samples: &[i16]) -> Vec<u8> {
        let data_len = (samples.len() * 2) as u32;
        let mut out = Vec::new();
        out.extend_from_slice(b"RIFF");
        out.extend_from_slice(&(36 + data_len).to_le_bytes());
        out.extend_from_slice(b"WAVEfmt ");
        out.extend_from_slice(&16u32.to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&rate.to_le_bytes());
        out.extend_from_slice(&(rate * 2).to_le_bytes());
        out.extend_from_slice(&2u16.to_le_bytes());
        out.extend_from_slice(&16u16.to_le_bytes());
        out.extend_from_slice(b"data");
        out.extend_from_slice(&data_len.to_le_bytes());
        for sample in samples {
            out.extend_from_slice(&sample.to_le_bytes());
        }
        out
    }

    #[test]
    fn size_bound_is_inclusive() {
        assert!(ensure_import_size(MAX_IMPORT_BYTES).is_ok());
        assert!(ensure_import_size(MAX_IMPORT_BYTES + 1).is_err());
    }

    #[test]
    fn decodes_every_sample_of_a_valid_file_at_its_rate() {
        let source: Vec<i16> = (0..4800).map(|n| ((n % 100) as i16 - 50) * 100).collect();
        let (mut rate, mut count) = (0, 0usize);
        decode(scratch("ok.wav", &wav(16000, &source)), |r, frames| {
            rate = r;
            count += frames.len();
            Ok(())
        })
        .unwrap();
        assert_eq!((rate, count), (16000, source.len()));
    }

    #[test]
    fn rejects_garbage_and_empty_files_without_consuming_audio() {
        for (name, bytes) in [("empty.bin", Vec::new()), ("junk.bin", vec![0x5a; 4096])] {
            let mut consumed = false;
            let result = decode(scratch(name, &bytes), |_, _| {
                consumed = true;
                Ok(())
            });
            assert!(result.is_err(), "{name} must be rejected");
            assert!(!consumed, "{name} must not yield samples");
        }
    }

    #[test]
    fn consumer_error_aborts_decode() {
        let source = vec![1000i16; 8000];
        let result = decode(scratch("abort.wav", &wav(8000, &source)), |_, _| {
            anyhow::bail!("cancelled")
        });
        assert_eq!(result.unwrap_err().to_string(), "cancelled");
    }
}
