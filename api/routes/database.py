"""Database connection routes for the text2sql API."""
import json
from typing import List, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from api.auth.user_management import token_required
from api.core.pipeline import MESSAGE_DELIMITER
from api.core.schema_loader import load_database
from api.loaders.agent_loader import AgentSchemaLoader
from api.loaders.yaml_loader import YamlSchemaLoader
from api.routes.tokens import UNAUTHORIZED_RESPONSE

database_router = APIRouter(tags=["Database Connection"])


def _normalize_form_identifier(value: str | None) -> str:
    return str(value or "").strip().strip('"`[]').lower()

class DatabaseConnectionRequest(BaseModel):
    """Database connection request model.

    Args:
        BaseModel (_type_): _description_
    """

    url: str

@database_router.post("/database", operation_id="connect_database", tags=["mcp_tool"], responses={
    401: UNAUTHORIZED_RESPONSE
})
@token_required
async def connect_database(request: Request, db_request: DatabaseConnectionRequest):
    """
    Accepts a JSON payload with a database URL and attempts to connect.
    Supports both PostgreSQL and MySQL databases.
    Streams progress steps as a sequence of JSON messages separated by a delimiter.
    Requires authentication.
    """
    generator = await load_database(db_request.url, request.state.user_id)
    return StreamingResponse(generator, media_type="application/json")


@database_router.post(
    "/database/yaml",
    operation_id="connect_database_from_yaml",
    tags=["mcp_tool"],
    responses={401: UNAUTHORIZED_RESPONSE},
)
@token_required
async def connect_database_from_yaml(
    request: Request,
    files: List[UploadFile] = File(...),
    database: str = Form(...),
    schema: Optional[str] = Form(None),
    execute_url: Optional[str] = Form(None),
    replace: bool = Form(False),
):
    """
    Load a database graph from uploaded YAML metadata files.

    The graph stores ``execute_url`` as its database URL, so query execution can
    be handled by the matching SQL connector (for example ``impala://...``).
    """
    database_name = _normalize_form_identifier(database)
    schema_name = _normalize_form_identifier(schema or database)
    if schema_name != database_name:
        return JSONResponse(
            content={
                "error": (
                    "YAML import expects one graph per database: "
                    "database and SQL schema names must match."
                )
            },
            status_code=400,
        )

    documents = []
    for uploaded_file in files:
        documents.append((uploaded_file.filename or "metadata.yml", await uploaded_file.read()))

    generator = YamlSchemaLoader.load_documents(
        prefix=request.state.user_id,
        documents=documents,
        database=database_name,
        schema=database_name,
        execute_url=execute_url,
        replace=replace,
    )

    async def stream():
        success = False
        async for step_success, message in generator:
            success = step_success
            event_type = "reasoning_step" if step_success else "error"
            yield json.dumps({"type": event_type, "message": message}) + MESSAGE_DELIMITER
            if not step_success:
                return
        yield json.dumps({
            "type": "final_result",
            "success": success,
            "message": "YAML schema loaded successfully",
        }) + MESSAGE_DELIMITER

    return StreamingResponse(stream(), media_type="application/json")


@database_router.post(
    "/database/enrich",
    operation_id="enrich_database_from_documents",
    tags=["mcp_tool"],
    responses={401: UNAUTHORIZED_RESPONSE},
)
@token_required
async def enrich_database_from_documents(
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    database: str = Form(...),
    execute_url: Optional[str] = Form(None),
    user_rules: Optional[str] = Form(None),
    knowledge: Optional[str] = Form(None),
):
    """
    Enrich an existing, database-built graph from arbitrary uploaded documents.

    The graph must already exist (built via ``POST /database``); this endpoint
    NEVER creates schema. An LLM agent reads the uploaded documents (any type:
    ``.md/.txt/.csv/.pdf/.docx/.xlsx/.json/.yml/...``) together with the live
    graph snapshot, the optional ``user_rules`` and ``knowledge``, and proposes
    grounded enrichments (table/column descriptions, gap-only primary keys and
    NOT NULL flags, foreign-key links). A deterministic gate validates the
    proposal against the live schema before it is merged through the same engine
    the YAML route uses. The database stays authoritative: data types are never
    changed and only gaps are filled.

    Streams ``reasoning_step``/``final_result`` exactly like ``POST /database/yaml``.
    """
    database_name = _normalize_form_identifier(database)

    documents = []
    for uploaded_file in (files or []):
        documents.append((uploaded_file.filename or "document", await uploaded_file.read()))

    generator = AgentSchemaLoader.enrich_documents(
        prefix=request.state.user_id,
        documents=documents,
        database=database_name,
        execute_url=execute_url,
        user_rules=user_rules,
        knowledge=knowledge,
    )

    async def stream():
        success = False
        async for step_success, message in generator:
            success = step_success
            event_type = "reasoning_step" if step_success else "error"
            yield json.dumps({"type": event_type, "message": message}) + MESSAGE_DELIMITER
            if not step_success:
                return
        yield json.dumps({
            "type": "final_result",
            "success": success,
            "message": "Schema enriched successfully",
        }) + MESSAGE_DELIMITER

    return StreamingResponse(stream(), media_type="application/json")
