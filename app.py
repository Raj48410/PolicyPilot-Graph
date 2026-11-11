"""PolicyPilot Graph Edition FastAPI entry point."""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from ai.openai_service import OpenAIService
from config import Settings, settings
from logging_config import configure_logging
from rag.rag import RAGService
from services.graph_service import GraphAuthenticationError, GraphError, GraphService, GraphTokenError
from services.message_processor import MessageProcessor
from services.polling_service import PollingService
from services.subscription_service import SubscriptionService
from services.teams_service import SenderNotInChatError, TeamsService
from services.user_service import UserService
from services.tab_chat import chat_router
from services.copilot_api import copilot_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

class Notification(BaseModel):
    model_config = ConfigDict(extra="allow")
    clientState: str
    tenantId: str
    subscriptionId: str
    changeType: str
    resource: str

class NotificationBatch(BaseModel):
    value: list[Notification] = Field(max_length=1000)

def create_app(config: Settings = settings) -> FastAPI:
    graph = GraphService(config)
    subscriptions = SubscriptionService(graph, config)
    ai = OpenAIService(config)
    processor = MessageProcessor(TeamsService(graph, config), UserService(graph, config),
                                 RAGService(), ai, config)

    async def validate_reply_identity(application: FastAPI) -> None:
        application.state.reply_account_ready = False
        try:
            await processor.teams.validate_reply_account()
        except (GraphError, GraphTokenError, RuntimeError, ValueError, OSError) as exc:
            logger.error("Reply account startup validation failed: %s", exc)
        else:
            application.state.reply_account_ready = True

    async def maintain_subscription(application: FastAPI) -> None:
        # The HTTP server must start BEFORE Graph calls back to validate the URL.
        await asyncio.sleep(1)
        current: dict | None = None
        restore_needed = True
        while True:
            try:
                if restore_needed:
                    current = await subscriptions.load_subscription()
                    restore_needed = False
                if current:
                    expires = datetime.fromisoformat(current["expirationDateTime"].replace("Z", "+00:00"))
                    if expires <= datetime.now(timezone.utc):
                        current = None
                current = (await subscriptions.renew_subscription(current["id"]) if current
                           else await subscriptions.create_subscription())
                application.state.subscription_id = current["id"]
                application.state.subscription_ready = True
                expires = datetime.fromisoformat(current["expirationDateTime"].replace("Z", "+00:00"))
                await asyncio.sleep(max(30, (expires - datetime.now(timezone.utc)).total_seconds() - 300))
            except GraphError as exc:
                application.state.subscription_ready = False
                if exc.status == 404:
                    current = None
                logger.error("Subscription maintenance failed: %s", exc)
                await asyncio.sleep(max(30, exc.retry_after or 0))
            except GraphAuthenticationError as exc:
                application.state.subscription_ready = False
                logger.error("Subscription maintenance failed: %s", exc)
                await asyncio.sleep(30)
            except (ValueError, KeyError, RuntimeError, OSError) as exc:
                application.state.subscription_ready = False
                logger.error("Subscription maintenance failed (%s)", type(exc).__name__)
                await asyncio.sleep(30)

    async def consume(application: FastAPI) -> None:
        while True:
            batch = await application.state.queue.get()
            try:
                for notification in batch:
                    try:
                        await application.state.processor.process_notification(notification)
                    except SenderNotInChatError as exc:
                        logger.warning("%s reply_user_id=%s chat_id=%s; no reply sent",
                                       exc, exc.user_id, exc.chat_id)
                    except Exception as exc:
                        # Never log notifications, tokens, user questions or full exception text.
                        detail = str(exc) if isinstance(exc, (GraphError, GraphTokenError)) else type(exc).__name__
                        logger.error("Message processing failed (%s); replay is required", detail)
            finally:
                application.state.queue.task_done()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging()
        application.state.subscription_ready = False
        application.state.subscription_id = None
        application.state.queue = asyncio.Queue(maxsize=100)
        # Validate identity in the background so webhook validation is never blocked.
        tasks = [asyncio.create_task(consume(application)),
                 asyncio.create_task(validate_reply_identity(application))]
        application.state.poller = None
        if config.receive_mode == "polling":
            try:
                config.validate_polling()
            except ValueError as exc:
                logger.error("Polling disabled: %s", exc)
            else:
                poller = PollingService(graph, application.state.processor, config)
                application.state.poller = poller
                tasks.append(asyncio.create_task(poller.run()))
        elif config.receive_mode != "webhook":
            logger.error("GRAPH_RECEIVE_MODE must be polling or webhook")
        elif config.auto_subscribe:
            try:
                config.validate_graph()
            except ValueError as exc:
                logger.error("Automatic subscriptions disabled: %s", exc)
            else:
                tasks.append(asyncio.create_task(maintain_subscription(application)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await ai.close()

    application = FastAPI(title="PolicyPilot Graph Edition", lifespan=lifespan)
    application.state.processor = processor
    application.include_router(chat_router(config, graph, processor))
    application.include_router(copilot_router(config, processor))

    @application.middleware("http")
    async def prevent_chat_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return HTMLResponse(
            content=(Path(__file__).parent / "static" / "chat.html").read_text(encoding="utf-8"),
            headers={"Content-Security-Policy":
                     "frame-ancestors https://teams.microsoft.com "
                     "https://*.teams.microsoft.com https://*.cloud.microsoft;"},
        )

    @application.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @application.get("/health")
    async def health() -> dict:
        poller = getattr(application.state, "poller", None)
        return {"status": "healthy", "subscription_ready": getattr(application.state, "subscription_ready", False),
                "reply_account_ready": getattr(application.state, "reply_account_ready", False),
                "receive_mode": config.receive_mode,
                "polling_ready": bool(poller and poller.ready),
                "polling_last_success": poller.last_success if poller else None,
                "polling_error": poller.last_error if poller else None,
                "ai_mode": "azure_openai" if config.ai_configured else "unconfigured",
                "retrieval_mode": "mock"}

    @application.get("/graph/webhook")
    async def validate(validationToken: str | None = None) -> PlainTextResponse:
        if validationToken is None:
            raise HTTPException(400, "validationToken is required")
        return PlainTextResponse(validationToken)

    @application.post("/graph/webhook")
    async def webhook(request: Request):
        # Graph validates notification URLs with POST, as well as the requested GET helper.
        token = request.query_params.get("validationToken")
        if token is not None:
            return PlainTextResponse(token)
        if config.receive_mode != "webhook":
            raise HTTPException(409, "Webhook delivery is inactive while polling is selected")
        if not config.client_state or not config.tenant_id:
            raise HTTPException(503, "Webhook configuration is incomplete")
        try:
            batch = NotificationBatch.model_validate(await request.json())
        except ValueError:
            raise HTTPException(400, "Invalid notification batch") from None
        for notification in batch.value:
            if (not hmac.compare_digest(notification.clientState.encode(), config.client_state.encode())
                    or notification.tenantId != config.tenant_id):
                raise HTTPException(403, "Invalid notification origin")
        try:
            application.state.queue.put_nowait([item.model_dump() for item in batch.value])
        except asyncio.QueueFull:
            raise HTTPException(503, "Notification queue is full; retry later") from None
        return {"status": "received"}

    return application

app = create_app()
