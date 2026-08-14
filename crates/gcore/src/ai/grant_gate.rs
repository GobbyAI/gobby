//! Grant-backed modality gates. Availability is decided from the grant
//! before any daemon HTTP call.

use crate::ai_types::AiError;
use crate::config::AiCapability;
use crate::grant::{self, GrantCapabilities};

pub fn require_modality(
    capabilities: &GrantCapabilities,
    capability: AiCapability,
) -> Result<(), AiError> {
    match granted(capabilities, capability) {
        grant::AiCapability::Daemon {} => Ok(()),
        grant::AiCapability::Unavailable {} => Err(AiError::capability_unavailable(
            capability.as_str(),
            format!("grant does not permit {}", capability.as_str()),
        )),
    }
}

pub fn require_modality_ready(
    capabilities: &GrantCapabilities,
    daemon_reachable: bool,
    capability: AiCapability,
) -> Result<(), AiError> {
    require_modality(capabilities, capability)?;
    if !daemon_reachable {
        return Err(AiError::capability_unavailable(
            capability.as_str(),
            "daemon is unreachable",
        ));
    }
    Ok(())
}

fn granted(capabilities: &GrantCapabilities, capability: AiCapability) -> &grant::AiCapability {
    match capability {
        AiCapability::Embed => &capabilities.embed,
        AiCapability::TextGenerate => &capabilities.text_generate,
        AiCapability::ToolChat => &capabilities.tool_chat,
        AiCapability::VisionExtract => &capabilities.vision_extract,
        AiCapability::AudioTranscribe | AiCapability::AudioTranslate => {
            &capabilities.audio_transcribe
        }
    }
}
