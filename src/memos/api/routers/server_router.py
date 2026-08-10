"""
Server API Router for MemOS (Class-based handlers version).

This router demonstrates the improved architecture using class-based handlers
with dependency injection, providing better modularity and maintainability.

Comparison with function-based approach:
- Cleaner code: No need to pass dependencies in every endpoint
- Better testability: Easy to mock handler dependencies
- Improved extensibility: Add new handlers or modify existing ones easily
- Clear separation of concerns: Router focuses on routing, handlers handle business logic
"""

import os
import random as _random
import socket

from fastapi import APIRouter, Depends, HTTPException, Query

from memos.api import handlers
from memos.api.access_control import CubeAccessControl
from memos.api.handlers.add_handler import AddHandler
from memos.api.handlers.base_handler import HandlerDependencies
from memos.api.handlers.chat_handler import ChatHandler
from memos.api.handlers.cube_handler import CubeHandler
from memos.api.handlers.feedback_handler import FeedbackHandler
from memos.api.handlers.search_handler import SearchHandler
from memos.api.middleware.auth import AuthContext, get_current_user
from memos.api.product_models import (
    AllStatusResponse,
    APIADDRequest,
    APIChatCompleteRequest,
    APIFeedbackRequest,
    APISearchRequest,
    ChatBusinessRequest,
    ChatPlaygroundRequest,
    ChatRequest,
    CreateCubeRequest,
    CreateCubeResponse,
    DeleteMemoryByRecordIdRequest,
    DeleteMemoryByRecordIdResponse,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    ExistMemCubeIdRequest,
    ExistMemCubeIdResponse,
    GetMemoryDashboardRequest,
    GetMemoryPlaygroundRequest,
    GetMemoryRequest,
    GetMemoryResponse,
    GetUserNamesByMemoryIdsRequest,
    GetUserNamesByMemoryIdsResponse,
    MemoryResponse,
    RecoverMemoryByRecordIdRequest,
    RecoverMemoryByRecordIdResponse,
    RegisterCubeRequest,
    RegisterCubeResponse,
    SearchResponse,
    StatusResponse,
    SuggestionRequest,
    SuggestionResponse,
    TaskQueueResponse,
)
from memos.log import get_logger
from memos.mem_scheduler.base_scheduler import BaseScheduler
from memos.mem_scheduler.utils.status_tracker import TaskStatusTracker
from memos.mem_user.user_manager import UserManager


logger = get_logger(__name__)

router = APIRouter(prefix="/product", tags=["Server API"])

# Instance ID for identifying this server instance in logs and responses
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{_random.randint(1000, 9999)}"

# Initialize all server components
components = handlers.init_server()

# Shared user manager and access control: one authorization object per process
# so every handler validates cube membership against the same database.
user_manager = UserManager()
access_control = CubeAccessControl(user_manager)

# Create dependency container
dependencies = HandlerDependencies.from_init_server(
    {**components, "access_control": access_control}
)

# Initialize all handlers with dependency injection
search_handler = SearchHandler(dependencies)
add_handler = AddHandler(dependencies)
chat_handler = (
    ChatHandler(
        dependencies=dependencies,
        chat_llms=components["chat_llms"],
        playground_chat_llms=components.get("playground_chat_llms"),
        search_handler=search_handler,
        add_handler=add_handler,
        online_bot=components.get("online_bot"),
    )
    if os.getenv("ENABLE_CHAT_API", "false") == "true"
    else None
)
feedback_handler = FeedbackHandler(dependencies)
cube_handler = CubeHandler(dependencies)
# Extract commonly used components for function-based handlers
# (These can be accessed from the components dict without unpacking all of them)
mem_scheduler: BaseScheduler = components["mem_scheduler"]
llm = components["llm"]
naive_mem_cube = components["naive_mem_cube"]
redis_client = components["redis_client"]
status_tracker = TaskStatusTracker(redis_client=redis_client)
graph_db = components["graph_db"]


# =============================================================================
# Search API Endpoints
# =============================================================================


@router.post("/search", summary="Search memories", response_model=SearchResponse)
def search_memories(
    search_req: APISearchRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """
    Search memories for a specific user.

    This endpoint uses the class-based SearchHandler for better code organization.
    """
    search_results = search_handler.handle_search_memories(search_req, current_user)
    return search_results


# =============================================================================
# Add API Endpoints
# =============================================================================


@router.post("/add", summary="Add memories", response_model=MemoryResponse)
def add_memories(
    add_req: APIADDRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """
    Add memories for a specific user.

    This endpoint uses the class-based AddHandler for better code organization.
    """
    return add_handler.handle_add_memories(add_req, current_user)


# =============================================================================
# Cube Management API Endpoints
# =============================================================================


@router.post("/create_cube", summary="Create a new memory cube", response_model=CreateCubeResponse)
async def create_cube(
    request: CreateCubeRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
) -> CreateCubeResponse:
    """
    Create a new memory cube for a user.

    Memory cubes are containers that store different types of memories (textual, activation, parametric).
    Each cube can be owned by a user and shared with other users.

    **Note on cube_id vs mem_cube_id:**
    These terms are used interchangeably throughout the API:
    - `cube_id` is the canonical identifier for a cube
    - `mem_cube_id` appears in many legacy endpoints and means the same thing
    - When using other endpoints (search, add, chat), you can reference this cube using either term

    **Semantic Clarification:**
    - **Single mem_cube_id** (deprecated): Used in older endpoints to identify a single cube.
      New code should use `readable_cube_ids` / `writable_cube_ids` lists instead.
    - **readable_cube_ids**: List of cube IDs the user can read from (used in search/chat)
    - **writable_cube_ids**: List of cube IDs the user can write to (used in add/chat)
    """
    return await cube_handler.create_cube(request, current_user)


@router.post(
    "/register_cube",
    summary="Register an existing memory cube",
    response_model=RegisterCubeResponse,
)
async def register_cube(
    request: RegisterCubeRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
) -> RegisterCubeResponse:
    """
    Register an existing memory cube with the MOS system.

    This method loads and registers a memory cube from a file path or creates a new one
    if the path doesn't exist. The cube becomes available for memory operations.

    **Note on cube_id vs mem_cube_id:**
    These terms are used interchangeably throughout the API. The registered cube can then
    be referenced by its cube_id/mem_cube_id in other endpoints.

    **Current Status:**
    This endpoint validates the registration request. Full registration functionality
    requires architectural integration with MOSCore, which will be completed in a future update.
    """
    return await cube_handler.register_cube(request, current_user)


# =============================================================================
# Scheduler API Endpoints
# =============================================================================


@router.get(  # Changed from post to get
    "/scheduler/allstatus",
    summary="Get detailed scheduler status",
    response_model=AllStatusResponse,
)
def scheduler_allstatus():
    """Get detailed scheduler status including running tasks and queue metrics."""
    return handlers.scheduler_handler.handle_scheduler_allstatus(
        mem_scheduler=mem_scheduler, status_tracker=status_tracker
    )


@router.get(  # Changed from post to get
    "/scheduler/status", summary="Get scheduler running status", response_model=StatusResponse
)
def scheduler_status(
    user_id: str = Query(..., description="User ID"),
    task_id: str | None = Query(None, description="Optional Task ID to query a specific task"),
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """Get scheduler running status."""
    return handlers.scheduler_handler.handle_scheduler_status(
        user_id=user_id,
        task_id=task_id,
        status_tracker=status_tracker,
        current_user=current_user,
        access_control=access_control,
    )


@router.get(  # Changed from post to get
    "/scheduler/task_queue_status",
    summary="Get scheduler task queue status",
    response_model=TaskQueueResponse,
)
def scheduler_task_queue_status(
    user_id: str = Query(..., description="User ID whose queue status is requested"),
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """Get scheduler task queue backlog/pending status for a user."""
    return handlers.scheduler_handler.handle_task_queue_status(
        user_id=user_id,
        mem_scheduler=mem_scheduler,
        current_user=current_user,
        access_control=access_control,
    )


@router.post("/scheduler/wait", summary="Wait until scheduler is idle for a specific user")
def scheduler_wait(
    user_name: str,
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.5,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """Wait until scheduler is idle for a specific user."""
    return handlers.scheduler_handler.handle_scheduler_wait(
        user_name=user_name,
        status_tracker=status_tracker,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        current_user=current_user,
        access_control=access_control,
    )


@router.get("/scheduler/wait/stream", summary="Stream scheduler progress for a user")
def scheduler_wait_stream(
    user_name: str,
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.5,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """Stream scheduler progress via Server-Sent Events (SSE)."""
    return handlers.scheduler_handler.handle_scheduler_wait_stream(
        user_name=user_name,
        status_tracker=status_tracker,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        instance_id=INSTANCE_ID,
        current_user=current_user,
        access_control=access_control,
    )


# =============================================================================
# Chat API Endpoints
# =============================================================================


@router.post("/chat/complete", summary="Chat with MemOS (Complete Response)")
def chat_complete(
    chat_req: APIChatCompleteRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """
    Chat with MemOS for a specific user. Returns complete response (non-streaming).

    This endpoint uses the class-based ChatHandler.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_complete(chat_req, current_user)


@router.post("/chat/stream", summary="Chat with MemOS")
def chat_stream(
    chat_req: ChatRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """
    Chat with MemOS for a specific user. Returns SSE stream.

    This endpoint uses the class-based ChatHandler which internally
    composes SearchHandler and AddHandler for a clean architecture.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_stream(chat_req, current_user)


@router.post("/chat/stream/playground", summary="Chat with MemOS playground")
def chat_stream_playground(chat_req: ChatPlaygroundRequest):
    """
    Chat with MemOS for a specific user. Returns SSE stream.

    This endpoint uses the class-based ChatHandler which internally
    composes SearchHandler and AddHandler for a clean architecture.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_stream_playground(chat_req)


# =============================================================================
# Suggestion API Endpoints
# =============================================================================


@router.post(
    "/suggestions",
    summary="Get suggestion queries",
    response_model=SuggestionResponse,
)
def get_suggestion_queries(
    suggestion_req: SuggestionRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """Get suggestion queries for a specific user with language preference."""
    return handlers.suggestion_handler.handle_get_suggestion_queries(
        user_id=suggestion_req.user_id,
        language=suggestion_req.language,
        message=suggestion_req.message,
        llm=llm,
        naive_mem_cube=naive_mem_cube,
        cube_id=suggestion_req.mem_cube_id,
        current_user=current_user,
        access_control=access_control,
    )


# =============================================================================
# Memory Retrieval Delete API Endpoints
# =============================================================================


@router.post("/get_all", summary="Get all memories for user", response_model=MemoryResponse)
def get_all_memories(
    memory_req: GetMemoryPlaygroundRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """
    Get all memories or subgraph for a specific user.

    If search_query is provided, returns a subgraph based on the query.
    Otherwise, returns all memories of the specified type.
    """
    if memory_req.search_query:
        return handlers.memory_handler.handle_get_subgraph(
            user_id=memory_req.user_id,
            mem_cube_id=(
                memory_req.mem_cube_ids[0] if memory_req.mem_cube_ids else memory_req.user_id
            ),
            query=memory_req.search_query,
            top_k=200,
            naive_mem_cube=naive_mem_cube,
            search_type=memory_req.search_type,
            current_user=current_user,
            access_control=access_control,
        )
    else:
        return handlers.memory_handler.handle_get_all_memories(
            user_id=memory_req.user_id,
            mem_cube_id=(
                memory_req.mem_cube_ids[0] if memory_req.mem_cube_ids else memory_req.user_id
            ),
            memory_type=memory_req.memory_type or "text_mem",
            naive_mem_cube=naive_mem_cube,
            current_user=current_user,
            access_control=access_control,
        )


@router.post("/get_memory", summary="Get memories for user", response_model=GetMemoryResponse)
def get_memories(
    memory_req: GetMemoryRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    return handlers.memory_handler.handle_get_memories(
        get_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
        current_user=current_user,
        access_control=access_control,
    )


@router.get("/get_memory/{memory_id}", summary="Get memory by id", response_model=GetMemoryResponse)
def get_memory_by_id(memory_id: str):
    return handlers.memory_handler.handle_get_memory(
        memory_id=memory_id,
        naive_mem_cube=naive_mem_cube,
    )


@router.post("/get_memory_by_ids", summary="Get memory by ids", response_model=GetMemoryResponse)
def get_memory_by_ids(
    memory_ids: list[str],
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    return handlers.memory_handler.handle_get_memory_by_ids(
        memory_ids=memory_ids,
        naive_mem_cube=naive_mem_cube,
        current_user=current_user,
        access_control=access_control,
    )


@router.post(
    "/delete_memory", summary="Delete memories for user", response_model=DeleteMemoryResponse
)
def delete_memories(
    memory_req: DeleteMemoryRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    return handlers.memory_handler.handle_delete_memories(
        delete_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
        current_user=current_user,
        access_control=access_control,
    )


# =============================================================================
# Feedback API Endpoints
# =============================================================================


@router.post("/feedback", summary="Feedback memories", response_model=MemoryResponse)
def feedback_memories(
    feedback_req: APIFeedbackRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """
    Feedback memories for a specific user.

    This endpoint uses the class-based FeedbackHandler for better code organization.
    """
    return feedback_handler.handle_feedback_memories(feedback_req, current_user)


# =============================================================================
# Other API Endpoints (for internal use)
# =============================================================================


@router.post(
    "/get_user_names_by_memory_ids",
    summary="Get user names by memory ids",
    response_model=GetUserNamesByMemoryIdsResponse,
)
def get_user_names_by_memory_ids(request: GetUserNamesByMemoryIdsRequest):
    """Get user names by memory ids. Now unified to query from graph_db only."""
    result = graph_db.get_user_names_by_memory_ids(memory_ids=request.memory_ids)

    return GetUserNamesByMemoryIdsResponse(
        code=200,
        message="Successfully",
        data=result,
    )


@router.post(
    "/exist_mem_cube_id",
    summary="Check if mem cube id exists",
    response_model=ExistMemCubeIdResponse,
)
def exist_mem_cube_id(request: ExistMemCubeIdRequest):
    """(inner) Check if mem cube id exists."""
    return ExistMemCubeIdResponse(
        code=200,
        message="Successfully",
        data=graph_db.exist_user_name(user_name=request.mem_cube_id),
    )


@router.post("/chat/stream/business_user", summary="Chat with MemOS for business user")
def chat_stream_business_user(chat_req: ChatBusinessRequest):
    """(inner) Chat with MemOS for a specific business user. Returns SSE stream."""
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )

    return chat_handler.handle_chat_stream_for_business_user(chat_req)


@router.post(
    "/delete_memory_by_record_id",
    summary="Delete memory by record id",
    response_model=DeleteMemoryByRecordIdResponse,
)
def delete_memory_by_record_id(
    memory_req: DeleteMemoryByRecordIdRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """(inner) Delete memory nodes by mem_cube_id (user_name) and delete_record_id. Record id is inner field, just for delete and recover memory, not for user to set."""
    # Actor -> cube validation before the graph delete (plan 5.3 #9).
    actor_user_id = access_control.resolve_actor(current_user, None)
    access_control.require_cube_access(
        current_user, actor_user_id, [memory_req.mem_cube_id]
    )
    graph_db.delete_node_by_mem_cube_id(
        mem_cube_id=memory_req.mem_cube_id,
        delete_record_id=memory_req.record_id,
        hard_delete=memory_req.hard_delete,
    )

    return DeleteMemoryByRecordIdResponse(
        code=200,
        message="Called Successfully",
        data={"status": "success"},
    )


@router.post(
    "/recover_memory_by_record_id",
    summary="Recover memory by record id",
    response_model=RecoverMemoryByRecordIdResponse,
)
def recover_memory_by_record_id(
    memory_req: RecoverMemoryByRecordIdRequest,
    current_user: AuthContext = Depends(get_current_user),  # noqa: B008
):
    """(inner) Recover memory nodes by mem_cube_id (user_name) and delete_record_id. Record id is inner field, just for delete and recover memory, not for user to set."""
    # Actor -> cube validation before the graph recover (plan 5.3 #10).
    actor_user_id = access_control.resolve_actor(current_user, None)
    access_control.require_cube_access(
        current_user, actor_user_id, [memory_req.mem_cube_id]
    )
    graph_db.recover_memory_by_mem_cube_id(
        mem_cube_id=memory_req.mem_cube_id,
        delete_record_id=memory_req.delete_record_id,
    )

    return RecoverMemoryByRecordIdResponse(
        code=200,
        message="Called Successfully",
        data={"status": "success"},
    )


@router.post(
    "/get_memory_dashboard", summary="Get memories for dashboard", response_model=GetMemoryResponse
)
def get_memories_dashboard(memory_req: GetMemoryDashboardRequest):
    return handlers.memory_handler.handle_get_memories_dashboard(
        get_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
    )
