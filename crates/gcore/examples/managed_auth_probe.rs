//! Dummy-only native sandbox proof driven by test_srt_envelope_auth.py.

fn main() -> anyhow::Result<()> {
    anyhow::ensure!(std::env::var_os("GOBBY_AGENT_API_TOKEN").is_none());
    let token = gobby_core::local_token::read_api_key_for(std::path::Path::new("/unavailable"))?;
    let response = ureq::get(&format!("{}/rust", std::env::var("GOBBY_DAEMON_URL")?))
        .set("Authorization", &format!("Bearer {token}"))
        .call()?;
    anyhow::ensure!(response.status() == 200);
    println!("rust-auth-ok");
    Ok(())
}
