"""Print setup readiness without printing credentials or making network calls."""

from config import settings
from services.graph_service import GraphTokenError, normalize_graph_token, validate_reply_token


def main() -> int:
    ready = True
    try:
        if settings.receive_mode == "polling":
            settings.validate_polling()
            print("Polling configuration: OK (format only); no public webhook is needed")
        elif settings.receive_mode == "webhook":
            settings.validate_graph()
            print("Graph subscription configuration: OK (format only)")
        else:
            raise ValueError("GRAPH_RECEIVE_MODE must be polling or webhook")
    except ValueError as exc:
        print(f"Graph subscription configuration: {exc}")
        ready = False
    for name, value in (("Delegated reply token", settings.delegated_token),
                        ("Delegated sender ID", settings.sender_user_id)):
        print(f"{name}: {'configured (not verified)' if value else 'missing; run python -m scripts.login'}")
        ready = ready and bool(value)
    if settings.delegated_token and settings.sender_user_id:
        try:
            report = validate_reply_token(normalize_graph_token(settings.delegated_token),
                                          tenant_id=settings.tenant_id, sender_id=settings.sender_user_id)
            print("Delegated reply token preflight: OK (claims only; Graph access not verified)"
                  if report["decoded"] else "Delegated reply token: opaque; Graph must verify it")
        except GraphTokenError as exc:
            print(f"Delegated reply token preflight: {exc}")
            ready = False
    print(f"AI: {'Azure OpenAI configured (not verified)' if settings.ai_configured else 'unconfigured'}")
    print("Retrieval: mock; SharePoint and Outlook integration: not implemented")
    print("No live credentials or permissions were validated.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
