//! Restores `resultType: "complete"` on the list and call results that go out over stdio.
//!
//! rmcp strips `resultType` from results for peers on protocol versions that predate the
//! field, and the wrapper's clients expect it on every list and call envelope.

use std::future::Future;
use std::sync::Arc;

use rmcp::RoleServer;
use rmcp::model::{JsonRpcMessage, ResultType, ServerResult};
use rmcp::service::{RxJsonRpcMessage, TxJsonRpcMessage};
use rmcp::transport::Transport;
use tokio::sync::Notify;

/// Wraps a server transport, marks its list and call results complete, and reports end of input.
pub struct CompleteResults<T> {
    inner: T,
    ended: Arc<Notify>,
}

impl<T> CompleteResults<T> {
    /// `ended` is notified once the inbound stream closes.
    pub fn new(inner: T, ended: Arc<Notify>) -> Self {
        Self { inner, ended }
    }
}

impl<T> Transport<RoleServer> for CompleteResults<T>
where
    T: Transport<RoleServer>,
{
    type Error = T::Error;

    fn send(
        &mut self,
        mut item: TxJsonRpcMessage<RoleServer>,
    ) -> impl Future<Output = Result<(), Self::Error>> + Send + 'static {
        mark_complete(&mut item);
        self.inner.send(item)
    }

    fn receive(&mut self) -> impl Future<Output = Option<RxJsonRpcMessage<RoleServer>>> + Send {
        let receive = self.inner.receive();
        let ended = Arc::clone(&self.ended);
        async move {
            let message = receive.await;
            if message.is_none() {
                ended.notify_one();
            }
            message
        }
    }

    fn close(&mut self) -> impl Future<Output = Result<(), Self::Error>> + Send {
        self.inner.close()
    }
}

fn mark_complete(message: &mut TxJsonRpcMessage<RoleServer>) {
    let JsonRpcMessage::Response(response) = message else {
        return;
    };
    let result_type = match &mut response.result {
        ServerResult::ListToolsResult(result) => &mut result.result_type,
        ServerResult::CallToolResult(result) => &mut result.result_type,
        _ => return,
    };
    *result_type = Some(ResultType::COMPLETE);
}
