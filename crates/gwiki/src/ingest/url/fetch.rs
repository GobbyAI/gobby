use std::io::Read;
use std::net::{IpAddr, Ipv6Addr, ToSocketAddrs};
use std::time::Duration;

use crate::WikiError;
use crate::ingest::{single_line, text_from_utf8_lossy};

use super::{UrlIngestFailure, UrlSnapshot};

const URL_FETCH_TIMEOUT: Duration = Duration::from_secs(30);
const HTTP_STATUS_BODY_LIMIT_BYTES: u64 = 8 * 1024;
const MAX_REDIRECTS: usize = 10;
const USER_AGENT: &str = "gwiki/0.1";

pub(super) fn fetch_url_snapshot(
    url: &str,
    fetched_at: &str,
) -> Result<UrlSnapshot, UrlIngestFailure> {
    BlockingUrlFetcher::default().fetch(url, fetched_at)
}

#[derive(Debug, Clone)]
struct BlockingUrlFetcher {
    agent: ureq::Agent,
}

impl Default for BlockingUrlFetcher {
    fn default() -> Self {
        Self {
            agent: ureq::AgentBuilder::new()
                .timeout(URL_FETCH_TIMEOUT)
                .redirects(0)
                .build(),
        }
    }
}

impl BlockingUrlFetcher {
    fn fetch(&self, url: &str, fetched_at: &str) -> Result<UrlSnapshot, UrlIngestFailure> {
        validate_fetch_url(url)?;
        validate_resolved_fetch_url(url)?;
        let mut current_url = url.to_string();

        for _ in 0..=MAX_REDIRECTS {
            let response = match self
                .agent
                .get(&current_url)
                .set("User-Agent", USER_AGENT)
                .call()
            {
                Ok(response) => response,
                Err(ureq::Error::Status(status, response)) => {
                    return Err(UrlIngestFailure::http_status(
                        &current_url,
                        status,
                        response,
                    ));
                }
                Err(ureq::Error::Transport(error)) => {
                    return Err(UrlIngestFailure::new(
                        &current_url,
                        "transport_error",
                        error.to_string(),
                    ));
                }
            };

            if (300..400).contains(&response.status()) {
                let location = response.header("Location").ok_or_else(|| {
                    UrlIngestFailure::new(
                        &current_url,
                        "redirect_without_location",
                        format!(
                            "HTTP redirect {} did not include Location",
                            response.status()
                        ),
                    )
                })?;
                let next_url = resolve_redirect_url(&current_url, location)?;
                validate_fetch_url(&next_url)?;
                validate_resolved_fetch_url(&next_url)?;
                current_url = next_url;
                continue;
            }

            let final_url = response.get_url().to_string();
            validate_fetch_url(&final_url)?;
            validate_resolved_fetch_url(&final_url)?;
            let content_type = response.header("content-type").map(ToOwned::to_owned);
            let max_bytes = crate::support::env::max_inbox_item_bytes_from_env();
            if content_length_exceeds_limit(response.header("content-length"), max_bytes) {
                return Err(response_too_large(&final_url, max_bytes));
            }
            let body = read_limited_body(response.into_reader(), max_bytes, &final_url)?;

            return Ok(UrlSnapshot {
                requested_url: url.to_string(),
                final_url,
                fetched_at: fetched_at.to_string(),
                body,
                content_type,
            });
        }

        Err(UrlIngestFailure::new(
            url,
            "too_many_redirects",
            format!("exceeded {MAX_REDIRECTS} URL redirects"),
        ))
    }
}

pub(super) fn content_length_exceeds_limit(content_length: Option<&str>, max_bytes: u64) -> bool {
    content_length
        .and_then(|value| value.trim().parse::<u64>().ok())
        .is_some_and(|length| length > max_bytes)
}

pub(super) fn read_limited_body(
    reader: impl Read,
    max_bytes: u64,
    url: &str,
) -> Result<Vec<u8>, UrlIngestFailure> {
    let mut body = Vec::new();
    reader
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut body)
        .map_err(|error| UrlIngestFailure::new(url, "read_error", error.to_string()))?;
    if u64::try_from(body.len()).unwrap_or(u64::MAX) > max_bytes {
        return Err(response_too_large(url, max_bytes));
    }
    Ok(body)
}

fn response_too_large(url: &str, max_bytes: u64) -> UrlIngestFailure {
    UrlIngestFailure::new(
        url,
        "response_too_large",
        format!("response exceeds GWIKI_MAX_INBOX_ITEM_BYTES limit of {max_bytes} bytes"),
    )
}

impl UrlIngestFailure {
    pub(super) fn new(
        url: impl Into<String>,
        code: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            url: url.into(),
            code: code.into(),
            message: message.into(),
        }
    }

    pub(super) fn from_wiki_error(url: &str, error: WikiError) -> Self {
        Self::new(url, error.code(), error.to_string())
    }

    fn http_status(url: &str, status: u16, response: ureq::Response) -> Self {
        let body =
            match read_limited_body(response.into_reader(), HTTP_STATUS_BODY_LIMIT_BYTES, url) {
                Ok(body) => text_from_utf8_lossy(&body),
                Err(error) => error.message,
            };
        let body = single_line(&body);
        let detail = if body.is_empty() {
            format!("HTTP status {status}")
        } else {
            format!("HTTP status {status}: {}", truncate_message(&body))
        };
        Self::new(url, "http_status", detail)
    }
}

pub(super) fn resolve_redirect_url(
    current_url: &str,
    location: &str,
) -> Result<String, UrlIngestFailure> {
    let base = url::Url::parse(current_url)
        .map_err(|error| UrlIngestFailure::new(current_url, "invalid_url", error.to_string()))?;
    base.join(location)
        .map(|url| url.to_string())
        .map_err(|error| UrlIngestFailure::new(current_url, "invalid_redirect", error.to_string()))
}

fn validate_fetch_url(raw_url: &str) -> Result<(), UrlIngestFailure> {
    let parsed = url::Url::parse(raw_url)
        .map_err(|error| UrlIngestFailure::new(raw_url, "invalid_url", error.to_string()))?;
    if matches!(parsed.scheme(), "http" | "https") {
        Ok(())
    } else {
        Err(UrlIngestFailure::new(
            raw_url,
            "invalid_url",
            format!("unsupported URL scheme `{}`", parsed.scheme()),
        ))
    }
}

fn validate_resolved_fetch_url(raw_url: &str) -> Result<(), UrlIngestFailure> {
    let parsed = url::Url::parse(raw_url)
        .map_err(|error| UrlIngestFailure::new(raw_url, "invalid_url", error.to_string()))?;
    let host = parsed
        .host_str()
        .ok_or_else(|| UrlIngestFailure::new(raw_url, "invalid_url", "URL host is required"))?;
    let port = parsed
        .port_or_known_default()
        .ok_or_else(|| UrlIngestFailure::new(raw_url, "invalid_url", "URL port is required"))?;
    let mut resolved_any = false;
    let addresses = (host, port).to_socket_addrs().map_err(|error| {
        UrlIngestFailure::new(raw_url, "dns_resolution_failed", error.to_string())
    })?;
    for address in addresses {
        resolved_any = true;
        let ip = address.ip();
        if is_disallowed_fetch_ip(ip) && !loopback_fetch_allowed_for_tests(ip) {
            return Err(UrlIngestFailure::new(
                raw_url,
                "disallowed_address",
                format!("URL host resolves to disallowed address {ip}"),
            ));
        }
    }
    if resolved_any {
        Ok(())
    } else {
        Err(UrlIngestFailure::new(
            raw_url,
            "dns_resolution_failed",
            "URL host did not resolve to any addresses",
        ))
    }
}

fn loopback_fetch_allowed_for_tests(ip: IpAddr) -> bool {
    cfg!(debug_assertions)
        && ip.is_loopback()
        && std::env::var_os("GWIKI_ALLOW_LOOPBACK_URL_FETCH_FOR_TESTS").is_some()
}

pub(super) fn is_disallowed_fetch_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            ip.is_private()
                || ip.is_loopback()
                || ip.is_link_local()
                || ip.is_multicast()
                || ip.is_unspecified()
                || ip.octets() == [169, 254, 169, 254]
        }
        IpAddr::V6(ip) => {
            if let Some(mapped) = ip.to_ipv4_mapped() {
                return is_disallowed_fetch_ip(IpAddr::V4(mapped));
            }
            ip.is_loopback()
                || ip.is_unspecified()
                || ip.is_multicast()
                || is_ipv6_unique_local(ip)
                || is_ipv6_unicast_link_local(ip)
        }
    }
}

fn is_ipv6_unique_local(ip: Ipv6Addr) -> bool {
    ip.segments()[0] & 0xfe00 == 0xfc00
}

fn is_ipv6_unicast_link_local(ip: Ipv6Addr) -> bool {
    ip.segments()[0] & 0xffc0 == 0xfe80
}

fn truncate_message(message: &str) -> String {
    const MAX_CHARS: usize = 200;
    let mut chars = message.chars();
    let truncated = chars.by_ref().take(MAX_CHARS).collect::<String>();
    if chars.next().is_some() {
        format!("{truncated}...")
    } else {
        truncated
    }
}
