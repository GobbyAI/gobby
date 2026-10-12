//! Node transport to its enrolled hub, without local authentication or routing.

use std::{error::Error, sync::Arc, time::Duration};

use anyhow::{Context, Result};
use axum::{
    body::Body,
    http::{HeaderMap, Request, Response, Uri, Version, header::HOST},
};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::{
    client::legacy::{Client, connect::HttpConnector},
    rt::TokioExecutor,
};
use tokio::net::TcpStream;
use tokio_rustls::{
    TlsConnector,
    rustls::{ClientConfig, RootCertStore, pki_types::ServerName},
};

use super::{
    health::{BackendState, bad_gateway, unavailable},
    proxy, tls, ws,
};

/// Parsed hub origin shared by the HTTP relay, WebSocket splice and node channel.
pub struct HubRelay {
    pub origin: String,
    pub authority: String,
    pub host: String,
    pub dial: String,
    pub tls: Option<Arc<ClientConfig>>,
    client: Client<HttpsConnector<HttpConnector>, Body>,
}

impl HubRelay {
    pub fn new(origin: &str, pem: Option<&[u8]>) -> Result<Self> {
        let uri: Uri = origin.parse().context("invalid hub origin")?;
        let https = match uri.scheme_str() {
            Some("https") => true,
            Some("http") => false,
            _ => anyhow::bail!("hub origin must use http or https"),
        };
        let authority = uri
            .authority()
            .context("hub origin has no authority")?
            .to_string();
        let host = uri
            .host()
            .context("hub origin has no host")?
            .trim_start_matches('[')
            .trim_end_matches(']')
            .to_owned();
        let port = uri.port_u16().unwrap_or(if https { 443 } else { 80 });
        let dial = if host.contains(':') {
            format!("[{host}]:{port}")
        } else {
            format!("{host}:{port}")
        };
        let config = if https {
            tls::pinned_client_config(pem.context("https hub requires a pinned certificate")?)?
        } else {
            ClientConfig::builder_with_provider(Arc::new(
                tokio_rustls::rustls::crypto::ring::default_provider(),
            ))
            .with_safe_default_protocol_versions()?
            .with_root_certificates(RootCertStore::empty())
            .with_no_client_auth()
        };
        let tls = https.then(|| Arc::new(config.clone()));
        let mut http_config = config;
        // hyper-rustls owns ALPN for its pooled client; manual TLS keeps http/1.1.
        http_config.alpn_protocols.clear();
        let mut http = HttpConnector::new();
        http.enforce_http(false);
        http.set_connect_timeout(Some(Duration::from_secs(10)));
        let connector = HttpsConnectorBuilder::new()
            .with_tls_config(http_config)
            .https_or_http()
            .enable_http1()
            .wrap_connector(http);
        let client = Client::builder(TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(90))
            .build(connector);
        Ok(Self {
            origin: origin.to_owned(),
            authority,
            host,
            dial,
            tls,
            client,
        })
    }

    fn prepare_headers(
        &self,
        headers: &mut HeaderMap,
    ) -> Result<(), axum::http::header::InvalidHeaderValue> {
        headers.insert(HOST, self.authority.parse()?);
        headers.remove("x-gobby-break-glass");
        Ok(())
    }

    pub async fn forward(&self, request: Request<Body>) -> Response<Body> {
        let (mut parts, body) = request.into_parts();
        let removed = proxy::strip_hop_by_hop(&mut parts.headers);
        let body = proxy::strip_trailers(body, removed);
        if let Err(error) = self.prepare_headers(&mut parts.headers) {
            return bad_gateway(&self.origin, error);
        }
        let path = parts.uri.path_and_query().map_or("/", |path| path.as_str());
        parts.uri = match format!("{}{path}", self.origin).parse() {
            Ok(uri) => uri,
            Err(error) => return bad_gateway(&self.origin, error),
        };
        parts.version = Version::HTTP_11;
        match self.client.request(Request::from_parts(parts, body)).await {
            Ok(response) => proxy::filter_response(response),
            // HttpConnector exposes its own connect error. hyper-rustls wraps TLS
            // failures in io::Error, though legacy::Error::is_connect covers both.
            Err(error)
                if error.is_connect()
                    && !error
                        .source()
                        .is_some_and(|source| source.is::<std::io::Error>()) =>
            {
                unavailable(&self.origin, BackendState::Down)
            }
            Err(error) => bad_gateway(&self.origin, error),
        }
    }

    pub async fn splice(&self, mut request: Request<Body>, to_root_ws: bool) -> Response<Body> {
        if let Err(error) = self.prepare_headers(request.headers_mut()) {
            return bad_gateway(&self.origin, error);
        }
        if to_root_ws {
            let path = request
                .uri()
                .query()
                .map_or_else(|| "/ws".to_owned(), |query| format!("/ws?{query}"));
            *request.uri_mut() = match path.parse() {
                Ok(uri) => uri,
                Err(error) => return bad_gateway(&self.origin, error),
            };
        }
        let stream =
            match connect_with_timeout(Duration::from_secs(10), TcpStream::connect(&self.dial))
                .await
            {
                Ok(stream) => stream,
                Err(_) => return unavailable(&self.origin, BackendState::Down),
            };
        if let Some(config) = &self.tls {
            let name = match ServerName::try_from(self.host.clone()) {
                Ok(name) => name,
                Err(error) => return bad_gateway(&self.origin, error),
            };
            let stream = match TlsConnector::from(config.clone())
                .connect(name, stream)
                .await
            {
                Ok(stream) => stream,
                Err(error) => return bad_gateway(&self.origin, error),
            };
            ws::splice_over(stream, request, &self.origin).await
        } else {
            ws::splice_over(stream, request, &self.origin).await
        }
    }
}

/// Bound one hub dial; exposed for pending-transport acceptance checks.
pub async fn connect_with_timeout<T>(
    budget: Duration,
    connect: impl std::future::Future<Output = std::io::Result<T>>,
) -> std::io::Result<T> {
    tokio::time::timeout(budget, connect)
        .await
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, "hub connect timed out"))?
}
