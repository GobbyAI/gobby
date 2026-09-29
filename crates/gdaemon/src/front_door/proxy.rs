//! Streaming HTTP proxy to the Python backend.

use axum::body::Body;
use axum::http::header::CONNECTION;
use axum::http::{HeaderMap, HeaderName, Request, Response, Uri, Version};
use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::body::Frame;
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::rt::TokioExecutor;

use super::FrontDoorState;
use super::health::{bad_gateway, unavailable};

pub type ProxyClient = Client<HttpConnector, Body>;

/// RFC 9110 section 7.6.1 connection-specific fields, removed in both directions.
const HOP_BY_HOP: [&str; 6] = [
    "connection",
    "proxy-connection",
    "keep-alive",
    "te",
    "transfer-encoding",
    "upgrade",
];

pub fn client() -> ProxyClient {
    Client::builder(TokioExecutor::new()).build_http()
}

/// Forward `request` to the listener's backend. Bodies stream in both directions;
/// a refused connection yields the typed 503, any other failure a 502.
pub async fn forward(state: &FrontDoorState, request: Request<Body>) -> Response<Body> {
    let target = state.target;
    let (mut parts, body) = request.into_parts();
    let removed = strip_hop_by_hop(&mut parts.headers);
    let body = strip_trailers(body, removed);
    let path_and_query = parts
        .uri
        .path_and_query()
        .map_or("/", |value| value.as_str());
    parts.uri = match Uri::builder()
        .scheme("http")
        .authority(target.to_string())
        .path_and_query(path_and_query)
        .build()
    {
        Ok(uri) => uri,
        Err(error) => return bad_gateway(target, error),
    };
    parts.version = Version::HTTP_11;

    match state.client.request(Request::from_parts(parts, body)).await {
        Ok(response) => filter_response(response),
        Err(error) if error.is_connect() => unavailable(target, state.backend_state),
        Err(error) => bad_gateway(target, error),
    }
}

/// Remove the fields named by `Connection` and the fixed hop-by-hop set.
pub fn strip_hop_by_hop(headers: &mut HeaderMap) -> Vec<HeaderName> {
    let mut removed: Vec<HeaderName> = headers
        .get_all(CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|token| HeaderName::from_bytes(token.trim().as_bytes()).ok())
        .collect();
    removed.extend(HOP_BY_HOP.map(HeaderName::from_static));
    for name in &removed {
        headers.remove(name);
    }
    removed
}

/// Strip connection-specific fields from a backend response's headers and trailers.
pub fn filter_response<B>(response: Response<B>) -> Response<Body>
where
    B: hyper::body::Body<Data = Bytes> + Send + 'static,
    B::Error: Into<axum::BoxError>,
{
    let (mut parts, body) = response.into_parts();
    let removed = strip_hop_by_hop(&mut parts.headers);
    Response::from_parts(parts, strip_trailers(body, removed))
}

/// RFC 9110 section 7.6.1 applies to trailer fields too: drop the `removed` names
/// from trailer frames as they stream past.
fn strip_trailers<B>(body: B, removed: Vec<HeaderName>) -> Body
where
    B: hyper::body::Body<Data = Bytes> + Send + 'static,
    B::Error: Into<axum::BoxError>,
{
    Body::new(body.map_frame(move |frame| match frame.into_trailers() {
        Ok(mut trailers) => {
            for name in &removed {
                trailers.remove(name);
            }
            Frame::trailers(trailers)
        }
        Err(frame) => frame,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    #[test]
    fn strips_fixed_and_connection_listed_fields() {
        let mut headers = HeaderMap::new();
        headers.insert(
            CONNECTION,
            HeaderValue::from_static("keep-alive, x-private"),
        );
        headers.insert("keep-alive", HeaderValue::from_static("timeout=5"));
        headers.insert("x-private", HeaderValue::from_static("1"));
        headers.insert("transfer-encoding", HeaderValue::from_static("chunked"));
        headers.insert("x-kept", HeaderValue::from_static("yes"));

        strip_hop_by_hop(&mut headers);

        let names: Vec<&str> = headers.keys().map(HeaderName::as_str).collect();
        assert_eq!(names, vec!["x-kept"]);
    }
}
