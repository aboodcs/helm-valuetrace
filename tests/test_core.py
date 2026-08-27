from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from helm_valuetrace.core import parse_set_arguments, trace_values  # noqa: E402


class TraceValuesTests(unittest.TestCase):
    def test_last_override_wins_and_history_is_preserved(self) -> None:
        result = trace_values(
            default_values={"replicaCount": 1, "image": {"tag": "old"}},
            default_source="values.yaml",
            default_lines={("replicaCount",): 1, ("image", "tag"): 3},
            overrides=[
                (
                    {"replicaCount": 2, "image": {"tag": "dev"}},
                    "values-dev.yaml",
                    {("replicaCount",): 1, ("image", "tag"): 3},
                ),
                (
                    {"replicaCount": 5},
                    "values-prod.yaml",
                    {("replicaCount",): 1},
                ),
            ],
        )

        self.assertEqual(result.values["replicaCount"], 5)
        history = result.history[("replicaCount",)]
        self.assertEqual([assignment.value for assignment in history], [1, 2, 5])
        self.assertEqual(history[-1].location, "values-prod.yaml:1")

    def test_typo_is_reported_with_suggestion(self) -> None:
        result = trace_values(
            default_values={"image": {"repository": "nginx"}},
            default_source="values.yaml",
            default_lines={("image", "repository"): 2},
            overrides=[
                (
                    {"image": {"repostory": "custom"}},
                    "values-prod.yaml",
                    {("image", "repostory"): 2},
                )
            ],
        )

        self.assertEqual(len(result.unknown), 1)
        self.assertEqual(result.unknown[0].key, "image.repostory")
        self.assertEqual(result.unknown[0].suggestion, "image.repository")

    def test_empty_default_mapping_accepts_arbitrary_children(self) -> None:
        result = trace_values(
            default_values={"podAnnotations": {}},
            default_source="values.yaml",
            default_lines={("podAnnotations",): 1},
            overrides=[
                (
                    {"podAnnotations": {"team": "platform"}},
                    "values-prod.yaml",
                    {("podAnnotations", "team"): 2},
                )
            ],
        )

        self.assertEqual(result.unknown, [])
        self.assertEqual(result.values["podAnnotations"]["team"], "platform")

    def test_empty_override_mapping_does_not_clear_default_children(self) -> None:
        result = trace_values(
            default_values={"resources": {"limits": {"cpu": "500m"}}},
            default_source="values.yaml",
            default_lines={("resources", "limits", "cpu"): 3},
            overrides=[
                (
                    {"resources": {"limits": {}}},
                    "values-production.yaml",
                    {("resources", "limits"): 2},
                )
            ],
        )

        self.assertEqual(result.values["resources"]["limits"]["cpu"], "500m")
        self.assertEqual(
            result.history[("resources", "limits", "cpu")][-1].source,
            "values.yaml",
        )

    def test_set_arguments_support_multiple_values(self) -> None:
        parsed = parse_set_arguments(["image.tag=v2,replicaCount=4", "feature.enabled=true"])

        self.assertEqual(parsed[0][0], ("image", "tag"))
        self.assertEqual(parsed[0][1], "v2")
        self.assertEqual(parsed[1][1], 4)
        self.assertIs(parsed[2][1], True)

    def test_set_scalar_types_match_helm_strvals(self) -> None:
        parsed = parse_set_arguments(
            [
                "word=yes,leadingZero=0123,floatValue=1.0,empty=",
                "enabled=TRUE,disabled=false,removed=null,zero=0",
                "positive=42,negative=-7,overflow=9223372036854775808",
            ]
        )
        values = {path[0]: value for path, value, _ in parsed}

        self.assertEqual(values["word"], "yes")
        self.assertEqual(values["leadingZero"], "0123")
        self.assertEqual(values["floatValue"], "1.0")
        self.assertEqual(values["empty"], "")
        self.assertIs(values["enabled"], True)
        self.assertIs(values["disabled"], False)
        self.assertIsNone(values["removed"])
        self.assertEqual(values["zero"], 0)
        self.assertEqual(values["positive"], 42)
        self.assertEqual(values["negative"], -7)
        self.assertEqual(values["overflow"], "9223372036854775808")

    def test_null_removes_existing_defaults_but_preserves_user_only_nulls(self) -> None:
        result = trace_values(
            default_values={
                "nullable": "original",
                "nested": {"removed": "original", "kept": "default"},
            },
            default_source="values.yaml",
            default_lines={
                ("nullable",): 1,
                ("nested", "removed"): 3,
                ("nested", "kept"): 4,
            },
            overrides=[
                (
                    {
                        "nullable": None,
                        "nested": {"removed": None, "userOnlyNull": None},
                    },
                    "override.yaml",
                    {
                        ("nullable",): 1,
                        ("nested", "removed"): 3,
                        ("nested", "userOnlyNull"): 4,
                    },
                )
            ],
        )

        self.assertNotIn("nullable", result.values)
        self.assertNotIn("removed", result.values["nested"])
        self.assertEqual(result.values["nested"]["kept"], "default")
        self.assertIsNone(result.values["nested"]["userOnlyNull"])
        self.assertEqual(
            [assignment.value for assignment in result.history[("nullable",)]],
            ["original", None],
        )

    def test_defaults_are_recoalesced_after_user_value_type_changes(self) -> None:
        result = trace_values(
            default_values={"service": {"port": 80, "type": "ClusterIP"}},
            default_source="values.yaml",
            default_lines={("service", "port"): 2, ("service", "type"): 3},
            overrides=[
                (
                    {"service": "disabled"},
                    "first.yaml",
                    {("service",): 1},
                ),
                (
                    {"service": {"port": 8080}},
                    "second.yaml",
                    {("service", "port"): 2},
                ),
            ],
        )

        self.assertEqual(
            result.values["service"],
            {"port": 8080, "type": "ClusterIP"},
        )

    def test_chart_default_null_handling_tracks_helm_major_version(self) -> None:
        common = {
            "default_values": {"defaultNull": None, "present": "value"},
            "default_source": "values.yaml",
            "default_lines": {("defaultNull",): 1, ("present",): 2},
            "overrides": [],
        }

        helm3 = trace_values(**common, helm_major_version=3)
        helm4 = trace_values(**common, helm_major_version=4)

        self.assertIn("defaultNull", helm3.values)
        self.assertIsNone(helm3.values["defaultNull"])
        self.assertNotIn("defaultNull", helm4.values)
        self.assertEqual(helm4.values["present"], "value")

    def test_reference_values_are_not_merged_and_missing_keys_are_reported(self) -> None:
        result = trace_values(
            default_values={},
            default_source="chart/values.yaml",
            default_lines={},
            overrides=[
                (
                    {"image": {"tag": "staging"}, "autoscaling": {"enabled": True}},
                    "values-staging.yaml",
                    {("image", "tag"): 2, ("autoscaling", "enabled"): 5},
                )
            ],
            reference_values={"image": {"repository": "nginx", "tag": "stable"}},
            reference_source="values-production.yaml",
        )

        self.assertNotIn("repository", result.values["image"])
        self.assertEqual([item.key for item in result.unknown], ["autoscaling.enabled"])
        self.assertEqual([item.key for item in result.missing], ["image.repository"])
        self.assertEqual(result.missing[0].reference, "values-production.yaml")


if __name__ == "__main__":
    unittest.main()
