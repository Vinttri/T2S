"""Graph-related routes for the text2sql API."""

import json
import logging
from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from api.core.schema_loader import list_databases
from api.core.text2sql import (
    ChatRequest,
    ConfirmRequest,
    _Final,
    delete_database,
    get_schema,
    refresh_database_schema,
    run_confirmed,
    run_query,
)
from api.core.pipeline import (
    GENERAL_PREFIX,
    MESSAGE_DELIMITER,
    graph_name,
    is_general_graph,
    validate_and_truncate_chat,
    validate_custom_model,
)
from api.core.errors import GraphNotFoundError, InternalError, InvalidArgumentError
from api.graph import (
    delete_document_source,
    get_document_sources,
    get_knowledge,
    get_user_rules,
    set_knowledge,
    set_user_rules,
)
from api.auth.user_management import token_required
from api.routes.tokens import UNAUTHORIZED_RESPONSE

graphs_router = APIRouter(tags=["Graphs & Databases"])

SQL_GENERATION_RESPONSES = {
    400: {"description": "Invalid SQL generation request"},
    401: UNAUTHORIZED_RESPONSE,
    404: {"description": "Database not found"},
    408: {"description": "SQL generation timed out"},
    422: {"description": "Unexpected SQL generation failure"},
    424: {"description": "SQL generation dependency failed"},
}


async def _serialize_pipeline(gen):
    """Serialize pipeline events to the wire format and stop on ``_Final``.

    Pure encoding loop — no exception handling here. Each route handler
    wraps iteration in its own ``try/except`` so the broad-except (which
    emits a generic error event without leaking stack data) lives in the
    route function CodeQL already accepts, not in a shared helper.
    """
    async for event in gen:
        if isinstance(event, _Final):
            return
        yield json.dumps(event) + MESSAGE_DELIMITER


class GraphData(BaseModel):
    """Graph data model.

    Args:
        BaseModel (_type_): _description_
    """

    database: str


class SqlGenerationRequest(BaseModel):
    """Request model for non-streaming SQL generation."""

    graph_id: str | None = None
    question: str | None = None
    chat: list[str] | None = None
    result: list[str] | None = None
    instructions: str | None = None
    custom_api_key: str | None = None
    custom_model: str | None = None
    use_user_rules: bool = True
    use_knowledge: bool = True
    use_memory: bool = False
    session_context: dict | None = None

    def to_chat_request(self) -> ChatRequest:
        """Convert to the existing text2sql pipeline request shape."""
        chat = self.chat or ([self.question] if self.question else [])
        return ChatRequest(
            chat=chat,
            result=self.result,
            instructions=self.instructions,
            custom_api_key=self.custom_api_key,
            custom_model=self.custom_model,
            use_user_rules=self.use_user_rules,
            use_knowledge=self.use_knowledge,
            use_memory=self.use_memory,
            session_context=self.session_context,
        )


class SimpleSqlGenerationRequest(BaseModel):
    """Minimal request model for SQL generation."""

    model_config = ConfigDict(extra="forbid")

    database: str
    query: str

    def to_sql_generation_request(self) -> SqlGenerationRequest:
        """Convert the minimal public request to the internal request shape."""
        return SqlGenerationRequest(
            question=self.query,
            use_user_rules=True,
            use_knowledge=True,
            use_memory=False,
        )


async def _available_databases_for_request(request: Request) -> list[str]:
    """Return user-visible database names for SQL generation errors."""
    try:
        return await list_databases(request.state.user_id, GENERAL_PREFIX)
    except Exception:  # pylint: disable=broad-exception-caught
        logging.exception("Failed to list databases for SQL generation error response")
        return []


def _sql_generation_error_response(
    *,
    graph_id: str,
    message: str,
    status_code: int,
    available_databases: list[str],
    missing_information: str = "",
    ambiguities: str = "",
    explanation: str = "",
) -> JSONResponse:
    """Return a structured non-2xx SQL generation error response."""
    available_text = ", ".join(available_databases) if available_databases else "none"
    error_message = f"{message}. Available databases: {available_text}"
    return JSONResponse(
        status_code=status_code,
        content={
            "graph_id": graph_id,
            "sql": "",
            "confidence": 0,
            "is_valid": False,
            "missing_information": missing_information,
            "ambiguities": ambiguities,
            "explanation": explanation,
            "ai_response": message,
            "error_message": error_message,
            "available_databases": available_databases,
            "executed": False,
        },
    )


def _append_available_databases(message: str, available_databases: list[str]) -> str:
    """Append available database names to an error message."""
    available_text = ", ".join(available_databases) if available_databases else "none"
    return f"{message}. Available databases: {available_text}"


def _exception_chain_text(exc: Exception) -> str:
    """Return a compact message built from an exception and its causes."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        if text:
            parts.append(text)
        current = current.__cause__ or current.__context__
    return " | ".join(dict.fromkeys(parts)) or exc.__class__.__name__


def _classify_sql_generation_exception(exc: Exception) -> tuple[int, str]:
    """Map text2sql failures to API-facing HTTP status and message."""
    detail = _exception_chain_text(exc)
    lowered = detail.lower()
    dependency_markers = (
        "apiconnectionerror",
        "connection error",
        "connecterror",
        "name or service not known",
        "certificate_verify_failed",
        "unable to get local issuer certificate",
        "llm completion failed",
        "openai",
        "litellm",
    )
    timeout_markers = (
        "timeout",
        "timed out",
    )
    if any(marker in lowered for marker in timeout_markers):
        return 408, f"SQL generation timed out: {detail}"
    if any(marker in lowered for marker in dependency_markers):
        return 424, f"SQL generation dependency failed: {detail}"
    return 422, f"SQL generation failed: {detail}"


async def _generate_sql_response(
    request: Request,
    graph_id: str,
    sql_request: SqlGenerationRequest,
) -> JSONResponse:
    """Generate SQL for a graph and return a plain JSON response."""
    available_databases = await _available_databases_for_request(request)
    try:
        graph_name(request.state.user_id, graph_id)
        chat_request = sql_request.to_chat_request()
        validate_and_truncate_chat(chat_request)
        validate_custom_model(getattr(chat_request, "custom_model", None))
    except InvalidArgumentError as iae:
        logging.warning("Invalid argument in SQL generation: %s", str(iae))
        return _sql_generation_error_response(
            graph_id=graph_id,
            message=str(iae) or "Invalid SQL generation request",
            status_code=400,
            available_databases=available_databases,
        )

    if graph_id not in available_databases:
        return _sql_generation_error_response(
            graph_id=graph_id,
            message=f"Database '{graph_id}' not found",
            status_code=404,
            available_databases=available_databases,
        )

    last_terminal_event: dict | None = None
    generator = run_query(request.state.user_id, graph_id, chat_request)
    try:
        async for event in generator:
            if isinstance(event, _Final):
                result = event.value
                content = {
                    "graph_id": graph_id,
                    "sql": result.sql_query or "",
                    "sql_commented": result.sql_commented or "",
                    "column_evidence": result.column_evidence or [],
                    "evidence_issues": result.evidence_issues or [],
                    "schema_json": result.schema_json or {},
                    "confidence": result.confidence,
                    "is_valid": result.is_valid,
                    "missing_information": result.missing_information,
                    "ambiguities": result.ambiguities,
                    "explanation": result.explanation,
                    "ai_response": result.ai_response,
                    "error_message": result.error_message,
                    "available_databases": available_databases,
                    "executed": False,
                }
                if result.error_message or not result.is_valid or not result.sql_query:
                    error_message = (
                        result.error_message
                        or result.missing_information
                        or result.ambiguities
                        or result.ai_response
                        or "SQL was not generated"
                    )
                    content["error_message"] = _append_available_databases(
                        error_message,
                        available_databases,
                    )
                    return JSONResponse(content=content)
                return JSONResponse(content=content)

            event_type = event.get("type") if isinstance(event, dict) else None
            if event_type == "sql_query":
                content = {
                    "graph_id": graph_id,
                    "sql": event.get("data", "") or "",
                    "sql_commented": event.get("sql_commented", "") or "",
                    "column_evidence": event.get("column_evidence", []) or [],
                    "evidence_issues": event.get("evidence_issues", []) or [],
                    "schema_json": event.get("schema_json", {}) or {},
                    "confidence": event.get("conf", 0),
                    "is_valid": event.get("is_valid", False),
                    "missing_information": event.get("miss", "") or "",
                    "ambiguities": event.get("amb", "") or "",
                    "explanation": event.get("exp", "") or "",
                    "ai_response": "",
                    "error_message": None,
                    "available_databases": available_databases,
                    "executed": False,
                }
                if not content["is_valid"] or not content["sql"]:
                    error_message = (
                        content["missing_information"]
                        or content["ambiguities"]
                        or "SQL was not generated"
                    )
                    content["error_message"] = _append_available_databases(
                        error_message,
                        available_databases,
                    )
                    return JSONResponse(content=content)
                return JSONResponse(content=content)

            if event_type in {"followup_questions", "error"}:
                last_terminal_event = event
    except GraphNotFoundError:
        return _sql_generation_error_response(
            graph_id=graph_id,
            message=f"Database '{graph_id}' not found",
            status_code=404,
            available_databases=available_databases,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.exception("SQL generation failed")
        status_code, message = _classify_sql_generation_exception(exc)
        return _sql_generation_error_response(
            graph_id=graph_id,
            message=message,
            status_code=status_code,
            available_databases=available_databases,
        )
    finally:
        await generator.aclose()

    message = (last_terminal_event or {}).get("message", "")
    message = message or "SQL was not generated"
    return JSONResponse(
        content={
            "graph_id": graph_id,
            "sql": "",
            "confidence": 0,
            "is_valid": False,
            "missing_information": message,
            "ambiguities": "",
            "explanation": "",
            "ai_response": message,
            "error_message": _append_available_databases(message, available_databases),
            "available_databases": available_databases,
            "executed": False,
        },
    )


@graphs_router.get(
    "",
    operation_id="list_databases",
    tags=["mcp_tool"],
    responses={401: UNAUTHORIZED_RESPONSE}
)
@token_required
async def list_graphs(request: Request):
    """
    List all available graphs/databases for the authenticated user.
    Requires authentication.
    """
    graphs = await list_databases(request.state.user_id, GENERAL_PREFIX)
    return JSONResponse(content=graphs)


@graphs_router.post(
    "/{graph_id}/sql",
    operation_id="generate_sql",
    tags=["mcp_tool"],
    responses=SQL_GENERATION_RESPONSES,
)
@token_required
async def generate_sql(
    request: Request,
    graph_id: str,
    sql_request: SqlGenerationRequest,
):
    """Generate SQL for a natural-language question without executing it."""
    return await _generate_sql_response(request, graph_id, sql_request)


@graphs_router.post(
    "/sql",
    operation_id="generate_sql_from_body",
    tags=["mcp_tool"],
    responses=SQL_GENERATION_RESPONSES,
)
@token_required
async def generate_sql_from_body(
    request: Request,
    sql_request: SimpleSqlGenerationRequest,
):
    """Generate SQL from a minimal request body: database + query."""
    graph_id = sql_request.database.strip()
    if not graph_id:
        return JSONResponse(content={"error": "database is required"}, status_code=400)
    return await _generate_sql_response(
        request,
        graph_id,
        sql_request.to_sql_generation_request(),
    )


@graphs_router.get(
    "/{graph_id}/data",
    operation_id="database_schema",
    tags=["mcp_tool"],
    responses={401: UNAUTHORIZED_RESPONSE}
)
@token_required
async def get_graph_data(
    request: Request, graph_id: str
):  # pylint: disable=too-many-locals,too-many-branches
    """Return all nodes and edges for the specified database schema.
    Requires authentication.

        args:
            graph_id (str): The ID of the graph to query (the database name).
    """

    try:
        schema = await get_schema(request.state.user_id, graph_id)
        return JSONResponse(content=schema)
    except GraphNotFoundError as gnfe:
        logging.warning("Graph not found: %s", str(gnfe))
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except InternalError as ie:
        logging.error("Internal error getting schema: %s", str(ie))
        return JSONResponse(
            content={"error": "Failed to retrieve database schema"},
            status_code=500
        )


@graphs_router.post("", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def load_graph(
    request: Request, data: GraphData = None, file: UploadFile = File(None)
):  # pylint: disable=unused-argument
    """
    This route is used to load the graph data into the database.
    It expects either:
    - A JSON payload (application/json)
    - A File upload (multipart/form-data)
    - An XML payload (application/xml or text/xml)
    """

    # ✅ Handle JSON Payload
    if data:  # pylint: disable=no-else-raise
        raise HTTPException(status_code=501, detail="JSONLoader is not implemented yet")
    # ✅ Handle File Upload
    elif file:
        filename = file.filename

        # ✅ Check if file is JSON
        if filename.endswith(".json"):  # pylint: disable=no-else-raise
            raise HTTPException(
                status_code=501, detail="JSONLoader is not implemented yet"
            )

        # ✅ Check if file is XML
        elif filename.endswith(".xml"):
            raise HTTPException(
                status_code=501, detail="ODataLoader is not implemented yet"
            )

        # ✅ Check if file is csv
        elif filename.endswith(".csv"):
            raise HTTPException(
                status_code=501, detail="CSVLoader is not implemented yet"
            )
        else:
            raise HTTPException(status_code=415, detail="Unsupported file type")
    else:
        raise HTTPException(status_code=415, detail="Unsupported Content-Type")


@graphs_router.post(
    "/{graph_id}",
    operation_id="query_database",
    tags=["mcp_tool"],
    responses={401: UNAUTHORIZED_RESPONSE}
)
@token_required
async def query_graph(
    request: Request, graph_id: str, chat_data: ChatRequest
):  # pylint: disable=too-many-statements
    """
    Query the Database with the given graph_id and chat_data.
    Requires authentication.

        Args:
            graph_id (str): The ID of the graph to query.
            chat_data (ChatRequest): The chat data containing user queries and context.
    """
    # Eager validation: ``run_query`` is an async generator, so its body
    # (including ``validate_and_truncate_chat``/``graph_name``) only runs once
    # the StreamingResponse is iterated. Surfacing client errors as HTTP 400
    # requires a synchronous check before we hand the stream to the response.
    try:
        graph_name(request.state.user_id, graph_id)
        validate_and_truncate_chat(chat_data)
        validate_custom_model(getattr(chat_data, "custom_model", None))
    except InvalidArgumentError as iae:
        logging.warning("Invalid argument in query: %s", str(iae))
        return JSONResponse(content={"error": "Invalid query request"}, status_code=400)

    async def stream():
        try:
            async for chunk in _serialize_pipeline(
                run_query(request.state.user_id, graph_id, chat_data)
            ):
                yield chunk
        except Exception:  # pylint: disable=broad-exception-caught
            # Don't leak stack traces (CodeQL: information exposure through
            # exception). Log internally; emit a generic error event.
            logging.exception("Streaming query failed")
            yield json.dumps({
                "type": "error",
                "final_response": True,
                "message": "Internal error while processing query",
            }) + MESSAGE_DELIMITER

    return StreamingResponse(stream(), media_type="application/json")


@graphs_router.post("/{graph_id}/confirm", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def confirm_destructive_operation(
    request: Request,
    graph_id: str,
    confirm_data: ConfirmRequest,
):
    """
    Handle user confirmation for destructive SQL operations.
    Requires authentication.
    """

    # Eager validation — see note on the query endpoint above.
    try:
        namespaced = graph_name(request.state.user_id, graph_id)
        if is_general_graph(namespaced):
            raise InvalidArgumentError(
                "Destructive operations are not allowed on demo graphs"
            )
        if not (getattr(confirm_data, "sql_query", "") or "").strip():
            raise InvalidArgumentError("No SQL query provided")
        validate_custom_model(getattr(confirm_data, "custom_model", None))
    except InvalidArgumentError as iae:
        logging.warning("Invalid argument in destructive operation: %s", str(iae))
        return JSONResponse(content={"error": "Invalid confirmation request"}, status_code=400)

    async def stream():
        try:
            async for chunk in _serialize_pipeline(
                run_confirmed(request.state.user_id, graph_id, confirm_data)
            ):
                yield chunk
        except Exception:  # pylint: disable=broad-exception-caught
            # See note on the query endpoint above (CodeQL).
            logging.exception("Streaming confirmed-destructive query failed")
            yield json.dumps({
                "type": "error",
                "final_response": True,
                "message": "Internal error while processing confirmation",
            }) + MESSAGE_DELIMITER

    return StreamingResponse(stream(), media_type="application/json")


@graphs_router.post("/{graph_id}/refresh", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def refresh_graph_schema(request: Request, graph_id: str):
    """
    Manually refresh the graph schema from the database.
    This endpoint allows users to manually trigger a schema refresh
    if they suspect the graph is out of sync with the database.
    Streams progress steps as a sequence of JSON messages.
    """
    try:
        generator = await refresh_database_schema(request.state.user_id, graph_id)
        return StreamingResponse(generator, media_type="application/json")
    except (InternalError, InvalidArgumentError) as e:
        # Log detailed error internally, send generic message to user
        if isinstance(e, InternalError):
            logging.error("Internal error refreshing schema: %s", str(e))
            error_message = "Failed to refresh database schema"
            status_code = 500
        else:
            logging.warning("Invalid argument refreshing schema: %s", str(e))
            error_message = "Invalid request to refresh schema"
            status_code = 400
        return JSONResponse(content={"error": error_message}, status_code=status_code)


@graphs_router.delete("/{graph_id}", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def delete_graph(request: Request, graph_id: str):
    """Delete the specified graph (namespaced to the user).

    This will attempt to delete the FalkorDB graph belonging to the
    authenticated user. The graph id used by the client is stripped of
    namespace and will be namespaced using the user's id from the request
    state.
    """

    try:
        result = await delete_database(request.state.user_id, graph_id)
        return JSONResponse(content=result)

    except InvalidArgumentError as iae:
        logging.warning("Invalid argument in delete: %s", str(iae))
        return JSONResponse(content={"error": "Invalid delete request"}, status_code=400)
    except GraphNotFoundError as gnfe:
        logging.warning("Graph not found for deletion: %s", str(gnfe))
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except InternalError as ie:
        logging.error("Internal error deleting database: %s", str(ie))
        return JSONResponse(
            content={"error": "Failed to delete database"},
            status_code=500
        )


class UserRulesRequest(BaseModel):
    """User rules request model."""
    user_rules: str


class KnowledgeRequest(BaseModel):
    """Knowledge request model."""
    knowledge: str


@graphs_router.get("/{graph_id}/knowledge", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_graph_knowledge(request: Request, graph_id: str):
    """Get database-specific knowledge for the specified graph."""
    try:
        full_graph_id = graph_name(request.state.user_id, graph_id)
        knowledge = await get_knowledge(full_graph_id)
        logging.info("Retrieved knowledge length: %d", len(knowledge) if knowledge else 0)
        return JSONResponse(content={"knowledge": knowledge})
    except GraphNotFoundError:
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error getting knowledge: %s", str(e))
        return JSONResponse(content={"error": "Failed to get knowledge"}, status_code=500)


@graphs_router.put("/{graph_id}/knowledge", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def update_graph_knowledge(request: Request, graph_id: str, data: KnowledgeRequest):
    """Update database-specific knowledge for the specified graph."""
    try:
        if GENERAL_PREFIX and graph_id.startswith(GENERAL_PREFIX):
            return JSONResponse(
                content={"error": "Knowledge cannot be modified for demo databases"},
                status_code=403
            )

        logging.info(
            "Received request to update knowledge, content length: %d",
            len(data.knowledge)
        )
        full_graph_id = graph_name(request.state.user_id, graph_id)
        # Knowledge is additive (R1): a non-empty payload is appended to the
        # existing knowledge for this DB; an empty payload clears it. set_knowledge
        # also chunks + embeds the merged knowledge into retrievable nodes.
        await set_knowledge(full_graph_id, data.knowledge)
        merged_knowledge = await get_knowledge(full_graph_id)
        logging.info("Knowledge updated successfully")
        return JSONResponse(content={"success": True, "knowledge": merged_knowledge})
    except GraphNotFoundError:
        logging.error("Graph not found")
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error updating knowledge: %s", str(e))
        return JSONResponse(content={"error": "Failed to update knowledge"}, status_code=500)


@graphs_router.get("/{graph_id}/user-rules", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_graph_user_rules(request: Request, graph_id: str):
    """Get user rules for the specified graph."""
    try:
        full_graph_id = graph_name(request.state.user_id, graph_id)
        user_rules = await get_user_rules(full_graph_id)
        logging.info("Retrieved user rules length: %d", len(user_rules) if user_rules else 0)
        return JSONResponse(content={"user_rules": user_rules})
    except GraphNotFoundError:
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error getting user rules: %s", str(e))
        return JSONResponse(content={"error": "Failed to get user rules"}, status_code=500)


@graphs_router.put("/{graph_id}/user-rules", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def update_graph_user_rules(request: Request, graph_id: str, data: UserRulesRequest):
    """Update user rules for the specified graph."""
    try:
        # Prevent modifying rules for demo databases
        if GENERAL_PREFIX and graph_id.startswith(GENERAL_PREFIX):
            return JSONResponse(
                content={"error": "Rules cannot be modified for demo databases"},
                status_code=403
            )

        logging.info(
            "Received request to update user rules, content length: %d", len(data.user_rules)
        )
        full_graph_id = graph_name(request.state.user_id, graph_id)
        await set_user_rules(full_graph_id, data.user_rules)
        logging.info("User rules updated successfully")
        return JSONResponse(content={"success": True, "user_rules": data.user_rules})
    except GraphNotFoundError:
        logging.error("Graph not found")
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error updating user rules: %s", str(e))
        return JSONResponse(content={"error": "Failed to update user rules"}, status_code=500)


@graphs_router.get("/{graph_id}/loaded-files", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_graph_loaded_files(request: Request, graph_id: str):
    """List what is loaded into this graph: business knowledge (one merged blob)
    and uploaded schema documents (per source/filename)."""
    try:
        full_graph_id = graph_name(request.state.user_id, graph_id)
        knowledge = await get_knowledge(full_graph_id)
        docs = await get_document_sources(full_graph_id)
        documents = [
            {"source": src, "chars": len(text or "")}
            for src, text in sorted((docs or {}).items())
        ]
        return JSONResponse(content={
            "knowledge": {
                "present": bool((knowledge or "").strip()),
                "chars": len(knowledge or ""),
            },
            "documents": documents,
        })
    except GraphNotFoundError:
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error listing loaded files: %s", str(e))
        return JSONResponse(content={"error": "Failed to list loaded files"}, status_code=500)


@graphs_router.get("/{graph_id}/document", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_graph_document(request: Request, graph_id: str, source: str):
    """Return the stored content of one uploaded schema document (for download)."""
    try:
        full_graph_id = graph_name(request.state.user_id, graph_id)
        docs = await get_document_sources(full_graph_id)
        content = (docs or {}).get(source)
        if content is None:
            return JSONResponse(content={"error": "Document not found"}, status_code=404)
        return JSONResponse(content={"source": source, "content": content})
    except GraphNotFoundError:
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error reading document: %s", str(e))
        return JSONResponse(content={"error": "Failed to read document"}, status_code=500)


@graphs_router.delete("/{graph_id}/document", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def delete_graph_document(request: Request, graph_id: str, source: str):
    """Delete one uploaded schema document (all its :Document chunks) from the graph."""
    try:
        if GENERAL_PREFIX and graph_id.startswith(GENERAL_PREFIX):
            return JSONResponse(
                content={"error": "Demo databases are read-only"}, status_code=403)
        full_graph_id = graph_name(request.state.user_id, graph_id)
        removed = await delete_document_source(full_graph_id, source)
        return JSONResponse(content={"success": True, "removed": removed, "source": source})
    except GraphNotFoundError:
        return JSONResponse(content={"error": "Database not found"}, status_code=404)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error deleting document: %s", str(e))
        return JSONResponse(content={"error": "Failed to delete document"}, status_code=500)
