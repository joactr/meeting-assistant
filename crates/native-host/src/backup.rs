//! Bounded streaming passphrase-encrypted portable backups using reviewed age.
use anyhow::{ensure, Result};
use serde_json::{json, Value};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};
fn staging(root: &Path, relative: &str, existing: bool) -> Result<PathBuf> {
    let path = Path::new(relative);
    let parts: Vec<_> = path.components().collect();
    ensure!(
        parts.len() == 2 && parts[0].as_os_str() == "staging",
        "only private staging files are accessible"
    );
    let name = parts[1]
        .as_os_str()
        .to_str()
        .ok_or_else(|| anyhow::anyhow!("invalid staging path"))?;
    let id = name
        .strip_suffix(".backup")
        .ok_or_else(|| anyhow::anyhow!("invalid staging filename"))?;
    uuid::Uuid::parse_str(id)?;
    let directory = root.join("staging");
    crate::private_directory(&directory)?;
    let output = directory.join(name);
    if existing {
        ensure!(
            output
                .canonicalize()?
                .starts_with(directory.canonicalize()?),
            "staging escaped private workspace"
        );
    }
    Ok(output)
}
pub fn encrypt(root: &Path, input: &str, destination: &Path, password: &str) -> Result<Value> {
    let source = staging(root, input, true)?;
    let mut input = File::open(source)?;
    let part = destination.with_extension(format!("{}.part", uuid::Uuid::new_v4()));
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&part)?;
    let result = (|| {
        let encryptor = age::Encryptor::with_user_passphrase(age::secrecy::SecretString::from(
            password.to_owned(),
        ));
        let mut encrypted = encryptor.wrap_output(&mut output)?;
        std::io::copy(&mut input, &mut encrypted)?;
        encrypted.finish()?;
        output.sync_all()?;
        Ok::<_, anyhow::Error>(())
    })();
    if let Err(error) = result {
        drop(output);
        let _ = std::fs::remove_file(&part);
        return Err(error);
    }
    drop(output);
    std::fs::rename(&part, destination)?;
    let (hash, bytes) = native_media::hash_file(destination)?;
    Ok(json!({"bytes":bytes,"sha256":hash,"encrypted":true}))
}
pub fn decrypt(root: &Path, input: &Path, relative: &str, password: &str) -> Result<Value> {
    let destination = staging(root, relative, false)?;
    let file = File::open(input)?;
    let decryptor = age::Decryptor::new(std::io::BufReader::new(file))?;
    ensure!(
        decryptor.is_scrypt(),
        "backup is not passphrase-encrypted age data"
    );
    let identity =
        age::scrypt::Identity::new(age::secrecy::SecretString::from(password.to_owned()));
    let mut decrypted = decryptor.decrypt(std::iter::once(&identity as &dyn age::Identity))?;
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&destination)?;
    let mut buffer = [0u8; 65536];
    let mut size = 0u64;
    let result = (|| {
        loop {
            let n = decrypted.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            size += n as u64;
            ensure!(
                size <= 100 * 1024 * 1024 * 1024,
                "decrypted backup exceeds workspace bound"
            );
            output.write_all(&buffer[..n])?;
        }
        output.sync_all()?;
        Ok::<_, anyhow::Error>(())
    })();
    if let Err(error) = result {
        drop(output);
        let _ = std::fs::remove_file(&destination);
        return Err(error);
    }
    drop(output);
    let (hash, bytes) = native_media::hash_file(&destination)?;
    Ok(json!({"bytes":bytes,"sha256":hash,"relative_path":relative}))
}
pub fn commit(root: &Path, relative: &str, destination: &Path) -> Result<Value> {
    let source = staging(root, relative, true)?;
    let part = destination.with_extension(format!("{}.part", uuid::Uuid::new_v4()));
    let mut input = File::open(source)?;
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&part)?;
    std::io::copy(&mut input, &mut output)?;
    output.sync_all()?;
    drop(output);
    std::fs::rename(&part, destination)?;
    let (hash, bytes) = native_media::hash_file(destination)?;
    Ok(json!({"bytes":bytes,"sha256":hash}))
}
