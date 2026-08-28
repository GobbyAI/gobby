use crossterm::event::KeyModifiers;

use super::*;

#[derive(Default)]
pub(crate) struct RawInputFramer {
    byte_framer: RawInputByteFramer,
}

impl RawInputFramer {
    pub(crate) fn for_host_input() -> Self {
        Self {
            byte_framer: RawInputByteFramer::for_host_input(),
        }
    }

    pub(crate) fn push(&mut self, data: &[u8]) -> Vec<RawInputEvent> {
        Self::events_from_chunks(self.byte_framer.push(data))
    }

    pub(crate) fn host_color_query_sent(&mut self) {
        self.byte_framer.host_color_query_sent();
    }

    pub(crate) fn enable_host_color_scheme_change_tracking(&mut self) {
        self.byte_framer.enable_host_color_scheme_change_tracking();
    }

    pub(crate) fn has_pending_input(&self) -> bool {
        self.byte_framer.has_pending_input()
    }

    pub(crate) fn has_pending_incomplete_sgr_mouse_sequence(&self) -> bool {
        self.byte_framer.has_pending_incomplete_sgr_mouse_sequence()
    }

    #[cfg(any(windows, test))]
    pub(crate) fn has_pending_bracketed_paste(&self) -> bool {
        self.byte_framer.has_pending_bracketed_paste()
    }

    pub(crate) fn flush_timeout(&mut self) -> Vec<RawInputEvent> {
        Self::events_from_chunks(self.byte_framer.flush_timeout())
    }

    fn events_from_chunks(chunks: Vec<Vec<u8>>) -> Vec<RawInputEvent> {
        chunks
            .into_iter()
            .filter_map(|chunk| {
                if chunk.as_slice() == [ESC] {
                    return Some(RawInputEvent::Key(
                        TerminalKey::new(crossterm::event::KeyCode::Esc, KeyModifiers::empty())
                            .with_vt_bytes(chunk),
                    ));
                }
                extract_one_event(&chunk).map(|(event, _consumed)| {
                    tracing::debug!(raw_bytes = ?chunk, event = ?event, "raw input event parsed");
                    event
                })
            })
            .collect()
    }
}

#[derive(Default)]
pub(crate) struct RawInputByteFramer {
    pub(crate) buffer: Vec<u8>,
    pub(crate) discard_until: Option<ControlStringFamily>,
    pub(crate) discarded_tail_bytes: usize,
    pub(crate) lone_escape_recently_flushed: bool,
    pub(crate) host_color_replies_awaited: u16,
    pub(crate) host_cell_size_replies_awaited: u16,
    pub(crate) held_pending_host_reply_esc: bool,
    pub(crate) host_color_scheme_change_tracking: bool,
    pub(crate) split_coalesced_escape: bool,
}

pub(super) const HOST_COLOR_QUERY_REPLIES: u16 = 258;
#[cfg(any(unix, test))]
pub(super) const HOST_CELL_SIZE_QUERY_REPLIES: u16 = 1;
pub(super) const MAX_ORPHANED_SGR_MOUSE_TAIL_BYTES: usize = 32;

impl RawInputByteFramer {
    pub(crate) fn for_host_input() -> Self {
        Self::with_host_input_policy(
            crate::platform::capabilities().preserve_legacy_doubled_escape_input,
        )
    }

    pub(crate) fn with_host_input_policy(preserve_legacy_doubled_escape_input: bool) -> Self {
        Self {
            split_coalesced_escape: !preserve_legacy_doubled_escape_input,
            ..Self::default()
        }
    }

    pub(crate) fn push(&mut self, data: &[u8]) -> Vec<Vec<u8>> {
        self.buffer.extend_from_slice(data);
        self.drain_available_chunks()
    }

    /// Hold a lone trailing ESC for one idle flush so an OSC 10/11 reply split
    /// at its ESC introducer stitches back together instead of leaking (#549).
    pub(crate) fn host_color_query_sent(&mut self) {
        self.host_color_replies_awaited = HOST_COLOR_QUERY_REPLIES;
        self.held_pending_host_reply_esc = false;
    }

    /// Same hold window as `host_color_query_sent`, for the XTWINOPS cell size
    /// reply. Only the Unix client sends this query.
    #[cfg(any(unix, test))]
    pub(crate) fn host_cell_size_query_sent(&mut self) {
        self.host_cell_size_replies_awaited = HOST_CELL_SIZE_QUERY_REPLIES;
        self.held_pending_host_reply_esc = false;
    }

    fn awaiting_host_reply(&self) -> bool {
        self.host_color_replies_awaited > 0 || self.host_cell_size_replies_awaited > 0
    }

    pub(crate) fn enable_host_color_scheme_change_tracking(&mut self) {
        self.host_color_scheme_change_tracking = true;
    }

    pub(crate) fn has_pending_input(&self) -> bool {
        !self.buffer.is_empty()
    }

    #[cfg(any(not(windows), test))]
    pub(crate) fn has_pending_lone_escape(&self) -> bool {
        self.buffer.as_slice() == [ESC]
    }

    pub(crate) fn has_pending_incomplete_sgr_mouse_sequence(&self) -> bool {
        starts_with_incomplete_sgr_mouse_sequence(&self.buffer)
    }

    #[cfg(any(windows, test))]
    pub(crate) fn has_pending_bracketed_paste(&self) -> bool {
        self.buffer.starts_with(BRACKETED_PASTE_START)
            && find_subsequence(&self.buffer, BRACKETED_PASTE_END).is_none()
    }

    pub(crate) fn flush_timeout(&mut self) -> Vec<Vec<u8>> {
        let mut chunks = self.drain_available_chunks();

        if let Some(family) = self.discard_until {
            if family == ControlStringFamily::HostReplyCsi {
                return chunks;
            }
            if family == ControlStringFamily::OrphanedSgrMouseTail {
                self.buffer.clear();
                self.discard_until = None;
                self.discarded_tail_bytes = 0;
                return chunks;
            }

            let keep_split_st = self.buffer.last() == Some(&ESC);
            let keep_discarding = plausible_control_string_tail(family, &self.buffer);
            self.discarded_tail_bytes = self.discarded_tail_bytes.saturating_add(self.buffer.len());
            self.buffer.clear();
            if keep_discarding && self.discarded_tail_bytes <= MAX_DISCARDED_CONTROL_TAIL_BYTES {
                if keep_split_st {
                    self.buffer.push(ESC);
                }
            } else {
                self.discard_until = None;
                self.discarded_tail_bytes = 0;
            }
            return chunks;
        }

        if self.buffer.is_empty() {
            return chunks;
        }

        if self.lone_escape_recently_flushed && self.buffer.starts_with(b"[<") {
            tracing::debug!(
                len = self.buffer.len(),
                "discarding incomplete orphaned SGR mouse tail after input timeout"
            );
            discard_or_buffer_orphaned_sgr_mouse_tail(
                &mut self.buffer,
                &mut self.discard_until,
                &mut self.discarded_tail_bytes,
            );
            self.lone_escape_recently_flushed = false;
            return chunks;
        }

        if starts_with_incomplete_sgr_mouse_sequence(&self.buffer) {
            tracing::debug!(
                bytes = ?self.buffer,
                "discarding incomplete SGR mouse sequence after input timeout"
            );
            self.discarded_tail_bytes = self.buffer.len();
            self.discard_until = (self.discarded_tail_bytes <= MAX_DISCARDED_CONTROL_TAIL_BYTES)
                .then_some(ControlStringFamily::OrphanedSgrMouseTail);
            self.buffer.clear();
            return chunks;
        }

        if self.buffer.starts_with(BRACKETED_PASTE_START)
            && find_subsequence(&self.buffer, BRACKETED_PASTE_END).is_none()
        {
            tracing::trace!(
                len = self.buffer.len(),
                "waiting for bracketed paste terminator"
            );
            return chunks;
        }

        if starts_with_incomplete_default_color_response(&self.buffer) {
            tracing::trace!(
                len = self.buffer.len(),
                "waiting for host color response terminator"
            );
            return chunks;
        }

        if self.host_cell_size_replies_awaited > 0 && self.buffer.as_slice() == b"\x1b[" {
            if !self.held_pending_host_reply_esc {
                self.held_pending_host_reply_esc = true;
                tracing::trace!("holding incomplete cell size reply one flush");
                return chunks;
            }
            self.host_cell_size_replies_awaited = 0;
            self.held_pending_host_reply_esc = false;
        }

        if self.host_cell_size_replies_awaited > 0
            && starts_with_incomplete_host_cell_size_report(&self.buffer)
        {
            tracing::debug!(
                len = self.buffer.len(),
                "discarding incomplete host cell size report after input timeout"
            );
            self.host_cell_size_replies_awaited = 0;
            self.held_pending_host_reply_esc = false;
            self.discard_until = Some(ControlStringFamily::HostReplyCsi);
            self.discarded_tail_bytes = 0;
            self.buffer.clear();
            return chunks;
        }

        if starts_with_incomplete_host_color_scheme_report(&self.buffer) {
            tracing::debug!(
                len = self.buffer.len(),
                "discarding incomplete host color scheme report after input timeout"
            );
            self.discard_until = Some(ControlStringFamily::HostReplyCsi);
            self.discarded_tail_bytes = 0;
            self.buffer.clear();
            return chunks;
        }

        if let Some(ControlString::Incomplete { family }) = control_string(&self.buffer) {
            tracing::debug!(
                len = self.buffer.len(),
                "discarding incomplete host control string after input timeout"
            );
            // This intentionally gives host control replies precedence over legacy
            // Alt forms like Alt+] after timeout, so later reply tails cannot leak.
            self.discard_until = Some(family);
            self.discarded_tail_bytes = 0;
            self.buffer.clear();
            return chunks;
        }

        if self.buffer.as_slice() == [ESC] {
            if self.awaiting_host_reply() && !self.held_pending_host_reply_esc {
                self.held_pending_host_reply_esc = true;
                tracing::trace!("holding lone escape one flush while awaiting host reply");
                return chunks;
            }
            // No continuation arrived; give up the window so Escape is not delayed again.
            self.host_color_replies_awaited = 0;
            self.host_cell_size_replies_awaited = 0;
            self.held_pending_host_reply_esc = false;
            tracing::warn!(
                bytes = ?self.buffer,
                "flushing lone escape after input timeout; if this follows an alt chord or focus switch it may reach the pane as plain esc"
            );
            self.lone_escape_recently_flushed = true;
            chunks.push(std::mem::take(&mut self.buffer));
            return chunks;
        }

        if let Ok(text) = std::str::from_utf8(&self.buffer) {
            if parse_terminal_key_sequence(text).is_some() {
                chunks.push(std::mem::take(&mut self.buffer));
                return chunks;
            }
        }

        if starts_with_incomplete_utf8_char(&self.buffer) {
            tracing::trace!(bytes = ?self.buffer, "waiting for UTF-8 continuation bytes");
            return chunks;
        }

        if self.buffer.first() == Some(&ESC) && starts_with_incomplete_utf8_char(&self.buffer[1..])
        {
            tracing::trace!(bytes = ?self.buffer, "waiting for escaped UTF-8 continuation bytes");
            return chunks;
        }

        tracing::debug!(bytes = ?self.buffer, "dropping incomplete raw input buffer after timeout");
        self.lone_escape_recently_flushed = false;
        self.buffer.clear();
        chunks
    }

    fn drain_available_chunks(&mut self) -> Vec<Vec<u8>> {
        let mut chunks = Vec::new();

        loop {
            if self.lone_escape_recently_flushed {
                if starts_with_incomplete_orphaned_sgr_mouse_tail(&self.buffer) {
                    break;
                }
                if discard_complete_orphaned_sgr_mouse_tail(&mut self.buffer) {
                    self.lone_escape_recently_flushed = false;
                    continue;
                }
                self.lone_escape_recently_flushed = false;
            }

            if let Some(family) = self.discard_until {
                if family == ControlStringFamily::HostReplyCsi {
                    if discard_host_reply_csi_tail(&mut self.buffer, &mut self.discarded_tail_bytes)
                    {
                        self.discard_until = None;
                        self.discarded_tail_bytes = 0;
                        continue;
                    }
                    break;
                }
                if family == ControlStringFamily::OrphanedSgrMouseTail {
                    if discard_orphaned_sgr_mouse_tail(
                        &mut self.buffer,
                        &mut self.discarded_tail_bytes,
                    ) {
                        self.discard_until = None;
                        self.discarded_tail_bytes = 0;
                        continue;
                    }
                    break;
                }

                let Some(terminator_len) =
                    control_string_terminator_for_family(&self.buffer, family)
                else {
                    break;
                };
                self.buffer.drain(..terminator_len);
                self.discard_until = None;
                self.discarded_tail_bytes = 0;
                continue;
            }

            if self.split_coalesced_escape && self.buffer.starts_with(b"\x1b\x1b") {
                chunks.push(vec![ESC]);
                self.buffer.drain(..1);
                continue;
            }

            let Some((event, consumed)) = extract_one_event(&self.buffer) else {
                break;
            };
            if matches!(
                event,
                RawInputEvent::HostDefaultColor { .. } | RawInputEvent::HostPaletteColors { .. }
            ) {
                self.host_color_replies_awaited = self.host_color_replies_awaited.saturating_sub(1);
            } else if matches!(event, RawInputEvent::HostCellSizeReport { .. }) {
                self.host_cell_size_replies_awaited =
                    self.host_cell_size_replies_awaited.saturating_sub(1);
            } else if self.host_color_scheme_change_tracking
                && matches!(event, RawInputEvent::HostColorSchemeChanged(_))
            {
                self.host_color_query_sent();
            }
            self.held_pending_host_reply_esc = false;
            chunks.push(self.buffer[..consumed].to_vec());
            self.buffer.drain(..consumed);
        }

        chunks
    }
}

pub(super) const MAX_DISCARDED_CONTROL_TAIL_BYTES: usize = 128;
