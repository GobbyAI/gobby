use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::Duration;

use tempfile::{Builder, NamedTempFile};

use crate::WikiError;

#[derive(Debug)]
pub struct Chunk {
    pub start_ms: u64,
    pub end_ms: u64,
    pub path: NamedTempFile,
}

pub fn probe_duration(path: impl AsRef<Path>) -> Option<u32> {
    let ffprobe = find_executable_on_path("ffprobe")?;
    probe_duration_with_tool(path.as_ref(), &ffprobe)
}

pub fn extract_audio_file(video: impl AsRef<Path>) -> Result<NamedTempFile, WikiError> {
    let ffmpeg = find_executable_on_path("ffmpeg").ok_or_else(|| missing_media_tool("ffmpeg"))?;
    extract_audio_file_with_ffmpeg(video.as_ref(), &ffmpeg)
}

pub fn sample_frame_images(
    video: impl AsRef<Path>,
    interval: Duration,
) -> Result<Vec<(u64, NamedTempFile)>, WikiError> {
    let tools = MediaTools::discover()?;
    sample_frame_images_with_tools(video.as_ref(), interval, &tools)
}

pub fn split_audio_file(
    audio: impl AsRef<Path>,
    window: Duration,
) -> Result<Vec<Chunk>, WikiError> {
    let tools = MediaTools::discover()?;
    split_audio_file_with_tools(audio.as_ref(), window, Duration::ZERO, &tools)
}

pub fn split_audio_file_with_overlap(
    audio: impl AsRef<Path>,
    window: Duration,
    overlap: Duration,
) -> Result<Vec<Chunk>, WikiError> {
    let tools = MediaTools::discover()?;
    split_audio_file_with_tools(audio.as_ref(), window, overlap, &tools)
}

#[derive(Debug)]
struct MediaTools {
    ffmpeg: PathBuf,
    ffprobe: PathBuf,
}

impl MediaTools {
    fn discover() -> Result<Self, WikiError> {
        Ok(Self {
            ffmpeg: find_executable_on_path("ffmpeg")
                .ok_or_else(|| missing_media_tool("ffmpeg"))?,
            ffprobe: find_executable_on_path("ffprobe")
                .ok_or_else(|| missing_media_tool("ffprobe"))?,
        })
    }
}

fn extract_audio_file_with_ffmpeg(video: &Path, ffmpeg: &Path) -> Result<NamedTempFile, WikiError> {
    let audio = media_temp_file(".wav", "create temporary audio file")?;
    let mut command = Command::new(ffmpeg);
    command
        .arg("-hide_banner")
        .arg("-loglevel")
        .arg("error")
        .arg("-y")
        .arg("-i")
        .arg(video)
        .arg("-vn")
        .arg("-ac")
        .arg("1")
        .arg("-ar")
        .arg("16000")
        .arg("-c:a")
        .arg("pcm_s16le")
        .arg("-f")
        .arg("wav")
        .arg(audio.path());
    run_media_command(command, "ffmpeg", "run ffmpeg", video)?;
    Ok(audio)
}

fn sample_frame_images_with_tools(
    video: &Path,
    interval: Duration,
    tools: &MediaTools,
) -> Result<Vec<(u64, NamedTempFile)>, WikiError> {
    let interval_ms = duration_to_ms(interval, "interval")?;
    let duration_ms = media_duration_ms(video, tools)?;
    let mut frames = Vec::new();
    let mut start_ms = 0;
    while start_ms < duration_ms {
        let frame = media_temp_file(".jpg", "create temporary frame image")?;
        let mut command = Command::new(&tools.ffmpeg);
        command
            .arg("-hide_banner")
            .arg("-loglevel")
            .arg("error")
            .arg("-y")
            .arg("-ss")
            .arg(seconds_arg(start_ms))
            .arg("-i")
            .arg(video)
            .arg("-frames:v")
            .arg("1")
            .arg("-q:v")
            .arg("2")
            .arg("-f")
            .arg("image2")
            .arg(frame.path());
        run_media_command(command, "ffmpeg", "run ffmpeg", video)?;
        frames.push((start_ms, frame));
        start_ms += interval_ms;
    }
    Ok(frames)
}

fn split_audio_file_with_tools(
    audio: &Path,
    window: Duration,
    overlap: Duration,
    tools: &MediaTools,
) -> Result<Vec<Chunk>, WikiError> {
    let window_ms = duration_to_ms(window, "window")?;
    let overlap_ms = if overlap.is_zero() {
        0
    } else {
        duration_to_ms(overlap, "overlap")?
    };
    if overlap_ms >= window_ms {
        return Err(WikiError::InvalidInput {
            field: "overlap",
            message: "must be shorter than window".to_string(),
        });
    }
    let duration_ms = media_duration_ms(audio, tools)?;
    let mut chunks = Vec::new();
    let mut start_ms = 0;
    while start_ms < duration_ms {
        let chunk = media_temp_file(".wav", "create temporary audio chunk")?;
        let chunk_ms = window_ms.min(duration_ms - start_ms);
        let end_ms = start_ms + chunk_ms;
        let mut command = Command::new(&tools.ffmpeg);
        command
            .arg("-hide_banner")
            .arg("-loglevel")
            .arg("error")
            .arg("-y")
            .arg("-ss")
            .arg(seconds_arg(start_ms))
            .arg("-i")
            .arg(audio)
            .arg("-t")
            .arg(seconds_arg(chunk_ms))
            .arg("-vn")
            .arg("-ac")
            .arg("1")
            .arg("-ar")
            .arg("16000")
            .arg("-c:a")
            .arg("pcm_s16le")
            .arg("-f")
            .arg("wav")
            .arg(chunk.path());
        run_media_command(command, "ffmpeg", "run ffmpeg", audio)?;
        chunks.push(Chunk {
            start_ms,
            end_ms,
            path: chunk,
        });
        if end_ms == duration_ms {
            break;
        }
        start_ms += window_ms - overlap_ms;
    }
    Ok(chunks)
}

fn media_duration_ms(path: &Path, tools: &MediaTools) -> Result<u64, WikiError> {
    probe_duration_with_tool(path, &tools.ffprobe)
        .map(|seconds| u64::from(seconds) * 1000)
        .ok_or_else(|| WikiError::Config {
            detail: format!(
                "ffprobe could not determine media duration for {}",
                path.display()
            ),
        })
}

fn probe_duration_with_tool(path: &Path, ffprobe: &Path) -> Option<u32> {
    let output = Command::new(ffprobe)
        .arg("-v")
        .arg("error")
        .arg("-show_entries")
        .arg("format=duration")
        .arg("-of")
        .arg("default=noprint_wrappers=1:nokey=1")
        .arg(path)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    parse_duration_seconds(&String::from_utf8_lossy(&output.stdout))
}

fn parse_duration_seconds(raw: &str) -> Option<u32> {
    let seconds = raw
        .lines()
        .map(str::trim)
        .find_map(|line| line.parse::<f64>().ok())?;
    if !seconds.is_finite() || seconds < 0.0 {
        return None;
    }
    let secs_int = seconds.trunc() as u64;
    if secs_int > u64::from(u32::MAX) {
        return None;
    }
    Some(seconds.ceil() as u32)
}

fn duration_to_ms(duration: Duration, field: &'static str) -> Result<u64, WikiError> {
    let millis = duration.as_millis();
    if millis == 0 {
        return Err(WikiError::InvalidInput {
            field,
            message: "must be at least 1 millisecond".to_string(),
        });
    }
    if millis > u64::MAX as u128 {
        return Err(WikiError::InvalidInput {
            field,
            message: "is too large to represent in milliseconds".to_string(),
        });
    }
    Ok(millis as u64)
}

fn seconds_arg(milliseconds: u64) -> String {
    format!("{}.{:03}", milliseconds / 1000, milliseconds % 1000)
}

fn media_temp_file(suffix: &str, action: &'static str) -> Result<NamedTempFile, WikiError> {
    Builder::new()
        .prefix("gwiki-media-")
        .suffix(suffix)
        .tempfile()
        .map_err(|source| WikiError::Io {
            action,
            path: None,
            source,
        })
}

fn run_media_command(
    mut command: Command,
    tool: &str,
    action: &'static str,
    source_path: &Path,
) -> Result<(), WikiError> {
    let output = command.output().map_err(|source| WikiError::Io {
        action,
        path: Some(source_path.to_path_buf()),
        source,
    })?;
    if output.status.success() {
        return Ok(());
    }
    Err(media_command_failed(tool, output))
}

fn media_command_failed(tool: &str, output: Output) -> WikiError {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stderr = stderr.trim();
    let detail = if stderr.is_empty() {
        format!("{tool} exited with status {}", output.status)
    } else {
        format!("{tool} exited with status {}: {stderr}", output.status)
    };
    WikiError::Config { detail }
}

fn find_executable_on_path(name: &str) -> Option<PathBuf> {
    let explicit = Path::new(name);
    if explicit.components().count() > 1 {
        return explicit.is_file().then(|| explicit.to_path_buf());
    }

    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }

        #[cfg(windows)]
        {
            if let Some(path_ext) = std::env::var_os("PATHEXT") {
                for extension in std::env::split_paths(&path_ext) {
                    let extension = extension.to_string_lossy();
                    let candidate = dir.join(format!("{name}{extension}"));
                    if candidate.is_file() {
                        return Some(candidate);
                    }
                }
            }
        }
    }
    None
}

fn missing_media_tool(tool: &str) -> WikiError {
    WikiError::Config {
        detail: format!("{tool} executable not found on PATH"),
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    #[test]
    fn seconds_args_use_integer_milliseconds() {
        assert_eq!(seconds_arg(0), "0.000");
        assert_eq!(seconds_arg(1_500), "1.500");
        assert_eq!(seconds_arg(61_007), "61.007");
    }

    #[cfg(unix)]
    #[test]
    fn temp_files_cleaned_asset_survives() {
        let temp = tempfile::tempdir().expect("tempdir");
        let asset_dir = temp.path().join("raw/assets");
        fs::create_dir_all(&asset_dir).expect("create asset dir");
        let asset_path = asset_dir.join("lecture.mp4");
        fs::write(&asset_path, b"raw video asset").expect("write raw asset");
        let log_path = temp.path().join("ffmpeg-outputs.log");
        let count_path = temp.path().join("ffmpeg-count");
        let ffmpeg = temp.path().join("ffmpeg");
        let ffprobe = temp.path().join("ffprobe");

        write_executable(
            &ffprobe,
            r#"#!/bin/sh
echo "3.000000"
"#,
        );
        write_executable(
            &ffmpeg,
            &format!(
                r#"#!/bin/sh
last=""
for arg do
  last="$arg"
done
count=0
if [ -f "{count}" ]; then
  count=$(cat "{count}")
fi
count=$((count + 1))
echo "$count" > "{count}"
echo "$last" >> "{log}"
printf frame > "$last"
if [ "$count" -eq 2 ]; then
  exit 7
fi
"#,
                count = count_path.display(),
                log = log_path.display()
            ),
        );

        let tools = MediaTools { ffmpeg, ffprobe };
        let result = sample_frame_images_with_tools(&asset_path, Duration::from_secs(1), &tools);

        assert!(result.is_err());
        assert_eq!(
            fs::read(&asset_path).expect("raw asset survives"),
            b"raw video asset"
        );
        let output_paths = fs::read_to_string(&log_path).expect("ffmpeg output log");
        for output_path in output_paths.lines() {
            assert!(
                !Path::new(output_path).exists(),
                "temporary output should be cleaned up: {output_path}"
            );
        }
    }

    #[cfg(unix)]
    fn write_executable(path: &Path, contents: &str) {
        use std::io::Write;
        use std::os::unix::fs::PermissionsExt;

        let mut file = fs::File::create(path).expect("create executable");
        file.write_all(contents.as_bytes())
            .expect("write executable");
        let mut permissions = file.metadata().expect("metadata").permissions();
        permissions.set_mode(0o755);
        file.set_permissions(permissions).expect("chmod executable");
    }
}
