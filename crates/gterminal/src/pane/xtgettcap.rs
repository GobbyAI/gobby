use bytes::Bytes;

#[derive(Debug, Default)]
pub(super) struct XtgettcapQueryTracker {
    state: XtgettcapTrackerState,
    body: Vec<u8>,
    pending: Vec<XtgettcapResponse>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct XtgettcapResponse {
    pub(super) end_offset: usize,
    pub(super) bytes: Bytes,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
enum XtgettcapTrackerState {
    #[default]
    Ground,
    Escape,
    DcsIntro,
    DcsIntroPlus,
    DcsBody,
    DcsEscape,
    IgnoreOsc,
    IgnoreOscEscape,
    IgnoreString,
    IgnoreStringEscape,
    OversizedDcs,
    OversizedDcsEscape,
}

impl XtgettcapQueryTracker {
    pub(super) fn observe(&mut self, bytes: &[u8]) {
        for (index, &byte) in bytes.iter().enumerate() {
            match self.state {
                XtgettcapTrackerState::Ground => {
                    if byte == 0x1b {
                        self.state = XtgettcapTrackerState::Escape;
                    } else if byte == 0x90 {
                        self.body.clear();
                        self.state = XtgettcapTrackerState::DcsIntro;
                    } else if byte == 0x9d {
                        self.state = XtgettcapTrackerState::IgnoreOsc;
                    } else if matches!(byte, 0x98 | 0x9e | 0x9f) {
                        self.state = XtgettcapTrackerState::IgnoreString;
                    }
                }
                XtgettcapTrackerState::Escape => match byte {
                    b'P' => {
                        self.body.clear();
                        self.state = XtgettcapTrackerState::DcsIntro;
                    }
                    b']' => {
                        self.body.clear();
                        self.state = XtgettcapTrackerState::IgnoreOsc;
                    }
                    b'_' | b'^' | b'X' => {
                        self.body.clear();
                        self.state = XtgettcapTrackerState::IgnoreString;
                    }
                    0x1b => self.state = XtgettcapTrackerState::Escape,
                    _ => self.state = XtgettcapTrackerState::Ground,
                },
                XtgettcapTrackerState::DcsIntro => match byte {
                    b'+' => self.state = XtgettcapTrackerState::DcsIntroPlus,
                    0x1b => self.state = XtgettcapTrackerState::IgnoreStringEscape,
                    0x9c => self.state = XtgettcapTrackerState::Ground,
                    _ => self.state = XtgettcapTrackerState::IgnoreString,
                },
                XtgettcapTrackerState::DcsIntroPlus => match byte {
                    b'q' => self.state = XtgettcapTrackerState::DcsBody,
                    0x1b => self.state = XtgettcapTrackerState::IgnoreStringEscape,
                    0x9c => self.state = XtgettcapTrackerState::Ground,
                    _ => self.state = XtgettcapTrackerState::IgnoreString,
                },
                XtgettcapTrackerState::DcsBody => match byte {
                    0x1b => self.state = XtgettcapTrackerState::DcsEscape,
                    0x9c => {
                        self.finalize(index + 1);
                        self.state = XtgettcapTrackerState::Ground;
                    }
                    _ => self.body.push(byte),
                },
                XtgettcapTrackerState::DcsEscape => {
                    if byte == b'\\' {
                        self.finalize(index + 1);
                        self.state = XtgettcapTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.body.clear();
                        self.state = XtgettcapTrackerState::IgnoreString;
                    }
                }
                XtgettcapTrackerState::IgnoreOsc => {
                    if byte == 0x1b {
                        self.state = XtgettcapTrackerState::IgnoreOscEscape;
                    } else if matches!(byte, 0x07 | 0x9c) {
                        self.state = XtgettcapTrackerState::Ground;
                    }
                }
                XtgettcapTrackerState::IgnoreOscEscape => {
                    if byte == b'\\' {
                        self.state = XtgettcapTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.state = XtgettcapTrackerState::IgnoreOsc;
                    }
                }
                XtgettcapTrackerState::IgnoreString => {
                    if byte == 0x1b {
                        self.state = XtgettcapTrackerState::IgnoreStringEscape;
                    } else if byte == 0x9c {
                        self.state = XtgettcapTrackerState::Ground;
                    }
                }
                XtgettcapTrackerState::IgnoreStringEscape => {
                    if byte == b'\\' {
                        self.state = XtgettcapTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.state = XtgettcapTrackerState::IgnoreString;
                    }
                }
                XtgettcapTrackerState::OversizedDcs => {
                    if byte == 0x1b {
                        self.state = XtgettcapTrackerState::OversizedDcsEscape;
                    } else if byte == 0x9c {
                        self.state = XtgettcapTrackerState::Ground;
                    }
                }
                XtgettcapTrackerState::OversizedDcsEscape => {
                    if byte == b'\\' {
                        self.state = XtgettcapTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.state = XtgettcapTrackerState::OversizedDcs;
                    }
                }
            }

            if self.body.len() > 1024 {
                self.body.clear();
                self.state = XtgettcapTrackerState::OversizedDcs;
            }
        }
    }

    fn finalize(&mut self, end_offset: usize) {
        for cap_hex in self.body.split(|byte| *byte == b';') {
            if let Some(bytes) = xtgettcap_response(cap_hex) {
                self.pending.push(XtgettcapResponse { end_offset, bytes });
            }
        }
        self.body.clear();
    }

    pub(super) fn drain_pending(&mut self) -> Vec<XtgettcapResponse> {
        std::mem::take(&mut self.pending)
    }
}

fn xtgettcap_response(cap_hex: &[u8]) -> Option<Bytes> {
    if cap_hex.is_empty() || !cap_hex.len().is_multiple_of(2) {
        return None;
    }

    let mut normalized_cap_hex = Vec::with_capacity(cap_hex.len());
    for &byte in cap_hex {
        if !byte.is_ascii_hexdigit() {
            return None;
        }
        normalized_cap_hex.push(byte.to_ascii_uppercase());
    }

    let value = xtgettcap_value(&normalized_cap_hex)?;
    Some(build_xtgettcap_response(&normalized_cap_hex, value))
}

fn xtgettcap_value(cap_hex: &[u8]) -> Option<Option<&'static [u8]>> {
    // Mirror only the Ghostty terminfo capabilities that this pane path can stand behind.
    match cap_hex {
        b"5463" => Some(None),
        b"524742" => Some(Some(b"8")),
        b"73657472676266" => Some(Some(b"\\E[38:2:%p1%d:%p2%d:%p3%dm")),
        b"73657472676262" => Some(Some(b"\\E[48:2:%p1%d:%p2%d:%p3%dm")),
        b"4D73" => Some(Some(b"\\E]52;%p1%s;%p2%s\\007")),
        b"5375" => Some(None),
        b"536D756C78" => Some(Some(b"\\E[4:%p1%dm")),
        b"536574756C63" => Some(Some(
            b"\\E[58:2::%p1%{65536}%/%d:%p1%{256}%/%{255}%&%d:%p1%{255}%&%d%;m",
        )),
        _ => None,
    }
}

fn build_xtgettcap_response(cap_hex: &[u8], value: Option<&[u8]>) -> Bytes {
    let mut response =
        Vec::with_capacity(8 + cap_hex.len() + value.map_or(0, |bytes| bytes.len() * 2));
    response.extend_from_slice(b"\x1bP1+r");
    response.extend_from_slice(cap_hex);
    if let Some(value) = value {
        response.push(b'=');
        append_upper_hex(value, &mut response);
    }
    response.extend_from_slice(b"\x1b\\");
    Bytes::from(response)
}

fn append_upper_hex(bytes: &[u8], output: &mut Vec<u8>) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    for &byte in bytes {
        output.push(HEX[usize::from(byte >> 4)]);
        output.push(HEX[usize::from(byte & 0x0f)]);
    }
}

#[cfg(test)]
#[path = "xtgettcap/tests.rs"]
mod tests;
