#![forbid(unsafe_code)]
use anyhow::{ensure, Result};
use clap::{Parser, Subcommand};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Instant,
};

#[derive(Parser)]
#[command(
    version,
    about = "Temporal flow bounds for analyst-selected funds; no crime inference"
)]
struct Args {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Analyze {
        #[arg(long)]
        input: PathBuf,
        /// A new output file; omit to emit the complete report on stdout.
        #[arg(long)]
        output: Option<PathBuf>,
    },
    Verify {
        #[arg(long)]
        input: PathBuf,
        #[arg(long)]
        report: PathBuf,
        #[arg(long)]
        anchor: String,
        /// Also require byte-identical canonical solver replay.
        #[arg(long)]
        recompute: bool,
    },
}
fn read(path: &Path, limit: usize) -> Result<Vec<u8>> {
    ensure!(
        fs::symlink_metadata(path)?.file_type().is_file(),
        "Expected a regular non-symlink file"
    );
    let mut data = Vec::new();
    File::open(path)?
        .take(limit as u64 + 1)
        .read_to_end(&mut data)?;
    ensure!(data.len() <= limit, "File exceeds resource budget");
    Ok(data)
}
fn analyze(input: &[u8]) -> Result<Vec<u8>> {
    let report = tracebound::flow::analyze(input)?;
    let mut bytes = serde_json::to_vec_pretty(&report)?;
    bytes.push(b'\n');
    ensure!(
        bytes.len() <= 16 * 1024 * 1024,
        "Report exceeds resource budget"
    );
    Ok(bytes)
}
fn main() -> Result<()> {
    match Args::parse().command {
        Command::Analyze { input, output } => {
            let source = read(&input, 2 * 1024 * 1024)?;
            let start = Instant::now();
            let bytes = analyze(&source)?;
            if let Some(path) = output {
                ensure!(!path.exists(), "Refusing to overwrite output");
                let parent = path
                    .parent()
                    .filter(|p| !p.as_os_str().is_empty())
                    .unwrap_or(Path::new("."));
                let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
                tmp.write_all(&bytes)?;
                tmp.as_file().sync_all()?;
                tmp.persist_noclobber(path)?;
                println!(
                    "{}",
                    json!({"report_sha256":hex::encode(Sha256::digest(&bytes)),"elapsed_seconds":start.elapsed().as_secs_f64(),"note":"Retain digest separately. Modeled bounds under declared completeness; not provenance or guilt."})
                );
            } else {
                std::io::stdout().write_all(&bytes)?;
            }
        }
        Command::Verify {
            input,
            report,
            anchor,
            recompute,
        } => {
            ensure!(
                anchor.len() == 64
                    && anchor
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
                "Invalid SHA-256 anchor"
            );
            let bytes = read(&report, 16 * 1024 * 1024)?;
            ensure!(
                hex::encode(Sha256::digest(&bytes)) == anchor,
                "Report differs from trusted anchor"
            );
            let source = read(&input, 2 * 1024 * 1024)?;
            let decoded: tracebound::flow::Report = serde_json::from_slice(&bytes)
                .map_err(|_| anyhow::anyhow!("Invalid report schema"))?;
            tracebound::flow::verify_certificates(&source, &decoded)?;
            if recompute {
                ensure!(
                    bytes == analyze(&source)?,
                    "Report differs from canonical replay with this solver version"
                );
            }
            println!(
                "{}",
                json!({"verified":true,"report_sha256":anchor,
                "method":"feasibility-and-optimality-certificates", "solver_recomputed":recompute})
            );
        }
    }
    Ok(())
}
