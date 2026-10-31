#![forbid(unsafe_code)]
pub mod flow;

#[cfg(feature = "browser")]
use wasm_bindgen::prelude::*;

/// Browser and CLI execute the same bounded integer solver.
#[cfg(feature = "browser")]
#[wasm_bindgen]
pub fn analyze_json(input: &str) -> Result<String, JsValue> {
    let report = flow::analyze(input.as_bytes()).map_err(|e| JsValue::from_str(&e.to_string()))?;
    flow::verify_certificates(input.as_bytes(), &report)
        .map_err(|e| JsValue::from_str(&e.to_string()))?;
    serde_json::to_string(&report).map_err(|_| JsValue::from_str("Report serialization failed"))
}

/// Canonical native-compatible report bytes, after solver-free certificate verification.
#[cfg(feature = "browser")]
#[wasm_bindgen]
pub fn verify_json(input: &str, report: &str) -> Result<String, JsValue> {
    if report.len() > flow::MAX_REPORT_BYTES {
        return Err(JsValue::from_str("Report exceeds resource budget"));
    }
    let decoded: flow::Report =
        serde_json::from_str(report).map_err(|_| JsValue::from_str("Invalid report schema"))?;
    flow::verify_certificates(input.as_bytes(), &decoded)
        .map_err(|e| JsValue::from_str(&e.to_string()))?;
    let mut canonical = serde_json::to_string_pretty(&decoded)
        .map_err(|_| JsValue::from_str("Report serialization failed"))?;
    canonical.push('\n');
    Ok(canonical)
}
