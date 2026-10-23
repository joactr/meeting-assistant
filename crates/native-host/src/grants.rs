//! Expiring purpose-bound native selections; paths never cross the WebView bridge.
use anyhow::{ensure, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::PathBuf,
    time::{Duration, Instant},
};
struct Grant {
    path: PathBuf,
    purpose: String,
    expires: Instant,
}
pub struct Grants {
    entries: HashMap<String, Grant>,
}
impl Default for Grants {
    fn default() -> Self {
        Self::new()
    }
}

impl Grants {
    pub fn new() -> Self {
        Self {
            entries: HashMap::new(),
        }
    }
    pub fn add(&mut self, path: PathBuf, purpose: &str) -> Result<Value> {
        ensure!(
            [
                "import_audio",
                "import_model",
                "export",
                "backup",
                "restore",
                "legacy_import"
            ]
            .contains(&purpose),
            "unsupported file grant purpose"
        );
        ensure!(
            path.is_absolute(),
            "native dialog must return absolute path"
        );
        if ["import_audio", "import_model", "restore", "legacy_import"].contains(&purpose) {
            ensure!(path.is_file(), "selected source is not a regular file");
            audio_core::decoder::ensure_import_size(path.metadata()?.len())
                .map_err(|_| anyhow::anyhow!("selected file exceeds size bound"))?;
        }
        let id = uuid::Uuid::new_v4().to_string();
        let name = path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        let bytes = path.metadata().ok().map(|m| m.len());
        self.entries.insert(
            id.clone(),
            Grant {
                path,
                purpose: purpose.into(),
                expires: Instant::now() + Duration::from_secs(600),
            },
        );
        Ok(json!({"grant_id":id,"name":name,"bytes":bytes}))
    }
    pub fn resolve(&mut self, id: &str, purpose: &str) -> Result<PathBuf> {
        self.entries.retain(|_, g| g.expires > Instant::now());
        let grant = self
            .entries
            .get(id)
            .ok_or_else(|| anyhow::anyhow!("file grant expired or unknown: select again"))?;
        ensure!(grant.purpose == purpose, "file grant purpose mismatch");
        Ok(grant.path.clone())
    }
    pub fn write_target(&mut self, id: &str) -> Result<PathBuf> {
        self.entries
            .retain(|_, grant| grant.expires > Instant::now());
        let grant = self.entries.get(id).ok_or_else(|| {
            anyhow::anyhow!("Save grant expired or unknown: choose the destination again")
        })?;
        ensure!(
            ["backup", "export"].contains(&grant.purpose.as_str()),
            "Grant is not an explicit save destination"
        );
        Ok(grant.path.clone())
    }
    pub fn revoke(&mut self, id: &str) {
        self.entries.remove(id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch_file() -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "ma-grant-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ));
        std::fs::write(&path, b"x").unwrap();
        path
    }

    fn grant_id(grants: &mut Grants, path: PathBuf, purpose: &str) -> String {
        grants.add(path, purpose).unwrap()["grant_id"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    #[test]
    fn rejects_unknown_purpose_relative_paths_and_directories() {
        let mut grants = Grants::new();
        let file = scratch_file();
        assert!(grants.add(file.clone(), "shell").is_err());
        assert!(grants
            .add(PathBuf::from("relative.wav"), "import_audio")
            .is_err());
        assert!(grants.add(std::env::temp_dir(), "import_audio").is_err());
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn resolve_is_bound_to_its_purpose() {
        let mut grants = Grants::new();
        let file = scratch_file();
        let id = grant_id(&mut grants, file.clone(), "import_audio");
        assert_eq!(grants.resolve(&id, "import_audio").unwrap(), file);
        assert!(grants.resolve(&id, "import_model").is_err());
        assert!(grants.resolve(&id, "restore").is_err());
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn expired_and_revoked_grants_cannot_be_resolved() {
        let mut grants = Grants::new();
        let file = scratch_file();
        let expired = grant_id(&mut grants, file.clone(), "import_audio");
        grants.entries.get_mut(&expired).unwrap().expires = Instant::now() - Duration::from_secs(1);
        let error = grants
            .resolve(&expired, "import_audio")
            .unwrap_err()
            .to_string();
        assert!(error.contains("expired"), "{error}");
        let revoked = grant_id(&mut grants, file.clone(), "import_audio");
        grants.revoke(&revoked);
        assert!(grants.resolve(&revoked, "import_audio").is_err());
        assert!(grants.resolve("never-issued", "import_audio").is_err());
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn only_save_grants_can_be_written_and_expire_like_reads() {
        let mut grants = Grants::new();
        let file = scratch_file();
        let read = grant_id(&mut grants, file.clone(), "import_audio");
        assert!(grants.write_target(&read).is_err());
        let target = std::env::temp_dir().join("ma-grant-export-target.md");
        let save = grant_id(&mut grants, target.clone(), "export");
        assert_eq!(grants.write_target(&save).unwrap(), target);
        grants.entries.get_mut(&save).unwrap().expires = Instant::now() - Duration::from_secs(1);
        assert!(grants.write_target(&save).is_err());
        std::fs::remove_file(file).unwrap();
    }
}
