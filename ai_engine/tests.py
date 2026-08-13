from django.test import TestCase

from users.models import User
from uploads.services import UploadService
from ai_engine.models import AIAnalysisJob, AIInsight
from ai_engine.services import _persist_insights


class PersistInsightsUnwrapsPromptTransformResultsTests(TestCase):
    """
    tools.services.ToolService.run() stores a prompt_transform tool's result
    verbatim as ToolCall.result — the wrapper _call_prompt_transform()
    returns, {"ok", "result": <raw text>, "structured": {...the actual
    parsed rows/anomalies/narrative...}, ...}. _persist_insights used to
    read "rows"/"anomalies"/"narrative" off the top level of that dict, which
    only builtin-handler results (e.g. reconcile_ura_vs_acon) actually have —
    for every prompt_transform tool (reconcile_datasets, flag_anomalies,
    summarise_batch — i.e. every LLM-driven workflow) those keys live one
    level deeper, under "structured", so no insight was ever created no
    matter what the LLM returned.
    """

    def setUp(self):
        user = User.objects.create_user(
            email="insights@example.com", password="pw12345!",
            first_name="I", last_name="T",
        )
        batch = UploadService.create_batch(label="t", user=user)
        self.job = AIAnalysisJob.objects.create(batch=batch, task_type="reconciliation", status="running")

    def test_reconcile_datasets_shaped_result_produces_variance_insights(self):
        prompt_transform_result = {
            "ok": True,
            "result": "raw llm text",
            "structured": {
                "rows": [
                    {"id": "INV-001", "amount_a": 1000, "amount_b": 1000, "variance": 0, "status": "MATCH"},
                    {"id": "INV-002", "amount_a": 2000, "amount_b": 1950, "variance": 50, "status": "VARIANCE"},
                    {"id": "INV-003", "amount_a": 3000, "amount_b": None, "variance": None, "status": "MISSING_IN_B"},
                ],
                "summary": "...",
            },
            "input_truncated": False,
        }
        _persist_insights(self.job, [prompt_transform_result])
        insights = list(AIInsight.objects.filter(job=self.job))
        self.assertEqual(len(insights), 2, "MATCH should not produce an insight; VARIANCE and MISSING_IN_B should")
        statuses = {i.title for i in insights}
        self.assertEqual(statuses, {"VARIANCE", "MISSING_IN_B"})

    def test_summarise_batch_shaped_result_produces_summary_insight(self):
        prompt_transform_result = {
            "ok": True,
            "result": "raw llm text",
            "structured": {"narrative": "Batch contains 2 files, both parsed successfully."},
        }
        _persist_insights(self.job, [prompt_transform_result])
        insights = list(AIInsight.objects.filter(job=self.job, insight_type="summary_point"))
        self.assertEqual(len(insights), 1)
        self.assertEqual(insights[0].detail, "Batch contains 2 files, both parsed successfully.")

    def test_builtin_handler_shaped_result_still_works(self):
        """Top-level (non-wrapped) results — e.g. reconcile_ura_vs_acon — must
        keep working; the fix must not regress the pre-existing shape."""
        builtin_result = {
            "ok": True,
            "rows": [{"id": "FR-1", "amount_a": 500, "amount_b": 400, "variance": 100, "status": "VARIANCE"}],
        }
        _persist_insights(self.job, [builtin_result])
        insights = list(AIInsight.objects.filter(job=self.job))
        self.assertEqual(len(insights), 1)
        self.assertEqual(insights[0].title, "VARIANCE")

    def test_failed_result_produces_no_insights(self):
        _persist_insights(self.job, [{"ok": False, "error": "boom"}])
        self.assertEqual(AIInsight.objects.filter(job=self.job).count(), 0)
