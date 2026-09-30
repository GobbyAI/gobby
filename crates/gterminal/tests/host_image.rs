//! Pinned host images (plan gterm-host-handover 1.4): the host runs from a
//! content-addressed private copy, promotion of the installed path never
//! touches it, and only the host that owns the sockets prunes old pins.

#![cfg(unix)]

mod host_support;

use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

use gobby_terminal::host::image::{
    is_pinned, pin_image, pin_image_linking, prune_images, remove_candidate, PinnedImage,
    IMAGES_DIR,
};
use sha2::{Digest, Sha256};

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn mode(path: &Path) -> u32 {
    std::fs::metadata(path)
        .expect("metadata")
        .permissions()
        .mode()
        & 0o777
}

fn write_file(path: &Path, bytes: &[u8]) {
    std::fs::write(path, bytes).expect("write file");
}

/// Replaces `path` by rename, the way promotion installs a new binary.
fn promote(path: &Path, bytes: &[u8]) {
    let staged = path.with_extension("staged");
    write_file(&staged, bytes);
    std::fs::rename(&staged, path).expect("rename over source");
}

fn gterm_pins(images: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(images)
        .expect("read images dir")
        .map(|entry| {
            entry
                .expect("dir entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|name| name.starts_with("gterm-"))
        .collect();
    names.sort();
    names
}

#[cfg(target_os = "macos")]
fn running_exe(pid: u32) -> PathBuf {
    let mut buf = vec![0u8; libc::PROC_PIDPATHINFO_MAXSIZE as usize];
    // SAFETY: buf is valid for its full length, which is passed as the size.
    let len = unsafe { libc::proc_pidpath(pid as i32, buf.as_mut_ptr().cast(), buf.len() as u32) };
    assert!(len > 0, "proc_pidpath for {pid}");
    buf.truncate(len as usize);
    PathBuf::from(String::from_utf8(buf).expect("utf-8 exe path"))
}

#[cfg(not(target_os = "macos"))]
fn running_exe(pid: u32) -> PathBuf {
    std::fs::read_link(format!("/proc/{pid}/exe")).expect("read /proc exe link")
}

#[test]
fn pin_image_is_content_addressed_and_private() {
    let dir = tempfile::tempdir().expect("tempdir");
    let images = dir.path().join(IMAGES_DIR);
    let src = dir.path().join("gterm");
    let bytes = b"gterm image v1".to_vec();
    write_file(&src, &bytes);
    let hash = sha256_hex(&bytes);

    let pin = pin_image(&images, &src).expect("pin");
    assert_eq!(pin.sha256, hash);
    assert_eq!(pin.path, images.join(format!("gterm-{hash}")));
    assert_eq!(std::fs::read(&pin.path).expect("pin bytes"), bytes);
    assert_eq!(mode(&pin.path), 0o700, "the pin is private and executable");
    assert_eq!(mode(&images), 0o700, "the images dir is private");
    assert!(is_pinned(&images, &pin.path).expect("is_pinned"));
    assert!(!is_pinned(&images, &src).expect("is_pinned src"));
    pin.verify().expect("fresh pin verifies");

    let inode = std::fs::metadata(&pin.path).expect("pin metadata").ino();
    let again = pin_image(&images, &src).expect("re-pin");
    assert_eq!(again, pin);
    assert_eq!(
        std::fs::metadata(&again.path)
            .expect("reused metadata")
            .ino(),
        inode,
        "a matching pin is reused in place"
    );

    // The pin shares the source inode, so a tampered pin is a new file there.
    std::fs::remove_file(&pin.path).expect("drop pin link");
    write_file(&pin.path, b"tampered");
    std::fs::set_permissions(&pin.path, std::fs::Permissions::from_mode(0o600))
        .expect("loosen pin mode");
    assert!(pin.verify().is_err(), "verify refuses a tampered pin");
    let repaired = pin_image(&images, &src).expect("replace mismatched pin");
    assert_eq!(repaired, pin);
    assert_eq!(
        std::fs::read(&repaired.path).expect("repaired bytes"),
        bytes
    );
    assert_eq!(mode(&repaired.path), 0o700);
    repaired.verify().expect("replaced pin verifies");

    let copied_images = dir.path().join("copied");
    let copied = pin_image_linking(&copied_images, &src, |_, _| {
        Err(std::io::Error::from_raw_os_error(libc::EXDEV))
    })
    .expect("cross-device pin falls back to copy");
    assert_eq!(copied.sha256, hash);
    assert_ne!(
        std::fs::metadata(&copied.path)
            .expect("copied metadata")
            .ino(),
        std::fs::metadata(&src).expect("src metadata").ino(),
        "the fallback pin is a copy"
    );
    assert_eq!(std::fs::read(&copied.path).expect("copied bytes"), bytes);

    let unsafe_images = dir.path().join("unsafe");
    std::fs::create_dir(&unsafe_images).expect("unsafe dir");
    std::fs::set_permissions(&unsafe_images, std::fs::Permissions::from_mode(0o777))
        .expect("open unsafe dir");
    assert!(
        pin_image(&unsafe_images, &src).is_err(),
        "a world-writable images dir is refused"
    );
    assert!(
        gterm_pins(&unsafe_images).is_empty(),
        "nothing is pinned there"
    );
}

/// A pin attempt that fails after staging bytes, in the link or in its copy
/// fallback, leaves no partial file behind for a later start to trip over.
#[test]
fn failed_pin_leaves_no_partial_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    let images = dir.path().join(IMAGES_DIR);
    let src = dir.path().join("gterm");
    write_file(&src, b"gterm image");
    let partial_link = |_: &Path, to: &Path, err: i32| {
        write_file(to, b"partial");
        Err(std::io::Error::from_raw_os_error(err))
    };

    assert!(
        pin_image_linking(&images, &src, |from, to| partial_link(from, to, libc::EIO)).is_err()
    );
    let missing = dir.path().join("missing");
    assert!(
        pin_image_linking(&images, &missing, |from, to| partial_link(
            from,
            to,
            libc::EXDEV
        ))
        .is_err(),
        "the copy fallback fails on a missing source"
    );

    let leftovers: Vec<_> = std::fs::read_dir(&images)
        .expect("read images dir")
        .map(|entry| entry.expect("dir entry").file_name())
        .collect();
    assert!(
        leftovers.is_empty(),
        "partial pins left behind: {leftovers:?}"
    );
}

#[test]
fn pins_survive_promotion_of_the_source() {
    let dir = tempfile::tempdir().expect("tempdir");
    let images = dir.path().join(IMAGES_DIR);
    let src = dir.path().join("gterm");

    write_file(&src, b"release one");
    let first = pin_image(&images, &src).expect("pin one");
    promote(&src, b"release two");
    assert_eq!(
        std::fs::read(&first.path).expect("first bytes"),
        b"release one"
    );
    first.verify().expect("first pin unchanged by promotion");

    let second = pin_image(&images, &src).expect("pin two");
    promote(&src, b"release three");
    let third = pin_image(&images, &src).expect("pin three");

    assert_eq!(
        gterm_pins(&images),
        {
            let mut all = vec![
                format!("gterm-{}", first.sha256),
                format!("gterm-{}", second.sha256),
                format!("gterm-{}", third.sha256),
            ];
            all.sort();
            all
        },
        "every earlier pin stays until pruned"
    );
    first.verify().expect("first pin intact");
    second.verify().expect("second pin intact");

    prune_images(&images, &third).expect("prune");
    assert_eq!(gterm_pins(&images), vec![format!("gterm-{}", third.sha256)]);
}

#[test]
fn cold_start_runs_from_pin() {
    let dir = host_support::temp_socket_dir();
    host_support::write_token(dir.path(), "token-pin");
    let mut host = host_support::spawn_host(dir.path());
    let control_path = dir.path().join(host_support::CONTROL_SOCKET);
    host_support::wait_socket(&control_path);

    let installed = std::fs::read(dir.path().join("gterm")).expect("launched binary");
    let hash = sha256_hex(&installed);
    let pin_path = dir.path().join(IMAGES_DIR).join(format!("gterm-{hash}"));
    assert!(pin_path.exists(), "the host pinned its launch binary");

    let mut stream = host_support::connect(&control_path);
    host_support::hello_control(&mut stream, "token-pin");
    let ping = host_support::rpc(&mut stream, "ping", serde_json::json!({}));
    assert_eq!(ping["binary_sha256"], hash, "{ping}");
    assert_eq!(ping["binary_version"], env!("CARGO_PKG_VERSION"), "{ping}");

    let running = std::fs::canonicalize(running_exe(host.id())).expect("running exe");
    assert_eq!(
        running,
        std::fs::canonicalize(&pin_path).expect("pin path"),
        "the host re-exec'd from its pin"
    );
    host.kill().expect("stop host");
}

#[test]
fn only_the_socket_owner_prunes() {
    let dir = host_support::temp_socket_dir();
    host_support::write_token(dir.path(), "token-prune");
    let images = dir.path().join(IMAGES_DIR);
    std::fs::create_dir(&images).expect("images dir");
    std::fs::set_permissions(&images, std::fs::Permissions::from_mode(0o700))
        .expect("images dir mode");
    let stale_before = images.join(format!("gterm-{}", sha256_hex(b"stale before start")));
    write_file(&stale_before, b"stale before start");

    let mut first = host_support::spawn_host(dir.path());
    let control_path = dir.path().join(host_support::CONTROL_SOCKET);
    host_support::wait_socket(&control_path);
    let running_hash = sha256_hex(&std::fs::read(dir.path().join("gterm")).expect("binary"));
    let running_pin = images.join(format!("gterm-{running_hash}"));
    host_support::wait_until("the socket owner prunes stale pins", || {
        !stale_before.exists()
    });

    let stale_after = images.join(format!("gterm-{}", sha256_hex(b"stale after start")));
    write_file(&stale_after, b"stale after start");
    let mut second = host_support::spawn_host(dir.path());
    let status = host_support::wait_exit(&mut second, std::time::Duration::from_secs(20))
        .expect("the losing host exits");
    assert!(!status.success(), "the second host loses the socket check");
    assert!(running_pin.exists(), "the live host's pin survives");
    assert!(stale_after.exists(), "a losing start never prunes");

    let running = PinnedImage {
        path: running_pin.clone(),
        sha256: running_hash.clone(),
    };
    remove_candidate(&running.clone(), &running).expect("keep running candidate");
    assert!(
        running_pin.exists(),
        "a candidate equal to the running pin stays"
    );
    let candidate = PinnedImage {
        path: stale_after.clone(),
        sha256: sha256_hex(b"stale after start"),
    };
    remove_candidate(&candidate, &running).expect("remove other candidate");
    assert!(!stale_after.exists(), "a different candidate is removed");
    first.kill().expect("stop host");
}
