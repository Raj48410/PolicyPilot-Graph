"""Mock retrieval; no company policy data or vector database is loaded."""
import logging
logger = logging.getLogger(__name__)

class RAGService:
    async def answer(self, question: str, role: str) -> str:
        logger.debug("Retrieving mock context for %s", role)
        # TODO: Enforce document access BEFORE retrieving real policy data.
        # TODO: Retrieve authorized SharePoint documents through Graph.
        # TODO: Generate embeddings and add vector search (e.g. FAISS) over authorized chunks.
        contexts = {"Admin": "administration and general employee policies",
                    "HR": "HR and general employee policies",
                    "Finance": "finance and general employee policies",
                    "IT": "IT and general employee policies",
                    "Employee": "general employee policies"}
        return f"Mock context: {contexts.get(role, contexts['Employee'])}."
