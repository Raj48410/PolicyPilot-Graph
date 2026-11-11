"""Local-only JWT diagnostics; never prints the bearer token or calls Graph."""

import json

from config import settings
from services.graph_service import GraphTokenError, graph_token_diagnostics, validate_reply_token


def main() -> int:
    print(json.dumps(graph_token_diagnostics(settings.delegated_token), indent=2))
    try:
        report = validate_reply_token(settings.delegated_token, tenant_id=settings.tenant_id,
                                     sender_id=settings.sender_user_id)
    except GraphTokenError as exc:
        print(str(exc))
        return 1
    print("Local checks passed; Graph signature/revocation/access validation is still required." if report["decoded"]
          else "Opaque token: local validation unavailable; Graph must validate this token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
