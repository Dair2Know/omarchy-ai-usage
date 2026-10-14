import importlib.util
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import contextlib
import io


PLUGIN = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


usage = load_module("ai_usage_read_usage", "read_usage.py")
codex = load_module("ai_usage_collect_codex", "collect_codex.py")


class PacingTests(unittest.TestCase):
    def test_pacing_uses_elapsed_window_and_current_average_forecast(self):
        now = 1_000_000
        limit = usage.normalize_limit({
            "label": "Primary · 60m window",
            "percent": 0.60,
            "resetsAt": now + 1800,
            "windowDurationMins": 60,
        }, now, now)
        self.assertEqual(limit["durationSeconds"], 3600)
        self.assertEqual(limit["expectedPercent"], 0.5)
        self.assertEqual(limit["pace"], "ahead")
        self.assertEqual(limit["paceGapPp"], 10.0)
        self.assertEqual(limit["projectionPercent"], 1.2)

    def test_duration_inference_requires_explicit_units(self):
        self.assertEqual(usage.duration_seconds({"label": "Session (5-hour)"}), 5 * 3600)
        self.assertEqual(usage.duration_seconds({"label": "Fable Weekly"}), 7 * 86400)
        self.assertIsNone(usage.duration_seconds({"label": "Monthly"}))
        self.assertEqual(usage.duration_seconds({"label": "Custom", "windowDurationMins": 95}), 5700)
        self.assertIsNone(usage.duration_seconds({"label": "999999999999999999999999999999999999 weeks"}))
        self.assertIsNone(usage.duration_seconds({"label": "Weekly", "windowDurationMins": 0}))

    def test_expired_window_keeps_reported_use_and_disables_pacing(self):
        now = 1_000_000
        limit = usage.normalize_limit({
            "label": "Weekly",
            "percent": 0.72,
            "resetsAt": now - 1,
            "windowDurationMins": 10080,
        }, now, now - 100)
        self.assertTrue(limit["resetExpired"])
        self.assertEqual(limit["percent"], 0.72)
        self.assertIsNone(limit["expectedPercent"])
        self.assertIsNone(limit["projectionPercent"])
        self.assertEqual(limit["pace"], "unknown")

    def test_missing_window_fields_remain_unknown(self):
        now = 1_000_000
        limit = usage.normalize_limit({"label": "Provider quota", "percent": 0.35}, now, now)
        self.assertIsNone(limit["resetsAt"])
        self.assertIsNone(limit["durationSeconds"])
        self.assertIsNone(limit["expectedPercent"])
        self.assertIsNone(limit["projectionPercent"])


class DiscoveryTests(unittest.TestCase):
    def test_missing_directories_produce_no_fake_providers(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            result = usage.build_snapshot(1_000_000, base / "missing", base / "none.json", base / "also-missing")
        self.assertEqual({provider["providerId"] for provider in result["providers"]}, {"claude", "codex"})
        self.assertTrue(all(not provider["limits"] for provider in result["providers"]))

    def test_malformed_provider_record_is_an_error_not_zero_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "bad.json").write_text("{bad", encoding="utf-8")
            records = usage.discover_records(base, "test", 1_000_000)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["isError"])
        self.assertEqual(records[0]["limits"], [])
        self.assertEqual(records[0]["statusText"], "Provider record could not be read")

    def test_unconfigured_not_ready_placeholder_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "fireworks.json").write_text(json.dumps({
                "id": "fireworks", "name": "Fireworks", "ready": False,
                "usageStatusText": "Fireworks unavailable", "limits": [],
            }), encoding="utf-8")
            self.assertEqual(usage.discover_records(base, "test", 1_000_000), [])

    def test_extra_provider_is_discovered_and_keeps_unknown_grants_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            supplemental = base / "providers.d"
            supplemental.mkdir()
            (supplemental / "future.json").write_text(json.dumps({
                "schemaVersion": 1,
                "id": "future-ai",
                "name": "Future AI",
                "updatedAt": "1970-01-12T13:46:40+00:00",
                "limits": [{"label": "30m window", "percent": 0.2, "resetsAt": 1_001_800,
                            "windowDurationSeconds": 1800}],
            }), encoding="utf-8")
            result = usage.build_snapshot(1_000_000, base / "none", base / "absent", supplemental)
        provider = next(item for item in result["providers"] if item["providerId"] == "future-ai")
        self.assertEqual(provider["providerId"], "future-ai")
        self.assertTrue(provider["limits"])
        self.assertNotIn("bankedResets", provider["supplemental"])
        self.assertNotIn("nextFreeResetAt", provider["supplemental"])

    def test_codex_saved_token_exposes_only_period_dates(self):
        with tempfile.TemporaryDirectory() as temp:
            auth_path = Path(temp) / "auth.json"
            service = {
                "chatgpt_plan_type": "plus",
                "chatgpt_subscription_active_start": "2026-01-05T09:00:00+00:00",
                "chatgpt_subscription_active_until": "2026-02-05T09:00:00+00:00",
                "chatgpt_subscription_last_checked": "2026-01-06T12:00:00.123456+00:00",
                "account_id": "must-not-be-copied",
            }
            payload = {"https://api.openai.com/auth": service, "email": "must-not-be-copied"}
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            auth_path.write_text(json.dumps({"tokens": {"id_token": "header." + encoded + ".signature"}}), encoding="utf-8")
            result = usage.read_codex_saved_subscription(auth_path)
        self.assertEqual(result["periodEndsAt"], "2026-02-05T09:00:00+00:00")
        self.assertEqual(result["periodSourceAt"], "2026-01-06T12:00:00.123456+00:00")
        self.assertEqual(result["periodPlan"], "plus")
        self.assertNotIn("account_id", result)
        self.assertNotIn("email", result)

    def test_codex_saved_token_malformed_or_missing_periods_stays_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            auth_path = Path(temp) / "auth.json"
            auth_path.write_text(json.dumps({"tokens": {"id_token": "not.a.jwt.token"}}), encoding="utf-8")
            self.assertEqual(usage.read_codex_saved_subscription(auth_path), {})
            payload = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}).encode()).decode().rstrip("=")
            auth_path.write_text(json.dumps({"tokens": {"id_token": "header." + payload + ".signature"}}), encoding="utf-8")
            self.assertEqual(usage.read_codex_saved_subscription(auth_path), {})

    def test_subscription_metadata_timestamp_does_not_freshen_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex.json"
            path.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T10:00:00Z",
                "tierLabel": "plus",
                "limits": [{"label": "Weekly", "percent": 0.2, "resetsAt": "2026-10-20T10:00:00Z", "windowDurationMins": 10080}],
            }), encoding="utf-8")
            with mock.patch.object(usage, "read_codex_saved_subscription", return_value={
                "periodEndsAt": "2026-02-05T09:00:00+00:00",
                "periodSourceAt": "2026-01-06T12:00:00+00:00",
                "periodPlan": "plus",
            }):
                record = usage.read_record(path, "Codex account usage", 1_792_000_000)
        self.assertEqual(record["updatedAt"], "2026-10-14T10:00:00+00:00")
        self.assertEqual(record["subscription"]["plan"], "plus")
        self.assertEqual(record["subscription"]["periodSourceAt"], "2026-01-06T12:00:00+00:00")
        self.assertEqual(record["subscriptionUpdatedAt"], record["updatedAt"])

    def test_saved_period_is_omitted_when_plan_no_longer_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex.json"
            path.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T10:00:00Z",
                "tierLabel": "pro", "limits": [],
            }), encoding="utf-8")
            with mock.patch.object(usage, "read_codex_saved_subscription", return_value={
                "periodEndsAt": "2026-02-05T09:00:00+00:00",
                "periodSourceAt": "2026-01-06T12:00:00+00:00",
                "periodPlan": "plus",
            }):
                record = usage.read_record(path, "Codex account usage", 1_792_000_000)
        self.assertNotIn("periodEndsAt", record["subscription"])
        self.assertNotIn("periodSourceAt", record["subscription"])

    def test_explicit_subscription_override_merges_without_freshening_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            usage_path, subscription_path = base / "usage.json", base / "subscription.json"
            usage_path.write_text(json.dumps({
                "id": "future-ai", "name": "Future AI", "updatedAt": "2026-10-14T10:00:00Z",
                "tierLabel": "Premium",
                "limits": [{"label": "Weekly", "percent": 0.4, "resetsAt": "2026-10-20T10:00:00Z", "windowDurationMins": 10080}],
            }), encoding="utf-8")
            subscription_path.write_text(json.dumps({
                "id": "future-ai", "name": "Future AI", "subscriptionUpdatedAt": "2026-10-14T12:00:00Z",
                "subscription": {"plan": "Premium", "periodEndsAt": "2026-11-01T00:00:00Z",
                                 "periodSourceAt": "2026-10-14T12:00:00Z", "source": "user supplied"},
            }), encoding="utf-8")
            with mock.patch.object(usage, "read_codex_saved_subscription", return_value={}):
                first = usage.read_record(usage_path, "usage", 1_792_000_000)
                second = usage.read_record(subscription_path, "subscription", 1_792_000_000)
        provider = usage.merge_records([first, second])[0]
        self.assertEqual(provider["updatedAt"], first["updatedAt"])
        self.assertEqual(provider["limits"][0]["percent"], 0.4)
        self.assertEqual(provider["subscription"]["periodEndsAt"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(provider["subscription"]["source"], "user supplied")


    def test_newer_plan_only_record_keeps_explicit_period_end(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            explicit_path, plan_path = base / "explicit.json", base / "plan.json"
            explicit_path.write_text(json.dumps({
                "id": "future-ai", "name": "Future AI", "subscriptionUpdatedAt": "2026-10-14T10:00:00Z",
                "subscription": {"plan": "Premium", "periodEndsAt": "2026-11-01T00:00:00Z",
                                 "periodSourceAt": "2026-10-14T10:00:00Z", "source": "user supplied"},
            }), encoding="utf-8")
            plan_path.write_text(json.dumps({
                "id": "future-ai", "name": "Future AI", "updatedAt": "2026-10-14T12:00:00Z",
                "tierLabel": "Premium", "limits": [],
            }), encoding="utf-8")
            with mock.patch.object(usage, "read_codex_saved_subscription", return_value={}):
                explicit = usage.read_record(explicit_path, "subscription", 1_792_000_000)
                plan = usage.read_record(plan_path, "usage", 1_792_000_000)
        provider = usage.merge_records([explicit, plan])[0]
        self.assertEqual(provider["subscription"]["periodEndsAt"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(provider["subscription"]["source"], "user supplied")

    def test_newer_different_codex_plan_drops_saved_period_date(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old_path, new_path = base / "old.json", base / "new.json"
            old_path.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T10:00:00Z",
                "tierLabel": "plus", "limits": [],
            }), encoding="utf-8")
            new_path.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T12:00:00Z",
                "tierLabel": "pro", "limits": [],
            }), encoding="utf-8")
            saved = {"periodPlan": "plus", "periodEndsAt": "2026-02-05T09:00:00+00:00",
                     "periodSourceAt": "2026-01-06T12:00:00+00:00"}
            with mock.patch.object(usage, "read_codex_saved_subscription", side_effect=[saved, {}]):
                old = usage.read_record(old_path, "usage", 1_792_000_000)
                new = usage.read_record(new_path, "usage", 1_792_000_000)
        provider = usage.merge_records([old, new])[0]
        self.assertEqual(provider["tierLabel"], "pro")
        self.assertNotIn("periodEndsAt", provider["subscription"])


class CodexNormalizationTests(unittest.TestCase):
    def test_multi_bucket_windows_keep_numeric_durations_and_do_not_duplicate_primary(self):
        result = codex.extract_windows({
            "rateLimits": {"primary": {"usedPercent": 55, "windowDurationMins": 300,
                                         "resetsAt": 1_001_800}},
            "rateLimitsByLimitId": {
                "codex": {"limitName": "Codex", "planType": "pro", "primary": {
                    "usedPercent": 55, "windowDurationMins": 300, "resetsAt": 1_001_800}},
                "special": {"limitName": "Special", "primary": {
                    "usedPercent": 25, "windowDurationMins": 90, "resetsAt": 1_005_400}},
            },
        })
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["windowDurationMins"], 300)
        self.assertEqual(result[1]["windowDurationMins"], 90)
        self.assertEqual(codex.window_label("Primary", {"windowDurationMins": 10080}), "Primary · Weekly")

    def test_reset_credit_output_omits_opaque_ids_and_filters_unavailable_types(self):
        output = codex.extract_reset_credits({"rateLimitResetCredits": {
            "availableCount": 2,
            "credits": [
                {"id": "secret-id", "title": "Full reset", "status": "available",
                 "resetType": "codexRateLimits", "grantedAt": 1_000, "expiresAt": 2_000},
                {"id": "other", "title": "Other", "status": "available",
                 "resetType": "unknown", "grantedAt": 1_000},
            ],
        }}, "1970-01-01T00:00:00+00:00")
        self.assertEqual(output["availableCount"], 2)
        self.assertEqual(len(output["details"]), 1)
        self.assertNotIn("id", output["details"][0])
        self.assertTrue(output["detailsAvailable"])

    def test_missing_reset_credit_summary_stays_unreported(self):
        self.assertIsNone(codex.extract_reset_credits({}, "now"))

    def test_newer_grant_only_record_does_not_freshen_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old = base / "base.json"
            new = base / "supplement.json"
            old.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T10:00:00Z",
                "limits": [{"label": "Weekly", "percent": 0.2, "resetsAt": "2026-10-20T10:00:00Z", "windowDurationMins": 10080}],
            }), encoding="utf-8")
            new.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T11:00:00Z",
                "supplementalUpdatedAt": "2026-10-14T11:00:00Z",
                "supplemental": {"bankedResets": {"availableCount": 2, "sourceAt": "2026-10-14T11:00:00Z"}},
            }), encoding="utf-8")
            first = usage.read_record(old, "Omarchy usage record", 1_792_000_000)
            second = usage.read_record(new, "Supplemental provider record", 1_792_000_000)
        merged = usage.merge_records([first, second])[0]
        self.assertEqual(merged["updatedAt"], first["updatedAt"])
        self.assertEqual(merged["limits"][0]["percent"], 0.2)
        self.assertEqual(merged["supplemental"]["bankedResets"]["availableCount"], 2)

    def test_fresh_own_collector_replaces_stale_base_error(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            base_file, own_file = base / "base.json", base / "own.json"
            base_file.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T10:00:00Z",
                "status": "error", "statusText": "old error", "limits": [],
            }), encoding="utf-8")
            own_file.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T11:00:00Z",
                "status": "ok", "replaceLimits": True,
                "limits": [{"label": "Weekly", "percent": 0.3, "resetsAt": "2026-10-20T11:00:00Z", "windowDurationMins": 10080}],
            }), encoding="utf-8")
            records = [usage.read_record(base_file, "Omarchy usage record", 1_792_000_000),
                       usage.read_record(own_file, "Codex account usage", 1_792_000_000)]
        merged = usage.merge_records(records)[0]
        self.assertFalse(merged["isError"])
        self.assertEqual(merged["statusText"], "")
        self.assertEqual(merged["limits"][0]["percent"], 0.3)

    def test_successful_codex_sample_survives_a_newer_empty_scan_error(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            failed_scan, successful_sample = base / "scan.json", base / "own.json"
            failed_scan.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T12:42:18Z",
                "usageStatusText": "Codex limits unavailable", "limits": [],
            }), encoding="utf-8")
            successful_sample.write_text(json.dumps({
                "id": "codex", "name": "Codex", "updatedAt": "2026-10-14T12:40:49Z",
                "replaceLimits": True, "source": "Codex account usage",
                "limits": [{"label": "Codex · Primary · Weekly", "percent": 0.28,
                            "resetsAt": "2026-10-20T12:40:49Z", "windowDurationMins": 10080}],
                "supplemental": {"bankedResets": {"availableCount": 3,
                    "sourceAt": "2026-10-14T12:40:49Z"}},
            }), encoding="utf-8")
            now = 1_792_000_000
            records = [usage.read_record(failed_scan, "Omarchy usage record", now),
                       usage.read_record(successful_sample, "Codex account usage", now)]
        provider = usage.merge_records(records)[0]
        self.assertEqual(provider["updatedAt"], "2026-10-14T12:40:49+00:00")
        self.assertEqual(provider["limits"][0]["percent"], 0.28)
        self.assertFalse(provider["isError"])
        self.assertEqual(provider["latestErrorText"], "Codex limits unavailable")
        self.assertEqual(provider["supplemental"]["bankedResets"]["availableCount"], 3)

    def test_collector_failure_retains_last_success_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "codex.json"
            previous = {"id": "codex", "name": "Codex", "updatedAt": "2026-10-14T10:00:00Z",
                        "limits": [{"label": "Weekly", "percent": 0.25}], "supplemental": {"bankedResets": {"availableCount": 1}}}
            path.write_text(json.dumps(previous), encoding="utf-8")
            with mock.patch.dict("os.environ", {"AI_USAGE_MONITOR_CODEX_STATE": str(path)}), \
                 mock.patch.object(codex, "collect", side_effect=RuntimeError("offline")), \
                 mock.patch("sys.argv", ["collect_codex.py", "--force"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(codex.main(), 1)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["updatedAt"], previous["updatedAt"])
        self.assertEqual(saved["limits"], previous["limits"])
        self.assertTrue(saved["supplementalError"])
        self.assertNotEqual(saved["lastAttemptAt"], saved["updatedAt"])


if __name__ == "__main__":
    unittest.main()
