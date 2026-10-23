//! Native save-grant writer. Only private staging inputs or bounded small content.
use anyhow::{ensure, Result};
use base64::Engine as _;
use serde_json::{json, Value};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::Path,
};
pub fn save(root: &Path, destination: &Path, payload: &Value) -> Result<Value> {
    let relative = payload["source_relative_path"].as_str();
    ensure!(
        relative.is_some() != payload["content_base64"].is_string(),
        "Choose exactly one staged source or bounded encoded content"
    );
    let part = destination.with_extension(format!("{}.part", uuid::Uuid::new_v4()));
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&part)?;
    let result = (|| {
        if let Some(relative) = relative {
            let path = Path::new(relative);
            let parts: Vec<_> = path.components().collect();
            ensure!(
                parts.len() >= 2
                    && parts.len() <= 6
                    && parts[0].as_os_str() == "staging"
                    && parts
                        .iter()
                        .all(|p| matches!(p, std::path::Component::Normal(_))),
                "Export source must be a normal private staging path"
            );
            let source = root.join(path).canonicalize()?;
            ensure!(
                source.starts_with(root.join("staging").canonicalize()?) && source.is_file(),
                "Export source escaped private staging"
            );
            let mut input = File::open(source)?;
            ensure!(
                input.metadata()?.len() <= 100 * 1024 * 1024 * 1024,
                "Export exceeds workspace bound"
            );
            std::io::copy(&mut input, &mut output)?;
        } else {
            let encoded = payload["content_base64"].as_str().unwrap();
            ensure!(
                encoded.len() <= 700_000,
                "Encoded export exceeds small-content limit; use streamed staging source"
            );
            let bytes = base64::engine::general_purpose::STANDARD.decode(encoded)?;
            ensure!(
                bytes.len() <= 512 * 1024,
                "Encoded export exceeds small-content limit"
            );
            output.write_all(&bytes)?;
        }
        output.sync_all()?;
        Ok::<_, anyhow::Error>(())
    })();
    drop(output);
    if let Err(error) = result {
        let _ = std::fs::remove_file(&part);
        return Err(error);
    }
    native_media::durable_rename(&part, destination)?;
    let (hash, bytes) = native_media::hash_file(destination)?;
    Ok(
        json!({"saved":true,"bytes":bytes,"sha256":hash,"filename":destination.file_name().unwrap_or_default().to_string_lossy()}),
    )
}
