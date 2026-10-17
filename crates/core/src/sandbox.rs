use anyhow::Result;
use serde::Serialize;
use std::process::Stdio;
use std::time::Duration;
use tokio::process::Command;

const EXEC_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_OUTPUT_BYTES: usize = 64 * 1024;

#[derive(Debug, Serialize)]
pub struct ExecResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
    pub timed_out: bool,
}

pub struct Sandbox;

/// Cap captured output so a chatty command cannot exhaust memory in the caller.
fn truncate(bytes: &[u8]) -> (String, bool) {
    if bytes.len() <= MAX_OUTPUT_BYTES {
        return (String::from_utf8_lossy(bytes).to_string(), false);
    }
    let mut text = String::from_utf8_lossy(&bytes[..MAX_OUTPUT_BYTES]).to_string();
    text.push_str(&format!("\n…[truncated, {} bytes total]", bytes.len()));
    (text, true)
}

impl Sandbox {
    pub async fn execute(cmd: &str, args: &[String]) -> Result<ExecResult> {
        let mut command = Command::new(cmd);
        command
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            // Without this a timed-out child would keep running after the future drops.
            .kill_on_drop(true);

        let output = match tokio::time::timeout(EXEC_TIMEOUT, command.output()).await {
            Ok(Ok(output)) => output,
            Ok(Err(e)) => {
                return Err(anyhow::Error::from(e).context(format!("Failed to run '{cmd}'")));
            }
            Err(_) => {
                return Ok(ExecResult {
                    stdout: String::new(),
                    stderr: format!(
                        "Command timed out after {}s and was killed.",
                        EXEC_TIMEOUT.as_secs()
                    ),
                    exit_code: -1,
                    timed_out: true,
                });
            }
        };

        let (stdout, _) = truncate(&output.stdout);
        let (stderr, _) = truncate(&output.stderr);

        Ok(ExecResult {
            stdout,
            stderr,
            // None means the process was terminated by a signal.
            exit_code: output.status.code().unwrap_or(-1),
            timed_out: false,
        })
    }
}
