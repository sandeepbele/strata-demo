import unittest
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic_ai.models.test import TestModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

from backend.reviewer.agent import (
    ROOT, INSTRUCTIONS, Citation, ImpactAssessment, ReviewContext, build_agent,
    load_obligations, read_change, read_provider_settings, read_source_window, review,
    search_changes, serialize_assessment, validate_assessment,
)


class ReviewerFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = ReviewContext.load(ROOT / "data/seed/projects/project-1", ROOT / "data/seed/dockets/R.25-06-019")
        cls.obligations = load_obligations(ROOT / "data/seed/projects/project-1/obligations.md")

    def test_fixture_and_search_cover_key_changes(self):
        self.assertEqual([item.id for item in self.obligations], ["OBL-01", "OBL-02", "OBL-03"])
        schedule = search_changes(self.data, "June 1 2031 procurement 2032 MW NQC", 12)
        storage = search_changes(self.data, "one-quarter long-duration storage clean firm", 8)
        self.assertIn("change-0248", [item["change_id"] for item in schedule])
        self.assertIn("change-0249", [item["change_id"] for item in storage])
        self.assertEqual(search_changes(self.data, "portable ladder inspection"), [])

        obligation = self.obligations[0]
        results = search_changes(
            self.data, obligation.title, limit=8,
            context_text=obligation.title + " " + obligation.text,
        )
        self.assertIn("change-0248", [item["change_id"] for item in results])

    def test_short_battery_query_recovers_resource_mix_change_from_obligation_context(self):
        obligation = self.obligations[1]
        results = search_changes(
            self.data, "battery screening eligibility", limit=8,
            context_text=obligation.title + " " + obligation.text,
        )
        match = next((item for item in results if item["change_id"] == "change-0249"), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["match_route"], "obligation_context")

    def test_reviewer_tool_uses_obligation_context_for_short_battery_query(self):
        step = [0]

        def scripted_model(messages, info):
            step[0] += 1
            if step[0] == 1:
                return ModelResponse(parts=[ToolCallPart("search_changes_tool", {
                    "query": "battery screening eligibility", "limit": 8})])
            if step[0] == 2:
                returns = [part for message in messages for part in message.parts
                           if isinstance(part, ToolReturnPart) and part.tool_name == "search_changes_tool"]
                result = next(item for item in returns[0].content if item["change_id"] == "change-0249")
                self.assertEqual(result["match_route"], "obligation_context")
                return ModelResponse(parts=[ToolCallPart("read_change_tool", {"change_id": "change-0249"})])
            if step[0] == 3:
                return ModelResponse(parts=[ToolCallPart("read_source_window_tool", {
                    "change_id": "change-0249", "side": "new"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "project_id": "project-1", "obligation_id": "OBL-02", "outcome": "needs_review",
                "summary": "Review resource criteria", "rationale": "The source changes the resource mix.",
                "proposed_action": "Check the candidate against the defined criteria.",
                "reviewer_role": "Storage development lead",
                "open_questions": ["Does the candidate meet the cited definition?"],
                "citations": [{"change_id": "change-0249", "side": "new"}],
            })])

        assessment = review(self.data, self.obligations[1], build_agent(FunctionModel(scripted_model)))
        self.assertEqual(assessment.outcome, "needs_review")
        self.assertEqual(assessment.citations[0].change_id, "change-0249")

    def test_source_window_is_from_verified_final_pdf(self):
        change = read_change(self.data, "change-0248")
        self.assertEqual(change["old"]["status"], "proposed")
        self.assertEqual(change["new"]["status"], "final")
        window = read_source_window(self.data, "change-0248", "new", 2)
        self.assertEqual(window["pdf_page"], 146)
        self.assertIn("June 1, 2031", " ".join(line["text"] for line in window["lines"]))

    def test_citation_text_is_from_source_and_invalid_id_is_rejected(self):
        assessment = ImpactAssessment(
            project_id="project-1", obligation_id="OBL-01", outcome="affected",
            summary="The plan needs review.", rationale="The final decision adds a 2031 milestone.",
            proposed_action="Update planning schedule after allocation review.",
            reviewer_role="Resource planning manager",
            citations=[Citation(change_id="change-0248", side="new")],
        )
        validate_assessment(assessment, self.data, self.obligations[0])
        citation = serialize_assessment(assessment, self.data)["citations"][0]
        self.assertEqual(citation["version"], "v2")
        self.assertEqual(citation["status"], "final")
        self.assertEqual(citation["pdf_page_start"], 146)
        self.assertEqual(citation["quote"], self.data.by_id["change-0248"]["new"]["text"])
        self.assertFalse(citation["quote_truncated"])
        assessment.citations[0].change_id = "change-does-not-exist"
        with self.assertRaisesRegex(ValueError, "Unknown cited change"):
            validate_assessment(assessment, self.data, self.obligations[0])

    def test_impact_proposals_require_citations_and_review_questions(self):
        assessment = ImpactAssessment(
            project_id="project-1", obligation_id="OBL-01", outcome="affected",
            summary="Review the schedule", rationale="The deadline changed.",
            proposed_action="Check the allocation.", reviewer_role="Planning manager",
            citations=[],
        )
        with self.assertRaisesRegex(ValueError, "requires a citation"):
            validate_assessment(assessment, self.data, self.obligations[0])

        assessment.outcome = "needs_review"
        assessment.citations = [Citation(change_id="change-0248", side="new")]
        with self.assertRaisesRegex(ValueError, "requires an open question"):
            validate_assessment(assessment, self.data, self.obligations[0])

    def test_nonfinal_source_only_allows_consideration(self):
        proposed = ReviewContext.load(ROOT / "data/seed/projects/project-1", ROOT / "data/seed/dockets/R.25-06-019", "v1", "v1a")
        assessment = ImpactAssessment(
            project_id="project-1", obligation_id="OBL-02", outcome="affected",
            summary="Potential change to screening criteria.", rationale="The proposed resource mix differs.",
            proposed_action="Consider the possible impact on the screening note.",
            reviewer_role="Storage development lead", open_questions=["Will this language be adopted?"],
            citations=[Citation(change_id="change-0249", side="new")],
        )
        for status in ("proposed", "draft_proposal"):
            source = ReviewContext(proposed.project, deepcopy(proposed.manifest), proposed.comparison,
                                   proposed.changes, proposed.docket_dir)
            source.manifest["versions"]["v1a"]["status"] = status
            with self.assertRaisesRegex(ValueError, "nonfinal source"):
                validate_assessment(assessment, source, self.obligations[1])
            assessment.outcome = "needs_review"
            assessment.proposed_action = "Revise the screening note now."
            with self.assertRaisesRegex(ValueError, "must consider or prepare"):
                validate_assessment(assessment, source, self.obligations[1])
            assessment.proposed_action = "Consider the possible impact and check the final filing before editing records."
            validate_assessment(assessment, source, self.obligations[1])
            self.assertEqual(serialize_assessment(assessment, source)["review_policy_version"], 1)
            assessment.outcome = "affected"

        final = ImpactAssessment(
            project_id="project-1", obligation_id="OBL-01", outcome="affected",
            summary="The final source changes the schedule.", rationale="The cited milestone is final.",
            proposed_action="Verify the company allocation, then update the planning schedule.",
            reviewer_role="Planning manager", citations=[Citation(change_id="change-0248", side="new")],
        )
        validate_assessment(final, self.data, self.obligations[0])

    def test_agent_rejects_citation_without_tool_use(self):
        expected = {
            "project_id": "project-1", "obligation_id": "OBL-01", "outcome": "not_affected",
            "summary": "No impact found", "rationale": "No candidate inspected.",
            "proposed_action": "Review manually.",
            "reviewer_role": "Resource planning manager", "open_questions": [], "citations": [],
        }
        agent = build_agent(TestModel(call_tools=[], custom_output_args=expected))
        with self.assertRaisesRegex(ValueError, "did not search"):
            review(self.data, self.obligations[0], agent)

    def test_agent_uses_search_change_and_pdf_tools(self):
        calls = []

        def scripted_model(messages, info):
            returns = [part for message in messages for part in message.parts
                       if isinstance(part, ToolReturnPart)]
            if not returns:
                self.assertIn('"old_status": "proposed"', str(messages))
                self.assertIn('"new_status": "final"', str(messages))
                return ModelResponse(parts=[ToolCallPart("search_changes_tool", {
                    "query": "June 1 2031 procurement 2032 MW NQC", "limit": 12})])
            if len(returns) == 1:
                self.assertIn("change-0248", str(returns[0].content))
                calls.append("search")
                return ModelResponse(parts=[ToolCallPart("read_change_tool", {"change_id": "change-0248"})])
            if len(returns) == 2:
                self.assertIn("June 1, 2031", str(returns[1].content))
                calls.append("read_change")
                return ModelResponse(parts=[ToolCallPart("read_source_window_tool", {
                    "change_id": "change-0248", "side": "new", "radius_lines": 2})])
            self.assertIn("June 1, 2031", str(returns[2].content))
            calls.append("source_window")
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "project_id": "project-1", "obligation_id": "OBL-01", "outcome": "affected",
                "summary": "Review the 2031 checkpoint", "rationale": "The final decision adds 2031.",
                "proposed_action": "Verify allocation, then update the schedule.",
                "reviewer_role": "Resource planning manager",
                "open_questions": ["What is Redwood Valley Energy's allocation?"],
                "citations": [{"change_id": "change-0248", "side": "new"}],
            })])

        assessment = review(self.data, self.obligations[0], build_agent(FunctionModel(scripted_model)))
        self.assertEqual(assessment.outcome, "affected")
        self.assertEqual(calls, ["search", "read_change", "source_window"])

    def test_draft_proposal_status_is_sent_in_reviewer_prompt(self):
        draft = ReviewContext(self.data.project, deepcopy(self.data.manifest),
                              self.data.comparison, self.data.changes, self.data.docket_dir)
        draft.manifest['versions']['v2']['status'] = 'draft_proposal'

        def inspect_prompt(messages, info):
            if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
                self.assertIn('"new_status": "draft proposal"', str(messages))
                return ModelResponse(parts=[ToolCallPart('search_changes_tool', {'query': 'procurement'})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                'project_id': 'project-1', 'obligation_id': 'OBL-01', 'outcome': 'not_affected',
                'summary': 'No direct impact found.', 'rationale': 'No cited change selected.',
                'proposed_action': 'Review manually if needed.', 'reviewer_role': 'Record owner',
                'open_questions': [], 'citations': [],
            })])

        self.assertEqual(review(draft, self.obligations[0], build_agent(FunctionModel(inspect_prompt))).outcome,
                         'not_affected')

    def test_instructions_and_tool_descriptions_have_no_fixture_guidance(self):
        banned = ("cpuc", "redwood", "battery", "allocation", "2031", "proposed-to-final", "final decision")
        for term in banned:
            self.assertNotIn(term, INSTRUCTIONS.casefold())

        def inspect_tools(messages, info):
            descriptions = " ".join(tool.description or "" for tool in info.function_tools
                                    if tool.name in {"search_changes_tool", "read_change_tool", "read_source_window_tool"})
            for term in banned:
                self.assertNotIn(term, descriptions.casefold())
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "project_id": "project-1", "obligation_id": "OBL-01", "outcome": "not_affected",
                "summary": "No relationship found", "rationale": "No cited change.",
                "proposed_action": "Review if more evidence appears.",
                "reviewer_role": "Project reviewer", "open_questions": [], "citations": [],
            })])

        result = build_agent(FunctionModel(inspect_tools)).run_sync("check metadata", deps=self.data)
        self.assertEqual(result.output.outcome, "not_affected")

    def test_model_supplied_quote_is_not_trusted(self):
        step = [0]

        def scripted_model(messages, info):
            step[0] += 1
            if step[0] == 1:
                return ModelResponse(parts=[ToolCallPart("search_changes_tool", {"query": "2031 procurement NQC"})])
            if step[0] == 2:
                return ModelResponse(parts=[ToolCallPart("read_change_tool", {"change_id": "change-0248"})])
            if step[0] == 3:
                return ModelResponse(parts=[ToolCallPart("read_source_window_tool", {
                    "change_id": "change-0248", "side": "new"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "project_id": "project-1", "obligation_id": "OBL-01", "outcome": "affected",
                "summary": "Review the 2031 checkpoint", "rationale": "The final decision adds 2031.",
                "proposed_action": "Verify allocation, then update the schedule.",
                "reviewer_role": "Resource planning manager", "open_questions": [],
                "citations": [{"change_id": "change-0248", "side": "new", "quote": "invented deadline"}],
            })])

        assessment = review(self.data, self.obligations[0], build_agent(FunctionModel(scripted_model)))
        self.assertFalse(hasattr(assessment.citations[0], "quote"))
        self.assertIn("June 1, 2031", serialize_assessment(assessment, self.data)["citations"][0]["quote"])
        self.assertEqual(step[0], 4)

    def test_agent_inspects_cited_pdf_side_before_finalizing(self):
        step = [0]

        def scripted_model(messages, info):
            step[0] += 1
            if step[0] == 1:
                return ModelResponse(parts=[ToolCallPart("search_changes_tool", {"query": "2032 4000 NQC"})])
            if step[0] == 2:
                return ModelResponse(parts=[ToolCallPart("read_change_tool", {"change_id": "change-0226"})])
            if step[0] == 3:
                return ModelResponse(parts=[ToolCallPart("read_source_window_tool", {
                    "change_id": "change-0226", "side": "new"})])
            if step[0] == 5:
                return ModelResponse(parts=[ToolCallPart("read_source_window_tool", {
                    "change_id": "change-0226", "side": "old"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "project_id": "project-1", "obligation_id": "OBL-01", "outcome": "affected",
                "summary": "Review the schedule", "rationale": "The procurement timeline changed.",
                "proposed_action": "Verify allocation and update the schedule.",
                "reviewer_role": "Resource planning manager", "open_questions": [],
                "citations": [{"change_id": "change-0226", "side": "old"}],
            })])

        assessment = review(self.data, self.obligations[0], build_agent(FunctionModel(scripted_model)))
        self.assertEqual(assessment.citations[0].side, "old")
        self.assertEqual(step[0], 6)

    def test_one_retry_requests_all_missing_source_windows(self):
        step = [0]

        def scripted_model(messages, info):
            step[0] += 1
            if step[0] == 1:
                return ModelResponse(parts=[ToolCallPart("search_changes_tool", {"query": "2031 procurement NQC"})])
            if step[0] == 2:
                return ModelResponse(parts=[
                    ToolCallPart("read_change_tool", {"change_id": "change-0248"}),
                    ToolCallPart("read_change_tool", {"change_id": "change-0214"}),
                ])
            output = {
                "project_id": "project-1", "obligation_id": "OBL-01", "outcome": "affected",
                "summary": "Review the schedule", "rationale": "The source changed.",
                "proposed_action": "Verify and update the schedule.",
                "reviewer_role": "Resource planning manager", "open_questions": [],
                "citations": [
                    {"change_id": "change-0248", "side": "new"},
                    {"change_id": "change-0214", "side": "new"},
                ],
            }
            if step[0] == 3:
                return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])
            if step[0] == 4:
                retries = [part.content for message in messages for part in message.parts
                           if isinstance(part, RetryPromptPart)]
                self.assertEqual(len(retries), 1)
                self.assertIn("change-0248", retries[0])
                self.assertIn("change-0214", retries[0])
                return ModelResponse(parts=[
                    ToolCallPart("read_source_window_tool", {"change_id": "change-0248", "side": "new"}),
                    ToolCallPart("read_source_window_tool", {"change_id": "change-0214", "side": "new"}),
                ])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

        result = review(self.data, self.obligations[0], build_agent(FunctionModel(scripted_model)))
        self.assertEqual(len(result.citations), 2)
        self.assertEqual(step[0], 5)

    def test_unlinked_project_cannot_review_docket(self):
        with self.assertRaisesRegex(ValueError, "not linked"):
            ReviewContext.load(ROOT / "data/seed/projects/project-2", ROOT / "data/seed/dockets/R.25-06-019")

    def test_private_dotenv_settings_and_environment_precedence(self):
        with TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text("OPENROUTER_API_KEY='test-file-value'\nOPENROUTER_MODEL=test/file-model\nUNRELATED=ignored\n")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(read_provider_settings(dotenv_path), ("test-file-value", "test/file-model"))
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-process-value",
                                              "OPENROUTER_MODEL": "test/process-model"}, clear=True):
                self.assertEqual(read_provider_settings(dotenv_path), ("test-process-value", "test/process-model"))


if __name__ == "__main__":
    unittest.main()
