//! Front-door TLS: load an operator PEM pair or generate a self-signed one
//! once, build the rustls server config, and build the pinned client config a
//! remote peer dials with.
//!
//! [`server_config`] is the only place the server config is built, so a future
//! client-certificate verifier attaches there.

use std::fs;
use std::io::Write;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result};
use gobby_core::bootstrap::{TlsBootstrap, TlsMode};
use sha2::{Digest, Sha256};
use tokio_rustls::rustls::client::verify_server_name;
use tokio_rustls::rustls::crypto::{CryptoProvider, ring};
use tokio_rustls::rustls::pki_types::pem::PemObject;
use tokio_rustls::rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName};
use tokio_rustls::rustls::server::ParsedCertificate;
use tokio_rustls::rustls::{ClientConfig, RootCertStore, ServerConfig};

/// Lifetime of a generated certificate.
const SELF_SIGNED_DAYS: i64 = 3650;

/// A loaded front-door certificate, ready to serve.
pub struct LoadedTls {
    pub config: Arc<ServerConfig>,
    /// `sha256:` plus 64 lowercase hex digits over the leaf certificate's DER.
    pub fingerprint: String,
    /// `tls.sans` entries a reused self-signed certificate does not cover.
    pub missing_sans: Vec<String>,
}

/// Load (or, in `self-signed` mode with neither file present, first generate)
/// the front-door certificate. `Ok(None)` means `tls.mode: off`.
///
/// gdaemon never writes over an existing file: a missing half, an unparsable
/// file, or a key that does not match the certificate fails naming the path.
pub fn load(tls: &TlsBootstrap, bind_host: &str) -> Result<Option<LoadedTls>> {
    if tls.mode == TlsMode::Off {
        return Ok(None);
    }
    let cert = expand_home(&tls.cert)?;
    let key = expand_home(&tls.key)?;
    match tls.mode {
        TlsMode::Off | TlsMode::Files => {}
        TlsMode::SelfSigned => match (cert.exists(), key.exists()) {
            (false, false) => {
                let names = self_signed_sans(bind_host, machine_hostname().as_deref(), &tls.sans);
                generate(&cert, &key, &names)?;
            }
            (true, false) => anyhow::bail!(
                "front-door TLS key {} is missing but certificate {} exists; restore the key \
                 or remove both files to regenerate",
                key.display(),
                cert.display()
            ),
            (false, true) => anyhow::bail!(
                "front-door TLS certificate {} is missing but key {} exists; restore the \
                 certificate or remove both files to regenerate",
                cert.display(),
                key.display()
            ),
            (true, true) => {}
        },
    }
    let cert_pem = fs::read(&cert)
        .with_context(|| format!("cannot read front-door TLS certificate {}", cert.display()))?;
    let key_pem = fs::read(&key)
        .with_context(|| format!("cannot read front-door TLS key {}", key.display()))?;
    let config = server_config(&cert_pem, &key_pem).with_context(|| {
        format!(
            "front-door TLS pair {} and {} is unusable",
            cert.display(),
            key.display()
        )
    })?;
    let missing_sans = if tls.mode == TlsMode::SelfSigned {
        uncovered_names(&cert_pem, &tls.sans)?
    } else {
        Vec::new()
    };
    Ok(Some(LoadedTls {
        config: Arc::new(config),
        fingerprint: fingerprint(&cert_pem)?,
        missing_sans,
    }))
}

/// Build the front door's rustls server config from a PEM certificate chain and key.
pub fn server_config(cert_pem: &[u8], key_pem: &[u8]) -> Result<ServerConfig> {
    let certs = CertificateDer::pem_slice_iter(cert_pem)
        .collect::<Result<Vec<_>, _>>()
        .context("certificate PEM does not parse")?;
    anyhow::ensure!(!certs.is_empty(), "certificate PEM holds no certificate");
    let key = PrivateKeyDer::from_pem_slice(key_pem).context("private key PEM does not parse")?;
    let mut config = ServerConfig::builder_with_provider(provider())
        .with_safe_default_protocol_versions()?
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .context("private key does not match the certificate")?;
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(config)
}

/// A client config that trusts exactly the certificate in `pem` and consults
/// no system roots.
pub fn pinned_client_config(pem: &[u8]) -> Result<ClientConfig> {
    let mut roots = RootCertStore::empty();
    roots
        .add(leaf_certificate(pem)?)
        .context("pinned certificate is not a usable trust anchor")?;
    let mut config = ClientConfig::builder_with_provider(provider())
        .with_safe_default_protocol_versions()?
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(config)
}

/// `sha256:` followed by 64 lowercase hex digits of SHA-256 over the leaf
/// certificate's DER bytes.
pub fn fingerprint(pem: &[u8]) -> Result<String> {
    let digest = Sha256::digest(leaf_certificate(pem)?.as_ref());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!("sha256:{hex}"))
}

/// The SAN set of a generated certificate: `localhost`, the hostname,
/// `127.0.0.1`, `::1`, `bind_host` when it is a concrete IP, then every
/// `tls.sans` entry. A wildcard bind adds no enumerated interface address.
pub fn self_signed_sans(bind_host: &str, hostname: Option<&str>, sans: &[String]) -> Vec<String> {
    let bare = bind_host
        .trim()
        .trim_start_matches('[')
        .trim_end_matches(']');
    let concrete_bind = bare
        .parse::<IpAddr>()
        .ok()
        .filter(|ip| !ip.is_unspecified())
        .map(|ip| ip.to_string());
    let candidates = std::iter::once("localhost".to_owned())
        .chain(hostname.map(str::to_owned))
        .chain(["127.0.0.1".to_owned(), "::1".to_owned()])
        .chain(concrete_bind)
        .chain(sans.iter().cloned());
    let mut names: Vec<String> = Vec::new();
    for name in candidates {
        if !names.iter().any(|seen| seen.eq_ignore_ascii_case(&name)) {
            names.push(name);
        }
    }
    names
}

/// This machine's hostname, when the platform reports one.
pub fn machine_hostname() -> Option<String> {
    let name = probe_hostname()?;
    let name = name.trim();
    (!name.is_empty()).then(|| name.to_owned())
}

/// Entries of `names` the leaf certificate in `pem` is not valid for.
pub fn uncovered_names(pem: &[u8], names: &[String]) -> Result<Vec<String>> {
    let leaf = leaf_certificate(pem)?;
    let parsed = ParsedCertificate::try_from(&leaf).context("certificate does not parse")?;
    Ok(names
        .iter()
        .filter(|name| {
            ServerName::try_from(name.as_str()).map_or(true, |server_name| {
                verify_server_name(&parsed, &server_name).is_err()
            })
        })
        .cloned()
        .collect())
}

fn generate(cert: &Path, key: &Path, names: &[String]) -> Result<()> {
    let mut params = rcgen::CertificateParams::new(names.to_vec())
        .context("self-signed certificate names are invalid")?;
    params.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
    let now = time::OffsetDateTime::now_utc();
    params.not_before = now - time::Duration::days(1);
    params.not_after = now + time::Duration::days(SELF_SIGNED_DAYS);
    let key_pair = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256)
        .context("failed to generate the front-door key")?;
    let certificate = params
        .self_signed(&key_pair)
        .context("failed to sign the front-door certificate")?;
    write_owner_only(key, key_pair.serialize_pem().as_bytes())?;
    write_owner_only(cert, certificate.pem().as_bytes())?;
    Ok(())
}

/// Create `path` with mode 0600, refusing to replace an existing file.
fn write_owner_only(path: &Path, contents: &[u8]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("cannot create {}", parent.display()))?;
    }
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(path)
        .with_context(|| format!("cannot create {}", path.display()))?;
    file.write_all(contents)
        .with_context(|| format!("cannot write {}", path.display()))
}

fn leaf_certificate(pem: &[u8]) -> Result<CertificateDer<'static>> {
    CertificateDer::from_pem_slice(pem).context("certificate PEM does not parse")
}

fn provider() -> Arc<CryptoProvider> {
    Arc::new(ring::default_provider())
}

fn expand_home(path: &str) -> Result<PathBuf> {
    match path.strip_prefix("~/") {
        Some(rest) => {
            let home = std::env::var_os("HOME")
                .filter(|home| !home.is_empty())
                .with_context(|| format!("cannot expand {path}: HOME is not set"))?;
            Ok(PathBuf::from(home).join(rest))
        }
        None => Ok(PathBuf::from(path)),
    }
}

#[cfg(unix)]
fn probe_hostname() -> Option<String> {
    let mut buffer = [0_u8; 256];
    // SAFETY: `gethostname` writes at most `buffer.len()` bytes into the
    // buffer lent for the call and nothing else; a longer name is cut and
    // the terminator written below bounds the read either way.
    let status = unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) };
    if status != 0 {
        return None;
    }
    buffer[buffer.len() - 1] = 0;
    std::ffi::CStr::from_bytes_until_nul(&buffer)
        .ok()?
        .to_str()
        .ok()
        .map(str::to_owned)
}

#[cfg(not(unix))]
fn probe_hostname() -> Option<String> {
    std::env::var("COMPUTERNAME").ok()
}
