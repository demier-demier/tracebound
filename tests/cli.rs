use sha2::{Digest, Sha256};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
use tempfile::TempDir;

fn run(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_tracebound"))
        .args(args)
        .output()
        .unwrap()
}
fn write_case(dir: &Path) -> std::path::PathBuf {
    let path = dir.join("case.json");
    fs::write(
        &path,
        include_bytes!("../examples/commingled-payments.json"),
    )
    .unwrap();
    path
}
fn analyze(dir: &Path) -> (std::path::PathBuf, std::path::PathBuf, String) {
    let input = write_case(dir);
    let report = dir.join("report.json");
    let out = run(&[
        "analyze",
        "--input",
        input.to_str().unwrap(),
        "--output",
        report.to_str().unwrap(),
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let meta: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    (
        input,
        report,
        meta["report_sha256"].as_str().unwrap().into(),
    )
}
#[test]
fn certificate_verification_and_optional_solver_replay() {
    let dir = TempDir::new().unwrap();
    let (i, r, h) = analyze(dir.path());
    for replay in [false, true] {
        let mut args = vec![
            "verify",
            "--input",
            i.to_str().unwrap(),
            "--report",
            r.to_str().unwrap(),
            "--anchor",
            &h,
        ];
        if replay {
            args.push("--recompute");
        }
        let out = run(&args);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(v["solver_recomputed"], replay);
    }
}
#[test]
fn output_never_overwrites_existing_evidence() {
    let dir = TempDir::new().unwrap();
    let (i, r, _) = analyze(dir.path());
    let before = fs::read(&r).unwrap();
    assert!(!run(&[
        "analyze",
        "--input",
        i.to_str().unwrap(),
        "--output",
        r.to_str().unwrap()
    ])
    .status
    .success());
    assert_eq!(before, fs::read(&r).unwrap());
}
#[test]
fn wrong_external_anchor_is_rejected() {
    let dir = TempDir::new().unwrap();
    let (i, r, _) = analyze(dir.path());
    assert!(!run(&[
        "verify",
        "--input",
        i.to_str().unwrap(),
        "--report",
        r.to_str().unwrap(),
        "--anchor",
        &"0".repeat(64)
    ])
    .status
    .success());
}
#[test]
fn tampered_witness_rejected_even_with_rehashed_report() {
    let dir = TempDir::new().unwrap();
    let (i, r, _) = analyze(dir.path());
    let mut v: serde_json::Value = serde_json::from_slice(&fs::read(&r).unwrap()).unwrap();
    v["targets"][0]["min_witness"]["final_allocations"][0]["seeded_minor"] = "999999999".into();
    let bytes = serde_json::to_vec(&v).unwrap();
    let anchor = hex::encode(Sha256::digest(&bytes));
    fs::write(&r, bytes).unwrap();
    assert!(!run(&[
        "verify",
        "--input",
        i.to_str().unwrap(),
        "--report",
        r.to_str().unwrap(),
        "--anchor",
        &anchor
    ])
    .status
    .success());
}
#[test]
fn altered_input_rejected_by_original_report() {
    let dir = TempDir::new().unwrap();
    let (i, r, h) = analyze(dir.path());
    let mut bytes = fs::read(&i).unwrap();
    bytes.push(b' ');
    fs::write(&i, bytes).unwrap();
    assert!(!run(&[
        "verify",
        "--input",
        i.to_str().unwrap(),
        "--report",
        r.to_str().unwrap(),
        "--anchor",
        &h
    ])
    .status
    .success());
}
#[test]
fn unknown_and_duplicate_report_fields_rejected() {
    for extra in [
        "\"unexpected\":true,",
        "\"schema\":\"flow-attribution/v1\",",
    ] {
        let dir = TempDir::new().unwrap();
        let (i, r, _) = analyze(dir.path());
        let raw = fs::read_to_string(&r).unwrap();
        let bytes = raw.replacen('{', &format!("{{{extra}"), 1).into_bytes();
        let h = hex::encode(Sha256::digest(&bytes));
        fs::write(&r, bytes).unwrap();
        assert!(!run(&[
            "verify",
            "--input",
            i.to_str().unwrap(),
            "--report",
            r.to_str().unwrap(),
            "--anchor",
            &h
        ])
        .status
        .success());
    }
}
#[test]
fn oversized_and_malformed_input_fail_without_echoing_values() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("bad.json");
    for bytes in [
        vec![b' '; 2 * 1024 * 1024 + 1],
        b"{\"private-account-secret\":999}".to_vec(),
    ] {
        fs::write(&path, bytes).unwrap();
        let out = run(&["analyze", "--input", path.to_str().unwrap()]);
        assert!(!out.status.success());
        assert!(!String::from_utf8_lossy(&out.stderr).contains("private-account-secret"));
        assert!(out.stdout.is_empty());
    }
}
#[cfg(unix)]
#[test]
fn symlink_inputs_rejected() {
    let dir = TempDir::new().unwrap();
    let input = write_case(dir.path());
    let link = dir.path().join("link.json");
    std::os::unix::fs::symlink(input, &link).unwrap();
    assert!(!run(&["analyze", "--input", link.to_str().unwrap()])
        .status
        .success());
}
