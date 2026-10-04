//! Dial `localhost` service hosts on numeric IPv4 loopback.
//!
//! Resolving the name `localhost` is a real `getaddrinfo` call. Inside the
//! managed SRT sandbox the mDNSResponder lookup is denied, which records a
//! `network-outbound` violation on every connect before `/etc/hosts` answers,
//! and that answer lists `::1` first. In #22412 a `::1` dial self-connected to
//! the hub's own port and hung a `gcode` refresh for five hours. Local Gobby
//! services listen on 127.0.0.1 or a wildcard, so clients dial that address
//! directly. AI and embedding clients, which serve user-configured local
//! endpoints, fall back to ::1 when 127.0.0.1 refuses. Configured hosts stay
//! as written for identity, TLS names and `Host` headers; only the dial
//! address changes.

use std::net::{IpAddr, Ipv4Addr};

/// The address a literal `localhost` host is dialed on.
pub const LOCALHOST_DIAL_ADDR: IpAddr = IpAddr::V4(Ipv4Addr::LOCALHOST);

/// True when `host` is the name `localhost`, in any case.
pub fn is_localhost(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
}

/// Dial host for `host`: `127.0.0.1` for `localhost`, anything else unchanged.
pub fn numeric_loopback_host(host: &str) -> &str {
    if is_localhost(host) {
        "127.0.0.1"
    } else {
        host
    }
}

/// Make a blocking client dial `localhost` URLs on 127.0.0.1 without a name
/// lookup. URL ports still apply and requests keep their `Host` header.
#[cfg(any(feature = "ai", feature = "qdrant"))]
pub fn dial_localhost_as_loopback(
    builder: reqwest::blocking::ClientBuilder,
) -> reqwest::blocking::ClientBuilder {
    builder.resolve(
        "localhost",
        std::net::SocketAddr::new(LOCALHOST_DIAL_ADDR, 0),
    )
}

/// Make a blocking client dial `localhost` URLs on 127.0.0.1, then ::1,
/// without a name lookup. For clients of user-configured local endpoints,
/// which may listen on either loopback.
#[cfg(any(feature = "ai", feature = "qdrant"))]
pub fn dial_localhost_on_either_loopback(
    builder: reqwest::blocking::ClientBuilder,
) -> reqwest::blocking::ClientBuilder {
    use std::net::{Ipv6Addr, SocketAddr};

    builder.resolve_to_addrs(
        "localhost",
        &[
            SocketAddr::new(LOCALHOST_DIAL_ADDR, 0),
            SocketAddr::new(IpAddr::V6(Ipv6Addr::LOCALHOST), 0),
        ],
    )
}

/// Give a PostgreSQL config with `localhost` hosts a numeric `hostaddr` for
/// each host, keeping `host` for TLS and diagnostics. Explicit `hostaddr`
/// values win, and a host list that still names another host is left alone.
#[cfg(feature = "postgres")]
pub fn dial_postgres_localhost_as_loopback(config: &mut postgres::Config) {
    use postgres::config::Host;

    let hosts = config.get_hosts();
    if !config.get_hostaddrs().is_empty()
        || !hosts
            .iter()
            .any(|host| matches!(host, Host::Tcp(name) if is_localhost(name)))
    {
        return;
    }
    let addrs: Option<Vec<IpAddr>> = hosts
        .iter()
        .map(|host| match host {
            Host::Tcp(name) => numeric_loopback_host(name).parse().ok(),
            #[cfg(unix)]
            Host::Unix(_) => None,
        })
        .collect();
    for addr in addrs.into_iter().flatten() {
        config.hostaddr(addr);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn localhost_in_any_case_dials_numeric_loopback() {
        for host in ["localhost", "LOCALHOST", "LocalHost"] {
            assert_eq!(numeric_loopback_host(host), "127.0.0.1", "{host}");
        }
    }

    #[test]
    fn other_hosts_are_returned_unchanged() {
        for host in [
            "db.example",
            "localhost.example",
            "127.0.0.1",
            "::1",
            "[::1]",
            "10.0.0.5",
            "",
        ] {
            assert_eq!(numeric_loopback_host(host), host);
        }
    }

    #[cfg(any(feature = "ai", feature = "qdrant"))]
    mod client {
        use super::super::{dial_localhost_as_loopback, dial_localhost_on_either_loopback};
        use reqwest::blocking::Client;
        use reqwest::dns::{Name, Resolve, Resolving};
        use std::sync::Arc;

        /// Fails every lookup, so a request only succeeds without one.
        struct RefusingResolver;

        impl Resolve for RefusingResolver {
            fn resolve(&self, name: Name) -> Resolving {
                let name = name.as_str().to_owned();
                Box::pin(async move { Err(format!("unexpected lookup of {name}").into()) })
            }
        }

        fn client() -> reqwest::Result<Client> {
            dial_localhost_as_loopback(Client::builder().dns_resolver(Arc::new(RefusingResolver)))
                .build()
        }

        #[test]
        fn localhost_url_dials_loopback_without_a_name_lookup() -> anyhow::Result<()> {
            let (base, handle) =
                crate::test_http::spawn_response(200, "OK", "text/plain", "ok".to_string())?;
            let url = base.replacen("127.0.0.1", "localhost", 1);

            let response = client()?.get(format!("{url}/probe")).send()?;

            assert_eq!(response.status(), 200);
            let request = handle.join().expect("server thread")?;
            let port = url.rsplit(':').next().expect("port");
            assert!(
                request.contains(&format!("host: localhost:{port}")),
                "{request}"
            );
            Ok(())
        }

        fn either_loopback_client() -> reqwest::Result<Client> {
            dial_localhost_on_either_loopback(
                Client::builder().dns_resolver(Arc::new(RefusingResolver)),
            )
            .build()
        }

        /// Serve one request on `bind` and send it through `localhost`.
        fn get_via_localhost(bind: &str, loopback: &str) -> anyhow::Result<()> {
            let (base, handle) =
                crate::test_http::spawn_response_at(bind, 200, "OK", "text/plain", "ok".into())?;
            let url = base.replacen(loopback, "localhost", 1);

            let response = either_loopback_client()?
                .get(format!("{url}/probe"))
                .send()?;

            assert_eq!(response.status(), 200);
            handle.join().expect("server thread")?;
            Ok(())
        }

        #[test]
        fn either_loopback_reaches_an_ipv6_only_listener() -> anyhow::Result<()> {
            get_via_localhost("[::1]:0", "[::1]")
        }

        #[test]
        fn either_loopback_reaches_an_ipv4_only_listener() -> anyhow::Result<()> {
            get_via_localhost("127.0.0.1:0", "127.0.0.1")
        }

        #[test]
        fn other_hosts_still_use_the_resolver() -> anyhow::Result<()> {
            let error = client()?
                .get("http://gobby-loopback-test.invalid:9/probe")
                .send()
                .expect_err("lookup must be refused");

            assert!(
                format!("{error:?}").contains("unexpected lookup of gobby-loopback-test.invalid"),
                "{error:?}"
            );
            Ok(())
        }
    }
}
