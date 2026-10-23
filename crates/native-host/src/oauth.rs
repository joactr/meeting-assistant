//! Secretless public-native OAuth with one-use PKCE callbacks and device polling.
use anyhow::{bail, ensure, Result};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use rand::RngCore;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    sync::Mutex,
};
#[derive(Clone)]
pub struct OAuth {
    vault: crate::vault::Vault,
    client: reqwest::Client,
    flows: Arc<Mutex<HashMap<String, Flow>>>,
    events: crate::Events,
    revoked: Arc<Mutex<std::collections::HashSet<String>>>,
}
struct Flow {
    state: Value,
    cancel: Arc<AtomicBool>,
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn random() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}
impl OAuth {
    pub fn new(vault: crate::vault::Vault, events: crate::Events) -> Result<Self> {
        Ok(Self {
            vault,
            events,
            client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(20))
                .build()?,
            flows: Arc::new(Mutex::new(HashMap::new())),
            revoked: Arc::new(Mutex::new(std::collections::HashSet::new())),
        })
    }
    pub async fn poll(&self, id: &str) -> Result<Value> {
        Ok(self
            .flows
            .lock()
            .await
            .get(id)
            .ok_or_else(|| anyhow::anyhow!("unknown OAuth flow"))?
            .state
            .clone())
    }
    pub async fn cancel(&self, id: Option<&str>) -> Result<Value> {
        let flows = self.flows.lock().await;
        for (key, flow) in flows.iter() {
            if id.is_none() || id == Some(key.as_str()) {
                flow.cancel.store(true, Ordering::Release);
            }
        }
        Ok(json!({"state":"cancelled"}))
    }
    async fn finish(&self, id: &str, result: Result<Value>) {
        let mut flows = self.flows.lock().await;
        let cancelled = flows
            .get(id)
            .map(|flow| flow.cancel.load(Ordering::Acquire))
            .unwrap_or(true);
        let value = match result {
            Ok(v) if cancelled => {
                if let Some(reference) = v["secret_ref"].as_str() {
                    let _ = self.vault.delete(reference);
                }
                json!({"flow_id":id,"state":"cancelled"})
            }
            Ok(v) => v,
            Err(e) => {
                json!({"flow_id":id,"state":if cancelled{"cancelled"}else{"failed"},"error":{"code":"oauth_failed","message":e.to_string(),"retryable":false}})
            }
        };
        if let Some(flow) = flows.get_mut(id) {
            flow.state = value.clone();
        }
        drop(flows);
        (self.events)("native.oauth", value);
    }
    fn store_tokens(
        &self,
        provider: &str,
        client_id: &str,
        tenant: &str,
        mut value: Value,
    ) -> Result<Value> {
        ensure!(
            value.get("access_token").and_then(Value::as_str).is_some(),
            "OAuth token response has no access token"
        );
        let reference = format!("oauth:{}", uuid::Uuid::new_v4());
        let expires = now()
            + value
                .get("expires_in")
                .and_then(Value::as_u64)
                .unwrap_or(3600);
        value["expires_at"] = json!(expires);
        value["provider"] = json!(provider);
        value["client_id"] = json!(client_id);
        value["tenant"] = json!(tenant);
        let scope = value.get("scope").cloned().unwrap_or(json!(""));
        self.vault.store(&reference, &value.to_string())?;
        Ok(json!({"state":"complete","secret_ref":reference,"expires_at":expires,"scopes":scope}))
    }
    pub async fn authorize(&self, payload: Value) -> Result<Value> {
        let provider = payload["provider"].as_str().unwrap_or("").to_owned();
        ensure!(
            ["google", "microsoft"].contains(&provider.as_str()),
            "unsupported native OAuth provider"
        );
        let client_id = payload["client_id"].as_str().unwrap_or("").to_owned();
        ensure!(
            !client_id.is_empty() && client_id.len() <= 256,
            "public client registration ID required"
        );
        let scopes: Vec<String> = serde_json::from_value(payload["scopes"].clone())?;
        let allowed = if provider == "google" {
            vec![
                "https://www.googleapis.com/auth/calendar.events.readonly",
                "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                "openid",
                "email",
            ]
        } else {
            vec!["Calendars.ReadBasic", "offline_access", "openid", "profile"]
        };
        ensure!(
            !scopes.is_empty() && scopes.iter().all(|s| allowed.contains(&s.as_str())),
            "OAuth scopes exceed calendar read capability"
        );
        let tenant = payload["tenant"].as_str().unwrap_or("common").to_owned();
        ensure!(
            tenant.len() <= 128
                && tenant
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-'),
            "invalid Microsoft tenant"
        );
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await?;
        let redirect = format!(
            "http://127.0.0.1:{}/callback",
            listener.local_addr()?.port()
        );
        let verifier = random();
        let state = random();
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        let endpoint = if provider == "google" {
            "https://accounts.google.com/o/oauth2/v2/auth".to_owned()
        } else {
            format!("https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize")
        };
        let mut url = url::Url::parse(&endpoint)?;
        url.query_pairs_mut().extend_pairs([
            ("client_id", client_id.as_str()),
            ("redirect_uri", redirect.as_str()),
            ("response_type", "code"),
            ("scope", scopes.join(" ").as_str()),
            ("state", state.as_str()),
            ("code_challenge", challenge.as_str()),
            ("code_challenge_method", "S256"),
        ]);
        if provider == "google" {
            url.query_pairs_mut()
                .append_pair("access_type", "offline")
                .append_pair("prompt", "consent");
        }
        let id = uuid::Uuid::new_v4().to_string();
        let cancel = Arc::new(AtomicBool::new(false));
        let initial = json!({"flow_id":id,"state":"pending","expires_at":now()+180});
        self.flows.lock().await.insert(
            id.clone(),
            Flow {
                state: initial.clone(),
                cancel: cancel.clone(),
            },
        );
        webbrowser::open(url.as_str())?;
        let this = self.clone();
        tokio::spawn(async move {
            let result=async{
   let deadline=tokio::time::Instant::now()+Duration::from_secs(180);let code=loop{ensure!(!cancel.load(Ordering::Acquire),"OAuth cancelled");ensure!(tokio::time::Instant::now()<deadline,"OAuth callback expired");let accepted=tokio::time::timeout(Duration::from_millis(250),listener.accept()).await;let Ok(Ok((mut socket,address)))=accepted else{continue};if !address.ip().is_loopback(){continue}let mut request=[0u8;8192];let n=tokio::time::timeout(Duration::from_secs(3),socket.read(&mut request)).await??;let request=std::str::from_utf8(&request[..n])?;let first=request.lines().next().unwrap_or("");let mut parts=first.split_whitespace();if parts.next()!=Some("GET"){continue}let target=parts.next().unwrap_or("");let callback=url::Url::parse(&format!("http://127.0.0.1{target}"))?;let query:HashMap<String,String>=callback.query_pairs().into_owned().collect();if callback.path()!="/callback"||query.get("state")!=Some(&state){let _=socket.write_all(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n").await;continue}let code=query.get("code").cloned();let body=b"Authorization received. You may close this browser window.";socket.write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len()).as_bytes()).await?;socket.write_all(body).await?;break code.ok_or_else(||anyhow::anyhow!("authorization denied"))?;};drop(listener);
   ensure!(!cancel.load(Ordering::Acquire),"OAuth cancelled before token exchange");let token_url=if provider=="google"{"https://oauth2.googleapis.com/token".to_owned()}else{format!("https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token")};let response=this.client.post(token_url).form(&[("grant_type","authorization_code"),("code",code.as_str()),("client_id",client_id.as_str()),("redirect_uri",redirect.as_str()),("code_verifier",verifier.as_str())]).send().await?;ensure!(response.status().is_success(),"OAuth token exchange denied");let value:Value=response.json().await?;ensure!(!cancel.load(Ordering::Acquire),"OAuth cancelled during token exchange");this.store_tokens(&provider,&client_id,&tenant,value)
  }.await;
            this.finish(&id, result).await;
        });
        Ok(initial)
    }
    pub async fn device(&self, payload: Value) -> Result<Value> {
        ensure!(
            payload["provider"].as_str() == Some("github"),
            "device flow currently supports GitHub only"
        );
        let client_id = payload["client_id"].as_str().unwrap_or("").to_owned();
        ensure!(!client_id.is_empty(), "GitHub public client ID required");
        let response = self
            .client
            .post("https://github.com/login/device/code")
            .header("Accept", "application/json")
            .form(&[("client_id", client_id.as_str())])
            .send()
            .await?;
        ensure!(
            response.status().is_success(),
            "GitHub device authorization denied"
        );
        let value: Value = response.json().await?;
        let device_code = value["device_code"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("device code missing"))?
            .to_owned();
        let interval = value["interval"].as_u64().unwrap_or(5).max(5);
        let expiry = now() + value["expires_in"].as_u64().unwrap_or(900);
        let uri = value["verification_uri"].as_str().unwrap_or("");
        ensure!(
            crate::validate_external_https(uri)?.host_str() == Some("github.com"),
            "invalid device verification URL"
        );
        let id = uuid::Uuid::new_v4().to_string();
        let cancel = Arc::new(AtomicBool::new(false));
        let initial = json!({"flow_id":id,"state":"pending","verification_uri":uri,"user_code":value["user_code"],"interval":interval,"expires_at":expiry});
        self.flows.lock().await.insert(
            id.clone(),
            Flow {
                state: initial.clone(),
                cancel: cancel.clone(),
            },
        );
        (self.events)("native.oauth", initial.clone());
        webbrowser::open(uri)?;
        let this = self.clone();
        tokio::spawn(async move {
            let result = async {
                let mut interval = interval;
                loop {
                    ensure!(
                        !cancel.load(Ordering::Acquire),
                        "GitHub authorization cancelled"
                    );
                    ensure!(now() < expiry, "GitHub authorization expired");
                    tokio::time::sleep(Duration::from_secs(interval)).await;
                    ensure!(
                        !cancel.load(Ordering::Acquire),
                        "GitHub authorization cancelled before polling"
                    );
                    let response = this
                        .client
                        .post("https://github.com/login/oauth/access_token")
                        .header("Accept", "application/json")
                        .form(&[
                            ("client_id", client_id.as_str()),
                            ("device_code", device_code.as_str()),
                            ("grant_type", "urn:ietf:params:oauth:grant-type:device_code"),
                        ])
                        .send()
                        .await?;
                    let value: Value = response.json().await?;
                    ensure!(
                        !cancel.load(Ordering::Acquire),
                        "GitHub authorization cancelled during polling"
                    );
                    match value["error"].as_str() {
                        Some("authorization_pending") => continue,
                        Some("slow_down") => {
                            interval += 5;
                            continue;
                        }
                        Some("access_denied") => bail!("GitHub authorization denied"),
                        Some("expired_token") => bail!("GitHub authorization expired"),
                        Some(_) => bail!("GitHub device authorization failed"),
                        None => return this.store_tokens("github", &client_id, "", value),
                    }
                }
            }
            .await;
            this.finish(&id, result).await;
        });
        Ok(initial)
    }
    pub async fn refresh(&self, reference: &str) -> Result<Value> {
        {
            let revoked = self.revoked.lock().await;
            ensure!(
                !revoked.contains(reference),
                "Credential revoked: reconnect required"
            );
        }
        let secret = self.vault.resolve(reference)?;
        let old: Value = serde_json::from_str(secret.as_str())?;
        let provider = old["provider"].as_str().unwrap_or("");
        let client = old["client_id"].as_str().unwrap_or("");
        let tenant = old["tenant"].as_str().unwrap_or("common");
        let refresh = old["refresh_token"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("No refresh token: reconnect required"))?;
        let endpoint = match provider {
            "google" => "https://oauth2.googleapis.com/token".to_owned(),
            "microsoft" => format!("https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"),
            "github" => "https://github.com/login/oauth/access_token".into(),
            _ => bail!("Unsupported token provider"),
        };
        let response = self
            .client
            .post(endpoint)
            .header("Accept", "application/json")
            .form(&[
                ("client_id", client),
                ("grant_type", "refresh_token"),
                ("refresh_token", refresh),
            ])
            .send()
            .await?;
        ensure!(
            response.status().is_success(),
            "Token refresh denied: reconnect required"
        );
        let mut updated: Value = response.json().await?;
        ensure!(
            updated
                .get("access_token")
                .and_then(Value::as_str)
                .is_some(),
            "Refresh response missing token"
        );
        if updated.get("refresh_token").is_none() {
            updated["refresh_token"] = old["refresh_token"].clone();
        }
        for key in ["provider", "client_id", "tenant"] {
            updated[key] = old[key].clone();
        }
        let expires = now() + updated["expires_in"].as_u64().unwrap_or(3600);
        updated["expires_at"] = json!(expires);
        {
            let revoked = self.revoked.lock().await;
            ensure!(
                !revoked.contains(reference),
                "Credential revoked while refresh was in flight"
            );
            self.vault.resolve(reference)?;
            self.vault.store(reference, &updated.to_string())?;
        }
        Ok(json!({"secret_ref":reference,"expires_at":expires,"state":"complete"}))
    }
    pub async fn revoke(
        &self,
        reference: &str,
        provider: Option<&str>,
        remote: bool,
    ) -> Result<Value> {
        let secret = self.vault.resolve(reference).ok();
        let old: Value = secret
            .as_ref()
            .and_then(|s| serde_json::from_str(s.as_str()).ok())
            .unwrap_or(Value::Null);
        let provider = old["provider"].as_str().or(provider).unwrap_or("unknown");
        {
            let mut revoked = self.revoked.lock().await;
            revoked.insert(reference.to_owned());
            self.vault.delete(reference)?;
        }
        for flow in self.flows.lock().await.values_mut() {
            if flow.state["secret_ref"].as_str() == Some(reference) {
                flow.cancel.store(true, Ordering::Release);
                flow.state = json!({"state":"revoked"});
            }
        }
        if provider == "google" && remote {
            if let Some(token) = old["refresh_token"]
                .as_str()
                .or_else(|| old["access_token"].as_str())
            {
                let response = self
                    .client
                    .post("https://oauth2.googleapis.com/revoke")
                    .form(&[("token", token)])
                    .send()
                    .await;
                return Ok(
                    json!({"local_deleted":true,"provider_revocation":if response.map(|r|r.status().is_success()).unwrap_or(false){"revoked"}else{"failed"},"manage_url":"https://myaccount.google.com/connections"}),
                );
            }
        }
        let manage = match provider {
            "google" => "https://myaccount.google.com/connections",
            "microsoft" => "https://account.live.com/consent/Manage",
            "github" => "https://github.com/settings/applications",
            _ => "",
        };
        Ok(
            json!({"local_deleted":true,"provider_revocation":if manage.is_empty(){"unavailable"}else{"manual"},"manage_url":manage,"reason":if remote{"This public native flow cannot revoke the provider grant without broader privileges; manage provider authorization separately."}else{"Privacy mode blocks provider revocation requests; the local credential was deleted."}}),
        )
    }
}
