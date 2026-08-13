from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from organizations.models import Organization
from users.models import User
from uploads.services import UploadService
from ai_engine.models import AIAnalysisJob, AIInsight
from ai_engine.services import AIEngineService, _persist_insights


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


class ConversationWideFileVisibilityTests(TestCase):
    """
    A file uploaded in turn 1 must stay usable in a later, file-less turn.

    Previously the "[File: id=X ...]" hint that tells the model which
    file_id to call read_file on was only injected when the CURRENT
    message's attachments resolved to a batch — never persisted anywhere
    conversation history is rebuilt from (chat.services.ChatService.
    build_conversation_history reads plain ChatMessage.content only) — so a
    follow-up turn with no re-attached file had no way to learn an earlier
    file_id existed at all, and tool_names was silently `[]` too (which
    happened to still allow every tool through, due to an unrelated empty-
    list-is-falsy quirk in tools.services._load_tool_map, but that's an
    accident, not the intended "no files, no tools" behavior it looked like).
    """

    def setUp(self):
        from chat.models import ChatConversation, ChatMessage, ChatMessageAttachment
        from uploads.models import UploadedFile

        self.user = User.objects.create_user(
            email="filevis@example.com", password="pw12345!",
            first_name="F", last_name="V",
        )
        self.conv = ChatConversation.objects.create(user=self.user, title="Test")
        self.batch = UploadService.create_batch(label="t", user=self.user)
        self.uf = UploadedFile.objects.create(
            batch=self.batch, original_filename="invoice.pdf",
            file_size_bytes=10, extension="pdf",
            parse_status="parsed", extracted_text="invoice content",
            detected_type="pdf",
        )

        # Turn 1: user message with the file attached.
        turn1 = ChatMessage.objects.create(
            conversation=self.conv, role="user", content="here is a file",
        )
        ChatMessageAttachment.objects.create(
            message=turn1, filename="invoice.pdf", file_type="pdf",
            attachment_type="user_upload", uploaded_file=self.uf,
        )
        ChatMessage.objects.create(
            conversation=self.conv, role="assistant", content="Got it.",
        )

    def test_file_less_followup_turn_still_sees_the_earlier_file(self):
        """Turn 3 has no batch/attachment of its own, but the conversation
        has file_id=self.uf.pk from turn 1 — the agent must still be told
        about it and still have read_file/export_file available."""
        with patch("tools.services.ToolService.run", return_value=("ok", [])) as mock_run:
            AIEngineService.handle_chat_message(
                user=self.user,
                message="summarize the file I sent earlier",
                batch=None,
                conversation=self.conv,
                conversation_history=[],
            )

        self.assertTrue(mock_run.called)
        user_message = mock_run.call_args.kwargs["user_message"]
        self.assertIn(f"file_id={self.uf.pk}", user_message)
        self.assertIn("invoice.pdf", user_message)

        tool_names = mock_run.call_args.kwargs["tool_names"]
        self.assertIn("read_file", tool_names)
        self.assertIn("export_file", tool_names)

    def test_current_turn_attachment_not_duplicated_in_the_other_files_list(self):
        """If the SAME file is attached again this turn, it shouldn't also
        show up in the "other files" listing — that's for files NOT already
        covered by the current-turn injection."""
        with patch("tools.services.ToolService.run", return_value=("ok", [])) as mock_run:
            AIEngineService.handle_chat_message(
                user=self.user,
                message="what's in this file",
                batch=self.batch,
                conversation=self.conv,
                conversation_history=[],
            )
        user_message = mock_run.call_args.kwargs["user_message"]
        # The current-turn single-file injection uses "id={pk}" (no "file_"
        # prefix); the "Other files available" block (which does use
        # "file_id={pk}") must not appear at all here, since the only file
        # in the conversation is already covered by the current-turn block.
        self.assertIn(f"id={self.uf.pk}", user_message)
        self.assertNotIn("Other files available", user_message)

    def test_no_conversation_does_not_crash(self):
        """Stateless callers that don't pass a conversation must still work
        exactly as before — conversation=None is a supported, safe default."""
        with patch("tools.services.ToolService.run", return_value=("ok", [])) as mock_run:
            AIEngineService.handle_chat_message(
                user=self.user,
                message="hello",
                batch=None,
                conversation=None,
                conversation_history=[],
            )
        self.assertTrue(mock_run.called)
        user_message = mock_run.call_args.kwargs["user_message"]
        self.assertNotIn("Other files available", user_message)


class OrgIsolationTests(TestCase):
    """
    AIAnalysisJob/AIInsight became org-scoped in Phase 3 — previously scoped
    to the exact requesting user (jobs) or completely unscoped (the two
    stats/recent-insights endpoints, a real pre-existing data leak). Verify
    same-org visibility, cross-org isolation, and that aggregate endpoints
    only count this org's data.
    """

    def setUp(self):
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        self.requester_a = User.objects.create_user(
            email="requester_a@example.com", password="pw12345!",
            first_name="R", last_name="A", organization=self.org_a,
        )
        self.teammate_a = User.objects.create_user(
            email="teammate_a@example.com", password="pw12345!",
            first_name="T", last_name="A", organization=self.org_a,
        )
        self.user_b = User.objects.create_user(
            email="user_b@example.com", password="pw12345!",
            first_name="U", last_name="B", organization=self.org_b,
        )

        self.batch_a = UploadService.create_batch(label="Org A batch", user=self.requester_a)
        self.job_a = AIEngineService.create_job(
            batch=self.batch_a, task_type="custom", requested_by=self.requester_a,
        )
        self.job_a.status = "done"
        self.job_a.save(update_fields=["status"])
        AIInsight.objects.create(
            job=self.job_a, insight_type="summary_point", severity="info",
            title="Org A insight", detail="...",
        )

        self.client = APIClient()

    def test_job_organization_is_set_from_requested_by_at_creation(self):
        self.assertEqual(self.job_a.organization_id, self.org_a.pk)

    def test_teammate_in_same_org_sees_job_they_did_not_request(self):
        self.client.force_authenticate(user=self.teammate_a)
        resp = self.client.get("/api/ai/jobs/")
        self.assertEqual(resp.status_code, 200)
        ids = [j["id"] for j in resp.json()]
        self.assertIn(self.job_a.pk, ids)

        detail = self.client.get(f"/api/ai/jobs/{self.job_a.pk}/")
        self.assertEqual(detail.status_code, 200)

    def test_cross_org_user_sees_nothing(self):
        self.client.force_authenticate(user=self.user_b)
        resp = self.client.get("/api/ai/jobs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

        detail = self.client.get(f"/api/ai/jobs/{self.job_a.pk}/")
        self.assertEqual(detail.status_code, 404)

    def test_insights_endpoint_is_org_scoped(self):
        self.client.force_authenticate(user=self.teammate_a)
        resp = self.client.get(f"/api/ai/jobs/{self.job_a.pk}/insights/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

        self.client.force_authenticate(user=self.user_b)
        resp = self.client.get(f"/api/ai/jobs/{self.job_a.pk}/insights/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_stats_view_only_counts_this_org(self):
        """AIEngineStatsView was completely unscoped before Phase 3 — a
        second org's jobs must not inflate this org's counts."""
        AIEngineService.create_job(
            batch=UploadService.create_batch(label="Org B batch", user=self.user_b),
            task_type="custom", requested_by=self.user_b,
        )

        self.client.force_authenticate(user=self.requester_a)
        resp = self.client.get("/api/ai/stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["analyses_today"], 1)

    def test_recent_insights_view_is_org_scoped(self):
        """AIRecentInsightsView was completely unscoped before Phase 3 —
        must not leak another org's insight titles/details."""
        job_b = AIEngineService.create_job(
            batch=UploadService.create_batch(label="Org B batch", user=self.user_b),
            task_type="custom", requested_by=self.user_b,
        )
        AIInsight.objects.create(
            job=job_b, insight_type="summary_point", severity="info",
            title="Org B secret finding", detail="...",
        )

        self.client.force_authenticate(user=self.requester_a)
        resp = self.client.get("/api/ai/insights/recent/")
        self.assertEqual(resp.status_code, 200)
        titles = [i["title"] for i in resp.json()["insights"]]
        self.assertIn("Org A insight", titles)
        self.assertNotIn("Org B secret finding", titles)
