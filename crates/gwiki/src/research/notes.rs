use super::*;

#[derive(Serialize)]
pub(crate) struct AcceptedNoteFrontmatter<'a> {
    title: &'a str,
    research_session: &'a str,
    research_note_id: &'a str,
    research_status: &'a str,
    indexable: bool,
    sources: &'a [String],
}

pub(crate) struct AcceptedNoteWrite {
    pub(crate) note: AcceptedResearchNote,
    pub(crate) created: bool,
    pub(crate) write_conflict: bool,
}

pub(crate) fn write_accepted_note(
    vault_root: &Path,
    session_id: &str,
    note: &AcceptedNoteDraft,
) -> Result<AcceptedNoteWrite, WikiError> {
    let research_dir = vault_root.join("raw").join("research");
    fs::create_dir_all(&research_dir).map_err(|error| WikiError::Io {
        action: "create raw research directory",
        path: Some(research_dir.clone()),
        source: error,
    })?;

    let draft_id = accepted_note_draft_id(note);
    let expected_body = note.body.trim();
    if let Some(found) = find_completed_accepted_note(&research_dir, &draft_id, expected_body)? {
        return Ok(AcceptedNoteWrite {
            note: AcceptedResearchNote {
                title: note.title.clone(),
                path: found.path,
            },
            created: false,
            write_conflict: found.write_conflict,
        });
    }

    let slot = create_materializing_research_note(&research_dir, session_id, note, &draft_id)?;
    let (path, write_conflict) = match slot {
        AcceptedNoteSlot::Existing {
            path,
            write_conflict,
        } => {
            return Ok(AcceptedNoteWrite {
                note: AcceptedResearchNote {
                    title: note.title.clone(),
                    path,
                },
                created: false,
                write_conflict,
            });
        }
        AcceptedNoteSlot::Materializing {
            path,
            write_conflict,
        } => (path, write_conflict),
    };

    let body = render_accepted_note_body(session_id, note, &draft_id, "completed", true)?;
    if let Err(error) = write_file_atomically(&path, body.as_bytes(), "accepted research note") {
        let _ = fs::remove_file(&path);
        return Err(error);
    }

    if let Err(error) = append_raw_index(vault_root, &note.title, &path) {
        let _ = fs::remove_file(&path);
        return Err(error);
    }

    Ok(AcceptedNoteWrite {
        note: AcceptedResearchNote {
            title: note.title.clone(),
            path,
        },
        created: true,
        write_conflict,
    })
}

pub(crate) enum AcceptedNoteSlot {
    Existing { path: PathBuf, write_conflict: bool },
    Materializing { path: PathBuf, write_conflict: bool },
}

pub(crate) enum ResearchNoteFileState {
    Missing,
    CompletedMatching,
    /// A completed note carries our draft id but its on-disk body differs from
    /// the validated draft — a concurrent writer changed it. The run must stop
    /// without overwriting rather than dedup to tampered content.
    CompletedConflict,
    MaterializingMatching {
        stale: bool,
    },
    Occupied,
}

pub(crate) fn render_accepted_note_body(
    session_id: &str,
    note: &AcceptedNoteDraft,
    draft_id: &str,
    status: &str,
    indexable: bool,
) -> Result<String, WikiError> {
    let frontmatter = serde_yaml::to_string(&AcceptedNoteFrontmatter {
        title: &note.title,
        research_session: session_id,
        research_note_id: draft_id,
        research_status: status,
        indexable,
        sources: &note.sources,
    })
    .map_err(|error| WikiError::Yaml {
        action: "serialize accepted research note frontmatter",
        path: None,
        source: error,
    })?;
    let mut body = String::new();
    body.push_str("---\n");
    body.push_str(&frontmatter);
    body.push_str("---\n\n");
    body.push_str(note.body.trim());
    body.push('\n');
    Ok(body)
}

pub(crate) fn create_materializing_research_note(
    research_dir: &Path,
    session_id: &str,
    note: &AcceptedNoteDraft,
    draft_id: &str,
) -> Result<AcceptedNoteSlot, WikiError> {
    let title = &note.title;
    let slug = slugify(title);
    let expected_body = note.body.trim();
    for attempt in 1..=MAX_RESEARCH_NOTE_SUFFIX_ATTEMPTS {
        let file_name = if attempt == 1 {
            format!("{slug}.md")
        } else {
            format!("{slug}-{attempt}.md")
        };
        let path = research_dir.join(file_name);
        match research_note_file_state(&path, draft_id, expected_body)? {
            ResearchNoteFileState::CompletedMatching => {
                return Ok(AcceptedNoteSlot::Existing {
                    path,
                    write_conflict: false,
                });
            }
            ResearchNoteFileState::CompletedConflict => {
                return Ok(AcceptedNoteSlot::Existing {
                    path,
                    write_conflict: true,
                });
            }
            ResearchNoteFileState::MaterializingMatching { stale } if stale => {
                fs::remove_file(&path).map_err(|error| WikiError::Io {
                    action: "remove stale accepted research note marker",
                    path: Some(path.clone()),
                    source: error,
                })?;
            }
            ResearchNoteFileState::MaterializingMatching { .. } => {
                if let Some((path, write_conflict)) =
                    wait_for_materializing_research_note(&path, draft_id, expected_body, title)?
                {
                    return Ok(AcceptedNoteSlot::Existing {
                        path,
                        write_conflict,
                    });
                }
                continue;
            }
            // A different note occupying this title slug is a legitimate
            // collision, not a write conflict — bump the numeric suffix.
            ResearchNoteFileState::Occupied => {
                continue;
            }
            ResearchNoteFileState::Missing => {}
        }

        let marker = render_accepted_note_body(session_id, note, draft_id, "materializing", false)?;
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                if let Err(error) = file.write_all(marker.as_bytes()) {
                    let _ = fs::remove_file(&path);
                    return Err(WikiError::Io {
                        action: "write accepted research note marker",
                        path: Some(path),
                        source: error,
                    });
                }
                if let Err(error) = file.sync_all() {
                    let _ = fs::remove_file(&path);
                    return Err(WikiError::Io {
                        action: "sync accepted research note marker",
                        path: Some(path),
                        source: error,
                    });
                }
                return Ok(AcceptedNoteSlot::Materializing {
                    path,
                    write_conflict: false,
                });
            }
            // Another writer created this marker between our check and create —
            // a slot race, not a content conflict. Try the next suffix.
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                continue;
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "create accepted research note marker",
                    path: Some(path),
                    source: error,
                });
            }
        }
    }
    Err(WikiError::InvalidInput {
        field: "title",
        message: format!("could not allocate unique research note path for `{title}`"),
    })
}

pub(crate) fn wait_for_materializing_research_note(
    path: &Path,
    draft_id: &str,
    expected_body: &str,
    title: &str,
) -> Result<Option<(PathBuf, bool)>, WikiError> {
    let started = Instant::now();
    let mut delay = RESEARCH_NOTE_MATERIALIZE_INITIAL_DELAY;
    loop {
        match research_note_file_state(path, draft_id, expected_body)? {
            ResearchNoteFileState::CompletedMatching => {
                return Ok(Some((path.to_path_buf(), false)));
            }
            ResearchNoteFileState::CompletedConflict => {
                return Ok(Some((path.to_path_buf(), true)));
            }
            ResearchNoteFileState::Missing | ResearchNoteFileState::Occupied => return Ok(None),
            ResearchNoteFileState::MaterializingMatching { stale } if stale => {
                fs::remove_file(path).map_err(|error| WikiError::Io {
                    action: "remove stale accepted research note marker",
                    path: Some(path.to_path_buf()),
                    source: error,
                })?;
                return Ok(None);
            }
            ResearchNoteFileState::MaterializingMatching { .. } => {
                if started.elapsed() >= RESEARCH_NOTE_MATERIALIZE_TIMEOUT {
                    return Err(WikiError::InvalidInput {
                        field: "accepted_note",
                        message: format!(
                            "accepted research note `{title}` is still materializing at {}",
                            path.display()
                        ),
                    });
                }
                notify_materializing_wait_observed();
                thread::sleep(delay);
                delay = delay
                    .saturating_mul(2)
                    .min(RESEARCH_NOTE_MATERIALIZE_MAX_DELAY);
            }
        }
    }
}

pub(crate) fn accepted_note_draft_id(note: &AcceptedNoteDraft) -> String {
    let mut key = String::new();
    key.push_str(note.title.trim());
    key.push('\0');
    key.push_str(note.body.trim());
    key.push('\0');
    for source in &note.sources {
        key.push_str(source.trim());
        key.push('\0');
    }
    uuid::Uuid::new_v5(&RESEARCH_NOTE_NAMESPACE, key.as_bytes()).to_string()
}

pub(crate) struct CompletedAcceptedNote {
    path: PathBuf,
    write_conflict: bool,
}

pub(crate) fn find_completed_accepted_note(
    research_dir: &Path,
    draft_id: &str,
    expected_body: &str,
) -> Result<Option<CompletedAcceptedNote>, WikiError> {
    let entries = match fs::read_dir(research_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read raw research directory",
                path: Some(research_dir.to_path_buf()),
                source: error,
            });
        }
    };

    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read raw research directory entry",
            path: Some(research_dir.to_path_buf()),
            source: error,
        })?;
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("md") {
            continue;
        }
        match research_note_file_state(&path, draft_id, expected_body)? {
            ResearchNoteFileState::CompletedMatching => {
                return Ok(Some(CompletedAcceptedNote {
                    path,
                    write_conflict: false,
                }));
            }
            ResearchNoteFileState::CompletedConflict => {
                return Ok(Some(CompletedAcceptedNote {
                    path,
                    write_conflict: true,
                }));
            }
            _ => continue,
        }
    }
    Ok(None)
}

pub(crate) fn research_note_file_state(
    path: &Path,
    draft_id: &str,
    expected_body: &str,
) -> Result<ResearchNoteFileState, WikiError> {
    let contents = match fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return Ok(ResearchNoteFileState::Missing);
        }
        Err(error) => {
            return Err(WikiError::Io {
                action: "read accepted research note",
                path: Some(path.to_path_buf()),
                source: error,
            });
        }
    };
    let Some(frontmatter) = frontmatter_block(&contents) else {
        return Ok(ResearchNoteFileState::Occupied);
    };
    if !yaml_field_eq(frontmatter, "research_note_id", draft_id) {
        return Ok(ResearchNoteFileState::Occupied);
    }
    if yaml_field_eq(frontmatter, "research_status", "completed") {
        if research_note_body_matches(&contents, expected_body) {
            return Ok(ResearchNoteFileState::CompletedMatching);
        }
        return Ok(ResearchNoteFileState::CompletedConflict);
    }
    if yaml_field_eq(frontmatter, "research_status", "materializing") {
        return Ok(ResearchNoteFileState::MaterializingMatching {
            stale: materializing_marker_is_stale(path),
        });
    }
    Ok(ResearchNoteFileState::Occupied)
}

pub(crate) fn frontmatter_block(markdown: &str) -> Option<&str> {
    let rest = markdown
        .strip_prefix("---\n")
        .or_else(|| markdown.strip_prefix("---\r\n"))?;
    let end = rest.find("\n---").or_else(|| rest.find("\r\n---"))?;
    Some(&rest[..end])
}

/// Extract the markdown body (the content after the frontmatter fence) of an
/// accepted research note, trimmed. Returns `None` for malformed notes that
/// lack a closing fence.
pub(crate) fn research_note_body(markdown: &str) -> Option<&str> {
    let rest = markdown
        .strip_prefix("---\n")
        .or_else(|| markdown.strip_prefix("---\r\n"))?;
    let end = rest.find("\n---").or_else(|| rest.find("\r\n---"))?;
    let after_fence = rest[end..].trim_start_matches(['\r', '\n']);
    let body = after_fence.strip_prefix("---")?;
    Some(body.trim())
}

/// Whether an on-disk completed note's body matches the validated draft body.
/// A malformed note (no extractable body) is treated as a mismatch so the run
/// stops rather than dedups to tampered content.
pub(crate) fn research_note_body_matches(contents: &str, expected_body: &str) -> bool {
    research_note_body(contents).is_some_and(|body| body == expected_body.trim())
}

pub(crate) fn yaml_field_eq(frontmatter: &str, key: &str, value: &str) -> bool {
    let prefix = format!("{key}:");
    frontmatter
        .lines()
        .filter_map(|line| {
            line.strip_suffix('\r')
                .unwrap_or(line)
                .trim_start()
                .strip_prefix(&prefix)
        })
        .any(|remainder| {
            let remainder = remainder.trim_start();
            remainder == value
                || remainder
                    .strip_prefix('"')
                    .and_then(|quoted| quoted.strip_suffix('"'))
                    .is_some_and(|quoted| quoted == value)
        })
}

pub(crate) fn materializing_marker_is_stale(path: &Path) -> bool {
    fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| SystemTime::now().duration_since(modified).ok())
        .is_some_and(|age| age >= RESEARCH_NOTE_MARKER_STALE_AFTER)
}

pub(crate) fn append_raw_index(
    vault_root: &Path,
    title: &str,
    note_path: &Path,
) -> Result<(), WikiError> {
    crate::sources::SourceManifest::with_lock(vault_root, || {
        append_raw_index_locked(vault_root, title, note_path)
    })
}
