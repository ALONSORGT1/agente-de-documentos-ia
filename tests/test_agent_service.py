import json
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, RateLimitError

import app.services.agent_service as agent
from app.core.config import Settings
from app.core.errors import ProviderError

DRIVE_ID = "drive_authorized_123"
FILE_ID = "file-active"


@pytest.fixture
def setup(monkeypatch):
    config = Settings(openai_api_key="fake", drive_folder_id="folder_authorized_123")
    monkeypatch.setattr(agent, "settings", config)
    manifest = {"vector_store_id": "vs-test", "documents": {
        DRIVE_ID: {"name": "Práctica RDS", "mime_type": "application/pdf", "openai_file_id": FILE_ID, "url": "https://attacker.example"}}}
    monkeypatch.setattr(agent.SyncService, "load_manifest", lambda: manifest)
    client = Mock()
    return agent.AgentService(client), client, manifest


def response(citation_id=FILE_ID, result_id=FILE_ID, text="El documento describe RDS.", status="completed"):
    output = [{"type": "file_search_call", "status": "completed", "results": [
        {"file_id": result_id, "text": "RDS utiliza una base de datos administrada."}]},
        {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": [
            {"type": "file_citation", "file_id": citation_id, "index": len(text)}]}]}]
    return {"output_text": text, "status": status, "output": output,
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}


def test_grounding_and_current_file_filter(setup):
    service, client, manifest = setup
    client.responses.create.return_value = response()
    result = service.ask("¿Qué es RDS?")
    assert result["grounded"] and result["sources"][0]["name"] == "Práctica RDS"
    assert result["sources"][0]["snippet"].startswith("RDS")
    assert result["sources"][0]["url"] == f"https://drive.google.com/file/d/{DRIVE_ID}/view"
    args = client.responses.create.call_args.kwargs
    assert args["tool_choice"] == "required" and args["store"] is False
    assert args["include"] == ["file_search_call.results"]
    assert args["tools"][0]["filters"]["value"] == [FILE_ID]


@pytest.mark.parametrize("kind", ["unknown_citation", "unknown_result", "no_citations", "no_results", "empty_text"])
def test_no_evidence_never_returns_model_claims(setup, kind):
    service, client, _ = setup
    data = response()
    if kind == "unknown_citation":
        data = response(citation_id="file-obsolete")
    elif kind == "unknown_result":
        data = response(result_id="file-obsolete")
    elif kind == "no_citations":
        data["output"][1]["content"][0]["annotations"] = []
    elif kind == "no_results":
        data["output"][0]["results"] = []
    elif kind == "empty_text":
        data["output_text"] = ""
    client.responses.create.return_value = data
    result = service.ask("¿Qué es RDS?")
    assert not result["grounded"] and result["sources"] == []
    assert "El documento describe" not in result["answer"]


def test_empty_index_avoids_api_cost(setup):
    service, client, manifest = setup
    manifest["documents"].clear()
    result = service.ask("Pregunta")
    assert not result["grounded"]
    client.responses.create.assert_not_called()


def test_null_fragment_is_insufficient_evidence(setup):
    service, client, _ = setup
    data = response()
    data["output"][0]["results"][0]["text"] = None
    client.responses.create.return_value = data
    result = service.ask("Pregunta")
    assert not result["grounded"] and result["sources"] == []


def test_missing_citations_reports_specific_reason_without_logging_content(setup, caplog):
    service, client, _ = setup
    data = response(text="private-answer-content")
    data["output"][1]["content"][0]["annotations"] = []
    client.responses.create.return_value = data
    result = service.ask("private-question-content")
    assert not result["grounded"]
    assert "modelo no devolvió citas" in result["warning"]
    assert "'search_calls': 1" in caplog.text and "'valid_chunks': 1" in caplog.text
    for private in ("private-answer-content", "private-question-content", FILE_ID, DRIVE_ID):
        assert private not in caplog.text


def test_empty_search_reports_index_reason(setup):
    service, client, _ = setup
    data = response()
    data["output"][0]["results"] = []
    data["output"][1]["content"][0]["annotations"] = []
    client.responses.create.return_value = data
    assert "no devolvió fragmentos" in service.ask("Pregunta")["warning"]


def comparison_response(manifest, cite_program=False):
    manifest["documents"]["drive-program-123"] = {
        "name": "Programa Auditoría.pdf", "mime_type": "application/pdf", "openai_file_id": "file-program"}
    data = response(text="Práctica RDS y Programa Auditoría.pdf tienen objetivos similares.")
    data["output"][0]["results"].append({"file_id": "file-program", "text": "Objetivos del programa."})
    if cite_program:
        data["output"][1]["content"][0]["annotations"].append({
            "type": "file_citation", "file_id": "file-program", "index": 1})
    return data


def test_comparison_requires_citations_for_each_named_document(setup):
    service, client, manifest = setup
    client.responses.create.return_value = comparison_response(manifest)
    result = service.ask("Compara Práctica RDS y Programa Auditoría.pdf")
    assert not result["grounded"] and result["sources"] == []
    assert "Faltan citas verificables" in result["warning"]
    assert "Programa Auditoría.pdf" in result["warning"]
    assert "objetivos similares" not in result["answer"]


def test_generic_question_rejects_uncited_document_named_in_answer(setup):
    service, client, manifest = setup
    client.responses.create.return_value = comparison_response(manifest)
    assert not service.ask("¿Qué objetivos tienen los documentos?")["grounded"]


def test_comparison_accepts_both_native_citations(setup):
    service, client, manifest = setup
    client.responses.create.return_value = comparison_response(manifest, cite_program=True)
    result = service.ask("Compara Práctica RDS y Programa Auditoría.pdf")
    assert result["grounded"] and len(result["sources"]) == 2


def test_question_titles_use_unicode_case_and_whitespace_normalization(setup):
    service, client, manifest = setup
    data = comparison_response(manifest)
    data["output_text"] = "El primer documento tiene objetivos."
    client.responses.create.return_value = data
    result = service.ask("Compara PRÁCTICA RDS con PROGRAMA  AUDITORI\u0301A.pdf")
    assert not result["grounded"] and "Programa Auditoría.pdf" in result["warning"]


def test_unrelated_retrieved_document_does_not_require_a_citation(setup):
    service, client, manifest = setup
    data = comparison_response(manifest)
    data["output_text"] = "El documento describe RDS."
    client.responses.create.return_value = data
    assert service.ask("¿Qué es RDS?")["grounded"]


def extraction_response(claims, answerable=True, status="completed"):
    return {"status": status, "output_text": json.dumps({"answerable": answerable, "claims": claims}),
            "usage": {"input_tokens": 70, "output_tokens": 40, "total_tokens": 110}}


def two_excerpt_claims():
    return [
        {"text": "Práctica RDS describe una base de datos administrada.", "evidence": [
            {"ref": "R1"}]},
        {"text": "Programa Auditoría.pdf describe los objetivos académicos.", "evidence": [
            {"ref": "R2"}]},
    ]


def prepare_extraction(manifest):
    data = comparison_response(manifest)
    data["output"][0]["results"][1]["text"] = "El programa establece objetivos académicos de auditoría."
    data["output"][1]["content"][0]["annotations"] = []
    data["output_text"] = "REJECTED PRIVATE PRIMARY CLAIMS SHOULD NEVER PROPAGATE."
    return data


def test_verified_extraction_uses_current_chunks_and_builds_own_references(setup):
    service, client, manifest = setup
    primary = prepare_extraction(manifest)
    client.responses.create.side_effect = [primary, extraction_response(two_excerpt_claims())]
    result = service.ask("Compara Práctica RDS y Programa Auditoría.pdf", history=[
        {"role": "user", "content": "Contexto de la pregunta"},
        {"role": "assistant", "content": "PRIVATE PREVIOUS ANSWER"},
    ])
    assert result["grounded"] and len(result["sources"]) == 2
    assert "[1]" in result["answer"] and "[2]" in result["answer"]
    assert "REJECTED PRIVATE" not in result["answer"]
    assert result["usage"] == {"input_tokens": 90, "output_tokens": 50, "total_tokens": 140}
    for number, source in enumerate(result["sources"], 1):
        assert source["evidence_kind"] == "verified_excerpt"
        assert source["reference_number"] == number and source["citation_indices"] == []
        assert source["snippet"] == primary["output"][0]["results"][number - 1]["text"].strip()
    assert client.responses.create.call_count == 2
    args = client.responses.create.call_args.kwargs
    assert not {"tools", "tool_choice", "include", "max_tool_calls"} & args.keys()
    assert args["store"] is False
    assert args["text"]["format"]["type"] == "json_schema" and args["text"]["format"]["strict"] is True
    evidence_schema = args["text"]["format"]["schema"]["properties"]["claims"]["items"]["properties"]["evidence"]["items"]
    assert set(evidence_schema["properties"]) == {"ref"} and evidence_schema["required"] == ["ref"]
    context = json.loads(args["input"][0]["content"])
    assert context["previous_questions"] == ["Contexto de la pregunta"]
    assert [fragment["ref"] for fragment in context["fragments"]] == ["R1", "R2"]
    assert "REJECTED PRIVATE" not in str(args) and "PRIVATE PREVIOUS ANSWER" not in str(args)


@pytest.mark.parametrize("kind", ["extra_quote", "extra_field", "unknown_ref", "zero_evidence",
                                  "empty_claims", "not_answerable", "manual_reference", "missing_document",
                                  "incomplete", "invalid_json", "too_many_claims", "too_many_evidence"])
def test_unverifiable_extraction_is_never_returned(setup, kind, caplog):
    service, client, manifest = setup
    primary = prepare_extraction(manifest)
    claims = two_excerpt_claims()
    if kind == "extra_quote":
        claims[0]["evidence"][0]["quote"] = "Invented evidence that appears in no authorized fragment."
    elif kind == "extra_field":
        claims[0]["evidence"][0]["file_id"] = FILE_ID
    elif kind == "unknown_ref":
        claims[0]["evidence"][0]["ref"] = "R999"
    elif kind == "zero_evidence":
        claims[0]["evidence"] = []
    elif kind == "empty_claims":
        claims = []
    elif kind == "manual_reference":
        claims[0]["text"] += " [99]"
    elif kind == "missing_document":
        claims = claims[:1]
    elif kind == "too_many_claims":
        claims = claims * 7
    elif kind == "too_many_evidence":
        claims[0]["evidence"] *= 13
    fallback = extraction_response(claims, answerable=kind != "not_answerable",
                                   status="incomplete" if kind == "incomplete" else "completed")
    if kind == "invalid_json":
        fallback["output_text"] = "not valid JSON"
    client.responses.create.side_effect = [primary, fallback]
    result = service.ask("Compara Práctica RDS y Programa Auditoría.pdf")
    assert not result["grounded"] and result["sources"] == []
    assert result["answer"] == agent.INSUFFICIENT
    assert client.responses.create.call_count == 2
    for private in ("REJECTED PRIVATE", "Invented evidence", "PRIVATE PREVIOUS ANSWER", FILE_ID, DRIVE_ID):
        assert private not in caplog.text


def test_missing_required_document_chunk_avoids_second_api_call(setup):
    service, client, manifest = setup
    primary = prepare_extraction(manifest)
    primary["output"][0]["results"] = primary["output"][0]["results"][:1]
    client.responses.create.return_value = primary
    result = service.ask("Compara Práctica RDS y Programa Auditoría.pdf")
    assert not result["grounded"]
    assert client.responses.create.call_count == 1


def test_claim_cannot_name_one_document_and_cite_another_even_with_global_coverage(setup):
    service, client, manifest = setup
    primary = prepare_extraction(manifest)
    claims = two_excerpt_claims()
    claims[0]["text"] = "Programa Auditoría.pdf establece una base de datos administrada."
    client.responses.create.side_effect = [primary, extraction_response(claims)]
    result = service.ask("Compara Práctica RDS y Programa Auditoría.pdf")
    assert not result["grounded"] and result["sources"] == []
    assert client.responses.create.call_count == 2


def test_extraction_provider_error_reuses_safe_mapping(setup):
    service, client, manifest = setup
    primary = prepare_extraction(manifest)
    client.responses.create.side_effect = [primary, APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com"))]
    with pytest.raises(ProviderError, match="tiempo límite"):
        service.ask("Compara Práctica RDS y Programa Auditoría.pdf")
    assert client.responses.create.call_count == 2


@pytest.mark.parametrize("kind", ["unknown_result", "unknown_citation", "incomplete_search", "no_results"])
def test_invalid_or_empty_search_never_attempts_extraction(setup, kind):
    service, client, _ = setup
    primary = response()
    if kind == "unknown_result":
        primary["output"][0]["results"][0]["file_id"] = "file-outside-scope"
    elif kind == "unknown_citation":
        primary["output"][1]["content"][0]["annotations"][0]["file_id"] = "file-outside-scope"
    elif kind == "incomplete_search":
        primary["output"][0]["status"] = "failed"
    else:
        primary["output"][0]["results"] = []
        primary["output"][1]["content"][0]["annotations"] = []
    client.responses.create.return_value = primary
    assert not service.ask("Pregunta")["grounded"]
    assert client.responses.create.call_count == 1


def test_native_citations_keep_single_call(setup):
    service, client, _ = setup
    client.responses.create.return_value = response()
    assert service.ask("Pregunta")["grounded"]
    assert client.responses.create.call_count == 1


def test_selected_table_passage_is_immutable_and_needs_no_model_copy(setup):
    service, client, _ = setup
    primary = response()
    primary["output"][1]["content"][0]["annotations"] = []
    exact = "  Columna A\tColumna B\n" + "\n".join(
        f"Fila {index:03d}\t  Base de datos administrada — actividad académica {index}."
        for index in range(45))
    primary["output"][0]["results"][0]["text"] = exact
    expected = agent.literal_passages(exact)[1]
    claims = [{"text": "El documento explica RDS.", "evidence": [
        {"ref": "R2"}]}]
    client.responses.create.side_effect = [primary, extraction_response(claims)]
    result = service.ask("Pregunta")
    assert result["grounded"] and result["sources"][0]["snippet"] == expected
    context = json.loads(client.responses.create.call_args.kwargs["input"][0]["content"])
    assert context["fragments"][1]["text"] == expected and expected in exact
    assert "\t  " in expected  # Internal table spacing is never normalized or copied by a model.


def test_literal_passages_are_bounded_disjoint_original_substrings():
    original = "\n\n".join(f"Párrafo {index}: " + "palabra  " * 95 for index in range(8))
    passages = agent.literal_passages(original)
    assert len(passages) > 1
    offset = 0
    for passage in passages:
        assert 24 <= len(passage) <= 1200 and len(" ".join(passage.split())) >= 24
        start = original.find(passage, offset)
        assert start >= offset
        offset = start + len(passage)
    assert agent.literal_passages("Información breve.") == []


def test_passage_selection_caps_count_and_preserves_file_diversity():
    by_id = {f"file-{index}": (f"drive-{index}", {"name": f"Documento {index}.pdf"})
             for index in range(8)}
    chunks = [(file_id, "\n\n".join(f"{file_id} bloque {block}: " + "x" * 587 for block in range(10)))
              for file_id in by_id]
    selected_chunks = agent.select_excerpt_chunks(chunks, by_id, set(by_id))
    passages = agent.select_excerpt_passages(selected_chunks, set(by_id))
    assert len(passages) <= 72 and {passage["file_id"] for passage in passages} == set(by_id)
    assert sum(len(passage["text"]) for passage in passages) <= 48000
    assert all(24 <= len(passage["text"]) <= 1200 for passage in passages)
    assert [passage["ref"] for passage in passages] == [f"R{index}" for index in range(1, len(passages) + 1)]
    originals = {chunk["file_id"]: chunk["text"] for chunk in selected_chunks}
    assert all(passage["text"] in originals[passage["file_id"]] for passage in passages)


def test_chunk_selection_prioritizes_required_files_and_enforces_budget():
    by_id = {"file-first": ("drive-first", {"name": "Primero.pdf"}),
             "file-required": ("drive-required", {"name": "Requerido.pdf"})}
    chunks = [("file-first", (f"Fragment {number} " + "a" * 7000)) for number in range(15)]
    chunks += [("file-required", "Required text " + "b" * 7000), chunks[0]]
    selected = agent.select_excerpt_chunks(chunks, by_id, {"file-required"})
    assert selected[0]["file_id"] == "file-required"
    assert {chunk["file_id"] for chunk in selected} == set(by_id)
    assert len(selected) <= 12 and sum(len(chunk["text"]) for chunk in selected) <= 48000
    assert all(len(chunk["text"]) <= 6000 for chunk in selected)
    assert len({(chunk["file_id"], chunk["text"]) for chunk in selected}) == len(selected)


def test_production_responses_client_disables_sdk_retries(setup, monkeypatch):
    _, injected, _ = setup
    injected.responses.create.return_value = response()
    provider = Mock()
    provider.with_options.return_value = injected
    monkeypatch.setattr(agent, "get_openai_client", lambda: provider)
    assert agent.AgentService().ask("Pregunta")["grounded"]
    provider.with_options.assert_called_once_with(max_retries=0)


def test_experiment_has_no_search_sources_or_document_claim(setup):
    service, client, _ = setup
    client.responses.create.return_value = response()
    result = service.ask("Pregunta", False)
    assert result["mode"] == "experimental" and not result["grounded"] and result["sources"] == []
    assert "no está verificada" in result["warning"]
    args = client.responses.create.call_args.kwargs
    assert "tools" not in args and "include" not in args


def test_previous_answers_never_become_evidence(setup):
    service, client, _ = setup
    client.responses.create.return_value = response()
    service.ask("¿Y su configuración?", history=[{"role": "user", "content": "¿Qué es RDS?"},
                                               {"role": "assistant", "content": "afirmación experimental"}])
    inputs = client.responses.create.call_args.kwargs["input"]
    assert len(inputs) == 2 and "experimental" not in str(inputs)


def test_incomplete_response_rejected(setup):
    service, client, _ = setup
    client.responses.create.return_value = response(status="incomplete")
    with pytest.raises(ProviderError, match="incompleta"):
        service.ask("Pregunta")


def test_network_errors_are_safe(setup):
    service, client, _ = setup
    client.responses.create.side_effect = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    with pytest.raises(ProviderError, match="tiempo límite"):
        service.ask("Pregunta")


def test_quota_errors_are_actionable(setup):
    service, client, _ = setup
    client.responses.create.side_effect = RateLimitError("private", response=httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com")), body={})
    with pytest.raises(ProviderError, match="cuota"):
        service.ask("Pregunta")
