"""REST API（设计文档 §11）。

本阶段提供只读接口与配置查看；重新处理/推送等写接口在后台流水线落地前返回
501 NOT_IMPLEMENTED，先把契约固定下来，避免调用方猜路径。
"""

from __future__ import annotations

import asyncio
import secrets as secrets_module
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .mail import O365MailClient
from .store import EMAIL_STATUSES, PUSH_STATUSES, REVIEW_STATUSES, Store
from .sync import MailSyncService, SyncAlreadyRunning

VERSION = "0.2.0"

IN_FLIGHT_STATUSES: tuple[str, ...] = (
    "PENDING",
    "FETCHED",
    "PARSED",
    "CLASSIFIED",
    "EXTRACTED",
    "VALIDATED",
)


class ApiError(HTTPException):
    """带业务错误码的 HTTP 异常，响应体统一成 `{"error": {...}}`。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _error_body(code: str, message: str, trace_id: str | None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "trace_id": trace_id}}


def create_app(
    settings: Settings,
    store: Store,
    *,
    sync_service: MailSyncService | None = None,
    start_sync: bool = False,
) -> FastAPI:
    """构造 FastAPI 应用。配置与数据访问对象通过 `app.state` 注入，便于测试。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI):  # type: ignore[no-untyped-def]
        task: asyncio.Task[None] | None = None
        if start_sync and sync_service:
            task = asyncio.create_task(sync_service.run_forever())
        try:
            yield
        finally:
            if task:
                sync_service.stop()
                try:
                    await asyncio.wait_for(task, timeout=30)
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    app = FastAPI(
        title="bank-emails",
        version=VERSION,
        description="交易确认单邮件处理 daemon 的状态与统计接口",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.sync_service = sync_service
    app.state.started_at = _now_iso()

    _register_trace_id(app)
    _register_error_handlers(app)
    app.include_router(_build_router())
    return app


def run(settings: Settings, store: Store) -> None:  # pragma: no cover - 由入口调用
    """以当前配置启动 API 服务。"""
    sync_service = MailSyncService(
        settings,
        store,
        client=O365MailClient(settings),
    )
    uvicorn.run(
        create_app(settings, store, sync_service=sync_service, start_sync=True),
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


def _register_trace_id(app: FastAPI) -> None:
    @app.middleware("http")
    async def add_trace_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.trace_id = uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, str(exc.detail), getattr(request.state, "trace_id", None)),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_IMPLEMENTED" if exc.status_code == 501 else f"HTTP_{exc.status_code}"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail), getattr(request.state, "trace_id", None)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "INVALID_REQUEST",
                details or "请求参数非法",
                getattr(request.state, "trace_id", None),
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=_error_body("INTERNAL_ERROR", str(exc), getattr(request.state, "trace_id", None)),
        )


def _require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    """校验 `Authorization: Bearer <API_TOKEN>`。"""
    expected = request.app.state.settings.api_token
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED", "缺少 Bearer 令牌")
    provided = authorization.split(" ", 1)[1].strip()
    if not secrets_module.compare_digest(provided, expected):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED", "令牌无效")


def _build_router() -> APIRouter:
    router = APIRouter()
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(_require_token)])

    # ------------------------------------------------------ 探针（免鉴权）
    @router.get("/healthz", tags=["probe"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz", tags=["probe"])
    def readyz(request: Request) -> JSONResponse:
        health = request.app.state.store.check_health()
        ready = bool(health["db_ok"] and health["writable"] and health["tables"] >= 6)
        payload = {"status": "ready" if ready else "not_ready", **health}
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    # ------------------------------------------------------------- 运行状态
    @api.get("/status", tags=["status"])
    def get_status(request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        state = store.runtime_state()
        by_status = store.email_counts_by_status()
        push_by_status = store.transaction_counts_by_push_status()
        retry = state.get("retry_queue_depth")
        return {
            "status": "running",
            "version": VERSION,
            "started_at": request.app.state.started_at,
            "last_success_at": state.get("last_success_at"),
            "last_error": state.get("last_error"),
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
            "watermark": state.get("watermark"),
            "queue": {
                "pending": sum(by_status.get(name, 0) for name in IN_FLIGHT_STATUSES),
                "retry": int(retry) if retry else 0,
                "needs_review": sum(by_status.get(name, 0) for name in REVIEW_STATUSES),
                "push_failed": sum(
                    push_by_status.get(name, 0) for name in ("FAILED", "NEEDS_REVIEW")
                ),
            },
            "emails_by_status": by_status,
            "transactions_by_push_status": push_by_status,
            "totals": store.counts(),
        }

    @api.get("/stats", tags=["status"])
    def get_stats(
        request: Request,
        date_from: str | None = Query(default=None, alias="from", description="区间起（ISO8601）"),
        date_to: str | None = Query(default=None, alias="to", description="区间止（ISO8601）"),
    ) -> dict[str, Any]:
        return request.app.state.store.stats(date_from, date_to)

    @api.get("/config", tags=["status"])
    def get_config(request: Request) -> dict[str, Any]:
        """当前生效配置，密钥只显示 `***`。"""
        return request.app.state.settings.public_dict()

    # ----------------------------------------------------------------- 邮件
    @api.get("/emails", tags=["emails"])
    def list_emails(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        sender: str | None = None,
        date_from: str | None = Query(default=None, alias="from"),
        date_to: str | None = Query(default=None, alias="to"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        if status_filter and status_filter not in EMAIL_STATUSES:
            raise ApiError(400, "INVALID_STATUS", f"status 必须是 {'/'.join(EMAIL_STATUSES)} 之一")
        result = request.app.state.store.list_emails(
            status=status_filter,
            sender=sender,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )
        return asdict(result)

    @api.get("/emails/{email_id}", tags=["emails"])
    def get_email(request: Request, email_id: int) -> dict[str, Any]:
        store: Store = request.app.state.store
        email = store.get_email(email_id)
        if not email:
            raise ApiError(404, "EMAIL_NOT_FOUND", f"邮件 {email_id} 不存在")
        return {
            "email": email,
            "events": store.list_email_events(email_id),
            "attachments": store.list_attachments(email_id),
            "transactions": store.list_email_transactions(email_id),
        }

    @api.get("/emails/{email_id}/attachments/{attachment_id}", tags=["emails"])
    def get_attachment(request: Request, email_id: int, attachment_id: int) -> dict[str, Any]:
        attachment = request.app.state.store.get_attachment(email_id, attachment_id)
        if not attachment:
            raise ApiError(404, "ATTACHMENT_NOT_FOUND", f"附件 {attachment_id} 不存在")
        return attachment

    @api.post("/emails/{email_id}/reprocess", tags=["emails"], status_code=501)
    def reprocess_email(email_id: int) -> dict[str, Any]:
        raise ApiError(501, "NOT_IMPLEMENTED", f"重新处理邮件 {email_id} 依赖后台流水线，尚未实现")

    # ----------------------------------------------------------------- 交易
    @api.get("/transactions", tags=["transactions"])
    def list_transactions(
        request: Request,
        push_status: str | None = None,
        symbol: str | None = None,
        issuer_name: str | None = None,
        email_id: int | None = None,
        date_from: str | None = Query(default=None, alias="from"),
        date_to: str | None = Query(default=None, alias="to"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        if push_status and push_status not in PUSH_STATUSES:
            raise ApiError(
                400, "INVALID_STATUS", f"push_status 必须是 {'/'.join(PUSH_STATUSES)} 之一"
            )
        result = request.app.state.store.list_transactions(
            push_status=push_status,
            symbol=symbol,
            issuer_name=issuer_name,
            email_id=email_id,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )
        return asdict(result)

    @api.get("/transactions/{transaction_id}", tags=["transactions"])
    def get_transaction(request: Request, transaction_id: int) -> dict[str, Any]:
        transaction = request.app.state.store.get_transaction(transaction_id)
        if not transaction:
            raise ApiError(404, "TRANSACTION_NOT_FOUND", f"交易 {transaction_id} 不存在")
        return transaction

    @api.post("/transactions/{transaction_id}/push", tags=["transactions"], status_code=501)
    def push_transaction(transaction_id: int) -> dict[str, Any]:
        raise ApiError(
            501, "NOT_IMPLEMENTED", f"推送交易 {transaction_id} 依赖 Odoo 客户端，尚未实现"
        )

    # --------------------------------------------------------- 人工复核与任务
    @api.get("/review", tags=["review"])
    def list_review(
        request: Request, limit: int = Query(default=50, ge=1, le=200)
    ) -> dict[str, Any]:
        items = request.app.state.store.review_queue(limit)
        return {"items": items, "total": len(items)}

    @api.post("/review/{item_id}", tags=["review"], status_code=501)
    def resolve_review(item_id: int) -> dict[str, Any]:
        raise ApiError(501, "NOT_IMPLEMENTED", f"人工修正 {item_id} 尚未实现")

    @api.post("/jobs/fetch", tags=["jobs"])
    async def trigger_fetch(request: Request) -> dict[str, Any]:
        sync_service: MailSyncService | None = request.app.state.sync_service
        if not sync_service:
            raise ApiError(501, "NOT_IMPLEMENTED", "当前应用未配置邮件采集服务")
        try:
            result = await sync_service.run_once()
        except SyncAlreadyRunning as exc:
            raise ApiError(409, "FETCH_ALREADY_RUNNING", str(exc)) from exc
        return result.as_dict()

    router.include_router(api)
    return router
