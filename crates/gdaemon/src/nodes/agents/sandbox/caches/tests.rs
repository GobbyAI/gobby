use super::*;
use anyhow::Context;
use std::os::unix::fs::symlink;

struct Fixture {
    dir: tempfile::TempDir,
    layout: RunLayout,
}

fn fixture() -> Result<Fixture> {
    let dir = tempfile::tempdir()?;
    let shared_cache = dir.path().join("gobby").join("cache").join("sandbox");
    fs::create_dir_all(&shared_cache)?;
    let run = dir.path().join("run");
    let layout = RunLayout {
        root: run.clone(),
        tmp: run.join("tmp"),
        hooks: run.join("hooks"),
        logs: run.join("logs"),
        cache: run.join("cache"),
        cargo_target: shared_cache.join("cargo-target").join("checkout"),
        shared_cache,
    };
    Ok(Fixture { dir, layout })
}

fn granted(path: &Path) -> String {
    text(&resolve_nonstrict(path))
}

fn real_grants(layout: &RunLayout) -> Result<()> {
    fs::create_dir_all(layout.cargo_home())?;
    fs::create_dir_all(&layout.cargo_target)?;
    Ok(())
}

#[test]
fn recheck_refuses_a_cargo_home_swapped_for_a_link() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    let outside = fixture.dir.path().join("outside");
    fs::create_dir_all(&outside)?;
    symlink(&outside, layout.cargo_home())?;
    fs::create_dir_all(&layout.cargo_target)?;
    let grants = [granted(&layout.cargo_home()), granted(&layout.cargo_target)];
    let error = recheck_cache_grants(layout, &grants)
        .err()
        .context("a cargo-home link must be refused")?;
    assert!(
        error
            .to_string()
            .contains("resolves outside the sandbox cache"),
        "{error}"
    );
    Ok(())
}

#[test]
fn recheck_refuses_a_cargo_target_swapped_for_a_link() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    let outside = fixture.dir.path().join("outside");
    fs::create_dir_all(&outside)?;
    fs::create_dir_all(layout.cargo_home())?;
    let target_parent = layout
        .cargo_target
        .parent()
        .context("cargo target has a parent")?;
    fs::create_dir_all(target_parent)?;
    symlink(&outside, &layout.cargo_target)?;
    let grants = [granted(&layout.cargo_home()), granted(&layout.cargo_target)];
    let error = recheck_cache_grants(layout, &grants)
        .err()
        .context("a cargo-target link must be refused")?;
    assert!(
        error
            .to_string()
            .contains("resolves outside the sandbox cache"),
        "{error}"
    );
    Ok(())
}

#[test]
fn recheck_refuses_a_real_grant_the_rendered_policy_omits() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    real_grants(layout)?;
    let grants = [granted(&layout.cargo_home())];
    let error = recheck_cache_grants(layout, &grants)
        .err()
        .context("a grant missing from the policy must be refused")?;
    assert!(error.to_string().contains("Sandbox cache grant"), "{error}");
    assert!(error.to_string().contains("cargo-target"), "{error}");
    Ok(())
}

#[test]
fn recheck_accepts_real_grants_the_rendered_policy_carries() -> Result<()> {
    let fixture = fixture()?;
    let layout = &fixture.layout;
    real_grants(layout)?;
    let grants = [granted(&layout.cargo_home()), granted(&layout.cargo_target)];
    recheck_cache_grants(layout, &grants)
}
