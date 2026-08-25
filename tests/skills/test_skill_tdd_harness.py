"""Behavioral scenario tests for bundled skills."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = [pytest.mark.unit, pytest.mark.skill_tdd]

SCENARIOS = Path(__file__).resolve().parent / "scenarios"


def test_writing_skills_requires_scenario_before_skill_body() -> None:
    result = run_recorded_skill_scenario(SCENARIOS / "writing-skills/create-discipline-skill.yaml")

    assert result.baseline.action_names == ("write_skill", "respond")
    assert result.loaded.action_names == (
        "add_pressure_scenario",
        "write_skill",
        "run_skill_tdd",
        "respond",
    )
    assert result.has_behavioral_delta


def test_plan_embeds_artifact_provenance_in_presented_full_plan() -> None:
    """Verify loaded plan guidance preserves provenance when plan bodies are copied."""
    result = run_recorded_skill_scenario(SCENARIOS / "plan/present-artifact-provenance.yaml")

    baseline_plan = str(result.baseline.actions[2]["text"])
    loaded_plan = str(result.loaded.actions[1]["text"])

    assert ".gobby/plans/" not in baseline_plan
    assert loaded_plan.splitlines()[0] == "Plan artifact: `.gobby/plans/blue-green-rollout.md`"
    assert result.has_behavioral_delta


def test_elicit_presents_decision_record_as_plain_conversation_text() -> None:
    """Verify Decision Records remain visible outside confirmation widgets."""
    result = run_recorded_skill_scenario(SCENARIOS / "elicit/plain-text-decision-record.yaml")

    assert result.baseline.combined_text == ""
    assert result.loaded.action_names == ("present_decision_record", "ask_user_question")
    assert "Storage: PostgreSQL." in result.loaded.combined_text
    assert "Success criterion: zero lost writes." in result.loaded.combined_text

    repository_root = Path(__file__).resolve().parents[2]
    skill_text = (repository_root / "src/gobby/install/shared/skills/elicit/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "plain conversation text" in skill_text
    assert "never solely inside" in skill_text


@pytest.mark.parametrize("skill_name", ["plan", "merge-expert", "goal"])
def test_coordinator_skills_page_bounded_terminal_captures(skill_name: str) -> None:
    """Verify coordinators retrieve complete captures before consuming terminal reports."""
    result = run_recorded_skill_scenario(SCENARIOS / skill_name / "page-terminal-capture.yaml")

    assert "trust_bounded_excerpt" in result.baseline.action_names
    assert "get_agent_capture_pages" in result.loaded.action_names
    capture_action = next(
        action for action in result.loaded.actions if action["action"] == "get_agent_capture_pages"
    )
    assert capture_action["tool"] == "get_agent_capture"
    assert result.has_behavioral_delta


def test_build_coordinator_turns_manual_coordination_into_build_fixes() -> None:
    """Verify the loaded build coordinator scenario replaces manual waits with build fixes."""
    result = run_recorded_skill_scenario(
        SCENARIOS / "build-coordinator/unattended-build-coordination.yaml"
    )

    assert result.baseline.action_names == (
        "run_build",
        "wait_for_agent",
        "close_target",
        "respond",
    )
    assert result.loaded.action_names == (
        "create_coordination_epic",
        "inspect_dependency_tree",
        "normalize_leaf_stages",
        "launch_build",
        "monitor_dispatch",
        "inspect_coordination_epic_bugs",
        "fix_actionable_coordination_bug",
        "monitor_agents",
        "check_context_health",
        "compact_self_before_agent_wait",
        "wait_for_agent_once_to_subscribe",
        "end_turn_until_wake",
        "recheck_wait_for_agent_then_sweep",
        "verify_build_bugs_closed",
        "close_target",
        "close_coordination_epic",
        "respond",
    )
    assert result.has_behavioral_delta


def test_coderabbit_verifies_findings_before_fixing() -> None:
    """Verify the loaded CodeRabbit scenario inspects findings before fixing and cleans up reports."""
    result = run_recorded_skill_scenario(SCENARIOS / "coderabbit/verify-before-fixing.yaml")

    assert result.baseline.action_names == ("apply_finding", "leave_report", "respond")
    assert result.loaded.action_names == (
        "inspect_current_code",
        "document_no_fix",
        "apply_valid_finding",
        "delete_processed_report",
        "run_validation",
        "commit_and_close_task",
        "respond",
    )
    assert result.has_behavioral_delta


def test_review_learning_records_confirmed_reusable_lessons() -> None:
    """Verify review-learning adds recall, sibling sweep, and lesson recording."""
    result = run_recorded_skill_scenario(
        SCENARIOS / "review-learning/record-confirmed-lessons.yaml"
    )

    assert result.baseline.action_names == ("fix_review_finding", "run_validation", "respond")
    assert result.loaded.action_names == (
        "recall_review_context",
        "add_relevant_memory_column",
        "gcode_sibling_sweep",
        "fix_review_finding",
        "run_validation",
        "record_review_lesson",
        "respond",
    )
    assert result.has_behavioral_delta


def test_code_index_uses_gcode_navigation_before_line_readers() -> None:
    """Verify loaded code-index behavior retrieves symbols before narrow line context."""
    result = run_recorded_skill_scenario(SCENARIOS / "code-index/gcode-before-line-readers.yaml")

    assert result.baseline.action_names == (
        "gcode_search",
        "broad_sed_read",
        "broad_file_read",
        "respond",
    )
    assert result.loaded.action_names == (
        "gcode_search",
        "gcode_outline",
        "gcode_symbol",
        "narrow_sed_context",
        "respond",
    )

    assert result.loaded.actions[2]["command"] == "gcode symbol <symbol-id-from-outline>"
    assert (
        result.loaded.actions[3]["command"]
        == "sed -n '<1-3 adjacent lines>' src/gobby/skills/parser.py"
    )
    assert result.has_behavioral_delta


def test_typescript_skill_loads_strict_reference_pattern() -> None:
    """Verify TypeScript work follows diagnostics before strict code and config changes."""
    result = run_recorded_skill_scenario(
        SCENARIOS / "typescript/strict-reference-implementation.yaml"
    )

    assert result.baseline.action_names == (
        "write_loose_tsconfig",
        "cast_external_response",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "follow_compiler_diagnostic",
        "write_strict_tsconfig",
        "model_domain_types",
        "validate_external_response",
        "add_type_and_runtime_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/error-handling.md"
    assert result.loaded.actions[3]["path"] == "packages/client/src/api.ts"
    assert result.has_behavioral_delta


def test_python_skill_loads_strict_typed_config_pattern() -> None:
    """Verify Python work loads references before config and typed boundary changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "python/strict-typed-config-boundaries.yaml")

    assert result.baseline.action_names == (
        "relax_pyproject_type_checks",
        "patch_untyped_config_loader",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "preserve_pyproject_tooling",
        "model_typed_config",
        "validate_environment_boundary",
        "add_pytest_cases",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/error-handling.md"
    assert result.loaded.actions[3]["path"] == "references/testing.md"
    assert result.has_behavioral_delta


def test_python_skill_replaces_suppressions_with_typed_boundaries() -> None:
    """Verify Python diagnostics route to adapters, stubs, and facade exports."""
    result = run_recorded_skill_scenario(SCENARIOS / "python/last-resort-suppressions.yaml")

    assert result.baseline.action_names == (
        "add_bare_noqa",
        "add_bare_type_ignore",
        "respond",
    )
    assert result.loaded.action_names == (
        "inspect_diagnostics",
        "attempt_root_cause_fixes",
        "define_typed_adapter",
        "add_local_stub",
        "export_registration_facade",
        "add_regression_tests",
        "run_validation",
        "respond",
    )
    assert result.has_behavioral_delta


def test_development_discipline_rejects_python_suppressions() -> None:
    """Verify deadline pressure still routes diagnostics to typed boundaries."""
    result = run_recorded_skill_scenario(
        SCENARIOS / "development-discipline/no-python-suppressions.yaml"
    )

    assert result.baseline.action_names == (
        "add_bare_noqa",
        "add_bare_type_ignore",
        "respond",
    )
    assert result.loaded.action_names == (
        "inspect_diagnostics",
        "define_protocol_adapter",
        "add_local_stub",
        "add_regression_tests",
        "run_suppression_ratchet",
        "respond",
    )
    assert result.has_behavioral_delta


def test_json_skill_loads_strict_config_schema_pattern() -> None:
    """Verify JSON work loads references before config and schema changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "json/strict-config-schema-boundaries.yaml")

    assert result.baseline.action_names == (
        "rewrite_package_json_by_hand",
        "loosen_schema_contract",
        "paste_unvalidated_fixture",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "preserve_package_manager_boundary",
        "tighten_schema_contract",
        "validate_fixture_round_trip",
        "reject_secret_placeholders",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/syntax-and-data-model.md"
    assert result.loaded.actions[2]["path"] == "references/schema-and-validation.md"
    assert result.loaded.actions[3]["path"] == "references/parsing-and-serialization.md"
    assert result.loaded.actions[4]["path"] == "references/security-and-secrets.md"
    assert result.has_behavioral_delta


def test_javascript_skill_loads_strict_runtime_boundary_pattern() -> None:
    """Verify JavaScript work loads references before runtime and config changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "javascript/strict-runtime-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_unvalidated_fetch_client",
        "add_quick_package_script",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_js_tooling",
        "validate_external_response",
        "add_runtime_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/error-handling.md"
    assert result.has_behavioral_delta


def test_go_skill_loads_strict_module_boundary_pattern() -> None:
    """Verify Go work loads references before module and boundary changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "go/strict-module-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_unvalidated_http_client",
        "run_broad_go_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "update_go_module",
        "model_domain_types",
        "validate_external_response",
        "add_table_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/error-handling.md"
    assert result.has_behavioral_delta


def test_rust_skill_loads_strict_crate_boundary_pattern() -> None:
    """Verify Rust work loads references before crate, Cargo, and async changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "rust/strict-crate-boundaries.yaml")

    assert result.baseline.action_names == (
        "clone_away_borrow_checker",
        "update_cargo_quickly",
        "spawn_unbounded_tokio_tasks",
        "run_broad_cargo_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_cargo_features",
        "model_domain_newtypes",
        "preserve_borrowed_api",
        "structure_timeout_async_boundary",
        "add_nextest_and_property_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/ownership.md"
    assert result.loaded.actions[2]["path"] == "references/types.md"
    assert result.loaded.actions[3]["path"] == "references/error-handling.md"
    assert result.has_behavioral_delta


def test_java_skill_loads_strict_service_boundary_pattern() -> None:
    """Verify Java work loads references before service and build changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "java/strict-service-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_null_prone_service",
        "run_broad_gradle_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_java_toolchain",
        "model_domain_types",
        "validate_external_response",
        "isolate_framework_boundary",
        "add_junit_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/error-handling.md"
    assert result.loaded.actions[3]["path"] == "references/framework-boundaries.md"
    assert result.has_behavioral_delta


def test_php_skill_loads_strict_web_boundary_pattern() -> None:
    """Verify PHP work loads references before web and Composer changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "php/strict-web-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_raw_superglobal_controller",
        "update_composer_quickly",
        "run_broad_composer_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_composer_and_static_analysis",
        "model_request_and_response_dtos",
        "validate_http_input",
        "isolate_framework_boundary",
        "add_phpunit_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/security.md"
    assert result.loaded.actions[3]["path"] == "references/framework-boundaries.md"
    assert result.has_behavioral_delta


def test_dart_skill_loads_strict_widget_boundary_pattern() -> None:
    """Verify Dart work loads references before Flutter widget and package changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "dart/strict-widget-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_stateful_widget_with_inline_network",
        "update_pubspec_quickly",
        "run_broad_flutter_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_pubspec_and_analysis_options",
        "model_null_safe_domain_state",
        "isolate_async_repository_boundary",
        "split_widget_from_state_management",
        "add_widget_and_unit_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/async-and-errors.md"
    assert result.loaded.actions[3]["path"] == "references/flutter-boundaries.md"
    assert result.has_behavioral_delta


def test_c_skill_loads_strict_library_boundary_pattern() -> None:
    """Verify C work loads references before parser and build changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "c/strict-library-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_unchecked_parser",
        "update_makefile_quickly",
        "run_broad_make_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_build_and_sanitizers",
        "model_header_abi_contract",
        "validate_buffer_bounds",
        "isolate_cleanup_paths",
        "add_unit_and_sanitizer_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types-and-abi.md"
    assert result.loaded.actions[2]["path"] == "references/memory-and-lifetime.md"
    assert result.loaded.actions[3]["path"] == "references/errors-and-resources.md"
    assert result.has_behavioral_delta


def test_cpp_skill_loads_strict_library_boundary_pattern() -> None:
    """Verify C++ work loads references before library and build changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "cpp/strict-library-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_raw_pointer_cache",
        "update_cmake_quickly",
        "run_broad_ctest",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_build_and_analysis",
        "model_public_header_contract",
        "replace_raw_pointer_ownership",
        "isolate_exception_and_result_boundaries",
        "add_gtest_and_sanitizer_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types-templates-and-abi.md"
    assert result.loaded.actions[2]["path"] == "references/ownership-and-lifetime.md"
    assert result.loaded.actions[3]["path"] == "references/errors-and-resources.md"
    assert result.has_behavioral_delta


def test_csharp_skill_loads_strict_service_boundary_pattern() -> None:
    """Verify C# work loads references before ASP.NET Core and project changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "csharp/strict-service-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_controller_with_sync_io",
        "update_csproj_quickly",
        "run_broad_dotnet_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_project_and_analyzers",
        "model_nullable_contracts",
        "isolate_async_service_boundary",
        "split_controller_from_domain_logic",
        "add_xunit_and_web_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types.md"
    assert result.loaded.actions[2]["path"] == "references/async-and-errors.md"
    assert result.loaded.actions[3]["path"] == "references/framework-boundaries.md"
    assert result.has_behavioral_delta


def test_elixir_skill_loads_strict_otp_boundary_pattern() -> None:
    """Verify Elixir work loads references before OTP and Mix changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "elixir/strict-otp-boundaries.yaml")

    assert result.baseline.action_names == (
        "spawn_unsupervised_worker",
        "update_mix_quickly",
        "run_broad_mix_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_mix_and_analysis",
        "model_module_contracts",
        "add_supervised_worker",
        "isolate_result_and_telemetry_boundaries",
        "add_exunit_process_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types-and-contracts.md"
    assert result.loaded.actions[2]["path"] == "references/otp-and-concurrency.md"
    assert result.loaded.actions[3]["path"] == "references/errors-and-observability.md"
    assert result.has_behavioral_delta


def test_ruby_skill_loads_strict_rails_boundary_pattern() -> None:
    """Verify Ruby work loads references before Rails and Bundler changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "ruby/strict-rails-boundaries.yaml")

    assert result.baseline.action_names == (
        "add_model_callback",
        "update_gemfile_quickly",
        "run_broad_rspec",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_bundler_and_analysis",
        "model_service_contract",
        "isolate_rails_boundary",
        "preserve_result_and_observability_boundaries",
        "add_rspec_boundary_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/object-model-and-contracts.md"
    assert result.loaded.actions[2]["path"] == "references/data-and-framework-boundaries.md"
    assert result.loaded.actions[3]["path"] == "references/errors-and-observability.md"
    assert result.has_behavioral_delta


def test_kotlin_skill_loads_strict_coroutine_boundary_pattern() -> None:
    """Verify Kotlin work loads references before Gradle and coroutine changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "kotlin/strict-coroutine-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_nullable_platform_service",
        "update_gradle_quickly",
        "launch_global_coroutine",
        "run_broad_gradle_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_kotlin_toolchain",
        "model_null_safe_contract",
        "structure_coroutine_scope",
        "isolate_android_boundary",
        "add_coroutine_boundary_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/type-system-and-api-contracts.md"
    assert result.loaded.actions[2]["path"] == "references/coroutines-and-error-handling.md"
    assert result.loaded.actions[3]["path"] == "references/framework-and-platform-boundaries.md"
    assert result.has_behavioral_delta


def test_swift_skill_loads_strict_concurrency_boundary_pattern() -> None:
    """Verify Swift work loads references before package and concurrency changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "swift/strict-concurrency-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_force_unwrapped_service",
        "update_package_quickly",
        "launch_detached_task",
        "run_broad_swift_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "configure_swift_package",
        "model_validated_profile_contract",
        "structure_actor_owned_refresh",
        "isolate_main_actor_view_model",
        "add_concurrency_boundary_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types-and-api-design.md"
    assert result.loaded.actions[2]["path"] == "references/concurrency-and-error-handling.md"
    assert result.loaded.actions[3]["path"] == "references/framework-and-platform-boundaries.md"
    assert result.has_behavioral_delta


def test_yaml_skill_loads_strict_config_boundary_pattern() -> None:
    """Verify YAML work loads references before config and platform changes."""
    result = run_recorded_skill_scenario(SCENARIOS / "yaml/strict-config-boundaries.yaml")

    assert result.baseline.action_names == (
        "write_ambiguous_workflow",
        "broaden_token_permissions",
        "edit_values_without_schema",
        "run_broad_yaml_lint",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "identify_workflow_and_chart_owners",
        "quote_ambiguous_scalars",
        "keep_least_privilege_permissions",
        "validate_chart_schema",
        "render_template_output",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/syntax-and-types.md"
    assert result.loaded.actions[2]["path"] == "references/schema-and-validation.md"
    assert result.loaded.actions[3]["path"] == "references/ci-and-platform-boundaries.md"
    assert result.loaded.actions[4]["path"] == "references/security-and-secrets.md"
    assert result.has_behavioral_delta


def test_bash_skill_loads_strict_script_boundary_pattern() -> None:
    """Verify Bash work loads references before writing boundary-sensitive scripts."""
    result = run_recorded_skill_scenario(SCENARIOS / "bash/strict-script-boundaries.yaml")

    assert result.baseline.action_names == (
        "build_command_string",
        "expand_unquoted_arguments",
        "ignore_pipeline_status",
        "remove_temp_dir_on_success_only",
        "respond",
    )
    assert result.loaded.action_names == (
        "inspect_repo_shell_conventions",
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "select_declared_bash_version",
        "build_argument_array",
        "handle_pipeline_failure",
        "register_exit_cleanup",
        "add_bats_edge_cases",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[1]["path"] == "references/configuration-and-portability.md"
    assert result.loaded.actions[2]["path"] == "references/quoting-and-data.md"
    assert result.loaded.actions[3]["path"] == "references/errors-and-cleanup.md"
    assert result.loaded.actions[4]["path"] == "references/testing-and-tooling.md"
    assert result.has_behavioral_delta


def test_scala_skill_loads_strict_scala3_domain_boundary_pattern() -> None:
    """Verify Scala work loads references before changing Scala 3 domain boundaries."""
    result = run_recorded_skill_scenario(SCENARIOS / "scala/strict-scala3-domain-boundaries.yaml")

    assert result.baseline.action_names == (
        "reuse_anyval_newtype",
        "keep_implicit_decoder",
        "model_status_with_verbose_sealed_hierarchy",
        "hide_import_failure",
        "run_broad_sbt_test",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "select_scala_dialect",
        "model_opaque_identifier",
        "choose_enum_for_closed_sum",
        "define_given_decoder",
        "model_typed_import_failure",
        "add_domain_boundary_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration.md"
    assert result.loaded.actions[1]["path"] == "references/types-and-contextual-abstractions.md"
    assert result.loaded.actions[2]["path"] == "references/effects-errors-and-resources.md"
    assert result.loaded.actions[3]["path"] == "references/framework-and-platform-boundaries.md"
    assert result.has_behavioral_delta


def test_lua_skill_loads_embedded_coroutine_boundary_pattern() -> None:
    """Verify Lua work loads references before changing an embedded boundary."""
    result = run_recorded_skill_scenario(SCENARIOS / "lua/embedded-coroutine-boundary.yaml")

    assert result.baseline.action_names == (
        "mutate_request_table",
        "create_implicit_global_module",
        "expose_mutable_metatable",
        "ignore_coroutine_resume_status",
        "open_all_standard_libraries",
        "run_broad_test_suite",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "select_lua_runtime",
        "validate_and_copy_input_table",
        "return_explicit_module_table",
        "define_locked_metatable_protocol",
        "check_coroutine_resume_status",
        "expose_narrow_host_api",
        "add_boundary_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration-and-modules.md"
    assert result.loaded.actions[1]["path"] == "references/tables-types-and-metatables.md"
    assert result.loaded.actions[2]["path"] == "references/coroutines-and-concurrency.md"
    assert result.loaded.actions[3]["path"] == "references/embedding-and-platform-boundaries.md"
    assert result.has_behavioral_delta


def test_objc_skill_loads_mixed_memory_block_interop_pattern() -> None:
    """Verify Objective-C work resolves ownership and mixed-language boundaries."""
    result = run_recorded_skill_scenario(SCENARIOS / "objc/mixed-memory-block-interop.yaml")

    assert result.baseline.action_names == (
        "assume_arc_for_all_files",
        "store_block_with_strong_cycle",
        "leave_header_unannotated",
        "use_exception_for_recoverable_error",
        "run_whole_scheme_tests",
        "respond",
    )
    assert result.loaded.action_names == (
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "load_reference",
        "detect_memory_management_mode",
        "use_copy_block_property",
        "break_block_owner_cycle",
        "annotate_nullability_and_generics",
        "return_nserror",
        "add_focused_objc_and_swift_tests",
        "run_validation",
        "respond",
    )
    assert result.loaded.actions[0]["path"] == "references/configuration-and-language-modes.md"
    assert result.loaded.actions[1]["path"] == "references/ownership-and-lifetimes.md"
    assert result.loaded.actions[2]["path"] == "references/blocks-and-concurrency.md"
    assert result.loaded.actions[3]["path"] == "references/foundation-and-api-design.md"
    assert result.loaded.actions[4]["path"] == "references/swift-and-c-family-interop.md"
    assert result.has_behavioral_delta


def test_pipelines_and_cron_selects_current_automation_paths() -> None:
    result = run_recorded_skill_scenario(
        SCENARIOS / "pipelines-and-cron/select-automation-path.yaml"
    )

    assert result.baseline.action_names == (
        "run_gobby_build",
        "write_agent_definition",
        "respond",
    )
    assert result.loaded.action_names == (
        "classify_automation_path",
        "write_pipeline_yaml",
        "validate_pipeline_definition",
        "create_pipeline",
        "run_pipeline",
        "get_pipeline_status",
        "create_cron_job",
        "run_cron_job",
        "list_cron_runs",
        "respond",
    )
    assert result.loaded.actions[0]["deterministic"] == "pipeline"
    assert result.loaded.actions[0]["scheduled"] == "cron"
    assert result.loaded.actions[0]["task_lifecycle"] == "build_dispatch"
    assert result.has_behavioral_delta
