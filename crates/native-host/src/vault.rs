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
    /// Windows Credential Manager caps a value at 2560 bytes (1280 UTF-16 units); OAuth token
    /// responses are larger, so long values are split across `reference#n` entries and the main
    /// entry holds only the part count. Short values keep the original single-entry form.
    pub fn store(&self, reference: &str, value: &str) -> Result<()> {
        ensure!(value.len() <= 256 * 1024, "secret too large");
        let old = self.parts(reference)?;
        let mut chunks = vec![Zeroizing::new(String::new())];
        let mut units = 0;
        for c in value.chars() {
            if units + c.len_utf16() > 1200 {
                chunks.push(Zeroizing::new(String::new()));
                units = 0;
            }
            units += c.len_utf16();
            chunks.last_mut().expect("one chunk").push(c);
        }
        if chunks.len() == 1 {
            self.entry(reference)?.set_password(value)?;
        } else {
            for (index, chunk) in chunks.iter().enumerate() {
                self.part(reference, index + 1)?.set_password(chunk)?;
            }
            self.entry(reference)?
                .set_password(&format!("{CHUNKED}{}", chunks.len()))?;
        }
        let kept = if chunks.len() == 1 { 0 } else { chunks.len() };
        for index in kept + 1..=old {
            Self::forget(self.part(reference, index)?)?;
        }
        Ok(())
    }
    pub fn resolve(&self, reference: &str) -> Result<Zeroizing<String>> {
        let head = Zeroizing::new(self.entry(reference)?.get_password()?);
        if !head.starts_with(CHUNKED) {
            return Ok(head);
        }
        let count: usize = head[CHUNKED.len()..].parse()?;
        let mut value = Zeroizing::new(String::new());
        for index in 1..=count {
            value.push_str(&Zeroizing::new(
                self.part(reference, index)?.get_password()?,
            ));
        }
        Ok(value)
    }
    pub fn delete(&self, reference: &str) -> Result<()> {
        for index in 1..=self.parts(reference)? {
            Self::forget(self.part(reference, index)?)?;
        }
        Self::forget(self.entry(reference)?)
    }
    fn part(&self, reference: &str, index: usize) -> Result<keyring::Entry> {
        self.entry(reference)?;
        Ok(keyring::Entry::new(
            &self.service,
            &format!("{reference}#{index}"),
        )?)
    }
    fn parts(&self, reference: &str) -> Result<usize> {
        match self.entry(reference)?.get_password() {
            Ok(head) => Ok(head
                .strip_prefix(CHUNKED)
                .and_then(|count| count.parse().ok())
                .unwrap_or(0)),
            Err(keyring::Error::NoEntry) => Ok(0),
            Err(e) => Err(e.into()),
        }
    }
    fn forget(entry: keyring::Entry) -> Result<()> {
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
}
const CHUNKED: &str = "\u{1}chunks:";
