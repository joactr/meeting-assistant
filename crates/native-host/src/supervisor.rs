//! Authenticated child process supervision using inherited anonymous stdio pipes.
use anyhow::{bail, ensure, Context, Result};
use rand::RngCore;
use serde::Serialize;
use serde_json::{json, Value};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    process::{Child, Command},
    sync::Mutex,
};
#[derive(Clone)]
pub struct Launch {
    pub executable: PathBuf,
    pub arguments: Vec<String>,
    pub workspace: PathBuf,
    pub build_id: String,
    pub host_port: u16,
}
pub type Ready = crate::contracts::ReadyFrame;
pub struct Engine {
    pub ready: Ready,
    token: zeroize::Zeroizing<String>,
    child: Mutex<Child>,
    client: reqwest::Client,
    #[cfg(windows)]
    _job: Job,
    #[cfg(unix)]
    _keepalive: Mutex<tokio::process::ChildStdin>,
}
impl Engine {
    pub async fn launch(launch: &Launch, token: zeroize::Zeroizing<String>) -> Result<Arc<Self>> {
        ensure!(launch.executable.is_absolute()&&launch.executable.is_file(),"bundled engine executable missing; configure an absolute developer executable explicitly");
        #[cfg(windows)]
        let mut cmd = Command::new(&launch.executable);
        #[cfg(windows)]
        cmd.args(&launch.arguments).arg("--host-stdio");
        #[cfg(unix)]
        let mut cmd = {
            let mut c = Command::new(std::env::current_exe()?);
            c.arg("--engine-supervisor");
            c
        };
        cmd.env_clear();
        for key in [
            "SYSTEMROOT",
            "WINDIR",
            // NVML (nvidia-smi, CUDA runtimes) fails to initialize without these.
            "ProgramFiles",
            "ProgramW6432",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "LANG",
            "LC_ALL",
            // Engine accepts this path only for first-party, identity-less developer builds.
            // It is a separate private CLI registry, never a workspace/model import grant.
            "MEETING_ASSISTANT_DEVELOPER_WORKER_REGISTRY",
        ] {
            if let Some(value) = std::env::var_os(key) {
                cmd.env(key, value);
            }
        }
        if launch.arguments.iter().any(|s| s == "meeting_assistant") {
            if let Some(path) = std::env::var_os("PYTHONPATH") {
                cmd.env("PYTHONPATH", path);
            }
        }
        cmd.env("PYANNOTE_METRICS_ENABLED", "0")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .kill_on_drop(true);
        #[cfg(unix)]
        unsafe {
            cmd.pre_exec(|| {
                if libc::setpgid(0, 0) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        #[cfg(windows)]
        {
            cmd.creation_flags(0x00000004 | 0x08000000);
        }
        let mut child = cmd.spawn().context("starting bundled engine")?;
        #[cfg(windows)]
        let job = Job::assign(&child)?;
        #[cfg(windows)]
        unsafe {
            #[link(name = "ntdll")]
            unsafe extern "system" {
                fn NtResumeProcess(process: *mut core::ffi::c_void) -> i32;
            }
            ensure!(
                NtResumeProcess(
                    child
                        .raw_handle()
                        .context("engine process handle missing")?
                ) >= 0,
                "could not resume supervised engine"
            );
        }
        let frame = json!({"token":token.as_str(),"protocol_major":1,"build_id":launch.build_id,"workspace":launch.workspace,"host_port":launch.host_port});
        #[cfg(unix)]
        let frame = {
            let mut frame = frame;
            frame["engine_executable"] = json!(launch.executable);
            frame["engine_arguments"] = json!(launch.arguments);
            frame
        };
        let mut input = child.stdin.take().context("launch input pipe")?;
        input.write_all(&serde_json::to_vec(&frame)?).await?;
        input.write_all(b"\n").await?;
        input.flush().await?;
        #[cfg(windows)]
        input.shutdown().await?;
        let stdout = child.stdout.take().context("ready pipe")?;
        let mut reader = BufReader::new(stdout);
        let ready = tokio::time::timeout(Duration::from_secs(120), async {
            let mut bytes = Vec::with_capacity(1024);
            loop {
                let mut byte = [0];
                tokio::io::AsyncReadExt::read_exact(&mut reader, &mut byte).await?;
                if byte[0] == b'\n' {
                    break;
                }
                ensure!(bytes.len() < 65536, "oversize ready frame");
                bytes.push(byte[0]);
            }
            Ok::<Ready, anyhow::Error>(serde_json::from_slice(&bytes)?)
        })
        .await??;
        ensure!(
            ready.protocol_major == 1
                && ready.build_id == launch.build_id
                && ready.pid > 0
                && ready.http_port > 0
                && ready.audio_port > 0,
            "incompatible bundled engine readiness"
        );
        #[cfg(windows)]
        ensure!(
            job.contains(ready.pid)?,
            "readiness PID escaped the supervised engine Job Object"
        );
        #[cfg(unix)]
        unsafe {
            ensure!(
                libc::getpgid(ready.pid as i32) == child.id().unwrap_or(0) as i32,
                "engine escaped supervised process group"
            );
        }
        tokio::spawn(async move {
            let mut bytes = [0u8; 4096];
            loop {
                match tokio::io::AsyncReadExt::read(&mut reader, &mut bytes).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
            }
        });
        Ok(Arc::new(Self {
            ready,
            token,
            child: Mutex::new(child),
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(30))
                .build()?,
            #[cfg(windows)]
            _job: job,
            #[cfg(unix)]
            _keepalive: Mutex::new(input),
        }))
    }
    pub fn fresh_token() -> zeroize::Zeroizing<String> {
        let mut bytes = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut bytes);
        zeroize::Zeroizing::new(hex::encode(bytes))
    }
    pub async fn request(&self, command: &crate::ApplicationCommand) -> Result<Value> {
        ensure!(
            crate::operation_allowed(&command.operation_id),
            "operation is not in generated command allowlist"
        );
        self.internal(&command.operation_id, &command.payload).await
    }
    pub async fn internal<P: Serialize + ?Sized>(
        &self,
        operation: &str,
        payload: &P,
    ) -> Result<Value> {
        ensure!(
            operation
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "._".contains(c)),
            "invalid operation identifier"
        );
        let mut request = self
            .client
            .post(format!(
                "http://127.0.0.1:{}/api/v1/commands/{operation}",
                self.ready.http_port
            ))
            .bearer_auth(self.token.as_str())
            .json(payload);
        if operation == "meetings.start" {
            // Start may cold-load the local transcription worker (startup limit 120s plus the
            // runtime check); the client-wide 30s cut it off and the UI saw a transport error.
            request = request.timeout(Duration::from_secs(180));
        }
        let mut response = request.send().await?;
        let status = response.status();
        ensure!(
            response.content_length().unwrap_or(0) <= 16 * 1024 * 1024,
            "engine response too large"
        );
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            ensure!(
                bytes.len() + chunk.len() <= 16 * 1024 * 1024,
                "engine response too large"
            );
            bytes.extend_from_slice(&chunk);
        }
        let value: Value = serde_json::from_slice(&bytes)?;
        if !status.is_success() {
            if let Ok(error) = serde_json::from_value::<crate::NativeError>(value.clone()) {
                return Err(error.into());
            }
            bail!("engine rejected operation without a valid error envelope")
        }
        Ok(value)
    }
    pub async fn heartbeat(&self) -> bool {
        let Ok(mut response) = self
            .client
            .get(format!(
                "http://127.0.0.1:{}/api/v1/health",
                self.ready.http_port
            ))
            .bearer_auth(self.token.as_str())
            .timeout(Duration::from_secs(2))
            .send()
            .await
        else {
            return false;
        };
        let healthy = response.status().is_success();
        // Dropping an unread body resets the Windows loopback connection.
        loop {
            match response.chunk().await {
                Ok(Some(_)) => {}
                Ok(None) => return healthy,
                Err(_) => return false,
            }
        }
    }
    pub fn token(&self) -> &str {
        self.token.as_str()
    }
    pub async fn verify_exit(&self) -> Result<()> {
        let mut child = self.child.lock().await;
        tokio::time::timeout(Duration::from_secs(10), child.wait())
            .await
            .context("engine did not exit after safe shutdown")??;
        Ok(())
    }
    pub async fn terminate(&self) -> Result<()> {
        let mut child = self.child.lock().await;
        #[cfg(unix)]
        if let Some(pid) = child.id() {
            unsafe {
                libc::kill(-(pid as i32), libc::SIGTERM);
            }
        }
        child.kill().await?;
        child.wait().await?;
        Ok(())
    }
}
#[cfg(windows)]
struct Job(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
unsafe impl Send for Job {}
#[cfg(windows)]
unsafe impl Sync for Job {}
#[cfg(windows)]
impl Job {
    fn assign(child: &Child) -> Result<Self> {
        use windows_sys::Win32::System::JobObjects::*;
        let process = child
            .raw_handle()
            .context("engine process handle missing")?;
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            ensure!(!job.is_null(), "creating engine Job Object failed");
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as _,
                std::mem::size_of_val(&info) as u32,
            ) == 0
                || AssignProcessToJobObject(job, process) == 0
            {
                windows_sys::Win32::Foundation::CloseHandle(job);
                bail!("assigning engine Job Object failed")
            }
            Ok(Self(job))
        }
    }
    fn contains(&self, pid: u32) -> Result<bool> {
        use windows_sys::Win32::{
            Foundation::CloseHandle,
            System::{
                JobObjects::IsProcessInJob,
                Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
            },
        };
        unsafe {
            let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            ensure!(
                !process.is_null(),
                "opening engine readiness process failed: {}",
                std::io::Error::last_os_error()
            );
            let mut contained = 0;
            if IsProcessInJob(process, self.0, &mut contained) == 0 {
                let error = std::io::Error::last_os_error();
                CloseHandle(process);
                return Err(error).context("checking engine Job Object membership");
            }
            CloseHandle(process);
            Ok(contained != 0)
        }
    }
}
#[cfg(windows)]
impl Drop for Job {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}
/// Unix watchdog holds the parent-liveness pipe. EOF after a host crash kills the
/// entire inherited process group, including engine inference descendants.
#[cfg(unix)]
pub fn unix_watchdog() -> Result<()> {
    use std::io::{BufRead, Read, Write};
    let mut parent = std::io::BufReader::new(std::io::stdin());
    let mut frame = String::new();
    parent.by_ref().take(65537).read_line(&mut frame)?;
    ensure!(frame.len() <= 65536, "launch frame exceeds limit");
    let mut value: Value = serde_json::from_str(&frame)?;
    let path = PathBuf::from(
        value["engine_executable"]
            .as_str()
            .context("supervised engine path missing")?,
    );
    ensure!(
        path.is_absolute() && path.is_file(),
        "invalid supervised executable"
    );
    let arguments: Vec<String> = serde_json::from_value(value["engine_arguments"].take())?;
    value.as_object_mut().unwrap().remove("engine_executable");
    value.as_object_mut().unwrap().remove("engine_arguments");
    let mut command = std::process::Command::new(path);
    command
        .args(arguments)
        .arg("--host-stdio")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::inherit());
    let mut child = command.spawn()?;
    let mut input = child.stdin.take().context("child launch pipe")?;
    writeln!(input, "{}", value)?;
    drop(input);
    std::thread::spawn(move || {
        let mut byte = [0u8; 1];
        loop {
            match parent.read(&mut byte) {
                Ok(0) | Err(_) => {
                    unsafe {
                        libc::kill(-libc::getpgrp(), libc::SIGKILL);
                    }
                    break;
                }
                Ok(_) => {}
            }
        }
    });
    let mut output = child.stdout.take().context("child ready pipe")?;
    std::thread::spawn(move || {
        let _ = std::io::copy(&mut output, &mut std::io::stdout());
    });
    let status = child.wait()?;
    ensure!(status.success(), "engine process failed");
    Ok(())
}
