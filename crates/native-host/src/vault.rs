//! OS vault only. No plaintext fallback is possible through this broker.
use anyhow::{ensure, Result};
use zeroize::Zeroizing;
#[derive(Clone)]
pub struct Vault {
    service: String,
}
impl Vault {
    pub fn new(service: String) -> Self {
        Self { service }
    }
    fn entry(&self, reference: &str) -> Result<keyring::Entry> {
        let (prefix, id) = reference
            .split_once(':')
            .ok_or_else(|| anyhow::anyhow!("invalid opaque secret reference"))?;
        ensure!(
            ["credential", "oauth", "backup"].contains(&prefix) && reference.len() <= 64,
            "invalid opaque secret reference"
        );
        uuid::Uuid::parse_str(id)?;
        Ok(keyring::Entry::new(&self.service, reference)?)
    }
    pub fn store(&self, reference: &str, value: &str) -> Result<()> {
        ensure!(value.len() <= 256 * 1024, "secret too large");
        self.entry(reference)?.set_password(value)?;
        Ok(())
    }
    pub fn resolve(&self, reference: &str) -> Result<Zeroizing<String>> {
        Ok(Zeroizing::new(self.entry(reference)?.get_password()?))
    }
    pub fn delete(&self, reference: &str) -> Result<()> {
        match self.entry(reference)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
}
