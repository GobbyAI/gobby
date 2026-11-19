//! The frame publisher a family uses to broadcast to WebSocket subscribers.

/// Broadcasts frames to the subscribers whose subscriptions select `filter`.
pub trait FramePublisher: Send + Sync {
    /// Queues `frames` for every subscriber that `filter`, the original message, selects.
    fn publish_frames(
        &self,
        filter: &serde_json::Value,
        frames: Vec<String>,
    ) -> Result<(), PublishRefused>;
}

/// The publisher could not accept the frames, for example because the batch is too large
/// or no broker is attached.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PublishRefused;
