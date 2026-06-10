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
        "compact_self_or_wait_for_agent",
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
    """Verify TypeScript work loads references before strict code and config changes."""
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
