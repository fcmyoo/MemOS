from __future__ import annotations
from typing import TYPE_CHECKING, Any

from memos.configs.memory import TreeTextMemoryConfig
from memos.embedders.base import BaseEmbedder
from memos.graph_dbs.base import BaseGraphDB
from memos.llms.base import BaseLLM
from memos.log import get_logger
from memos.mem_reader.base import BaseMemReader
from memos.memories.textual.tree import TreeTextMemory
from memos.memories.textual.item import TextualMemoryItem
from memos.memories.textual.tree_text_memory.organize.manager import MemoryManager
from memos.memories.textual.tree_text_memory.retrieve.bm25_util import EnhancedBM25
from memos.memories.textual.tree_text_memory.retrieve.retrieve_utils import FastTokenizer
from memos.reranker.base import BaseReranker


if TYPE_CHECKING:
    from memos.embedders.factory import OllamaEmbedder
    from memos.graph_dbs.factory import Neo4jGraphDB
    from memos.llms.factory import AzureLLM, OllamaLLM, OpenAILLM


logger = get_logger(__name__)


class SimpleTreeTextMemory(TreeTextMemory):
    """General textual memory implementation for storing and retrieving memories."""

    def __init__(
        self,
        llm: BaseLLM,
        embedder: BaseEmbedder,
        mem_reader: BaseMemReader,
        graph_db: BaseGraphDB,
        reranker: BaseReranker,
        memory_manager: MemoryManager,
        config: TreeTextMemoryConfig,
        internet_retriever: None = None,
        is_reorganize: bool = False,
        tokenizer: FastTokenizer | None = None,
        include_embedding: bool = False,
    ):
        """Initialize memory with the given configuration."""
        self.config: TreeTextMemoryConfig = config
        self.mode = self.config.mode
        logger.info(f"Tree mode is {self.mode}")

        self.extractor_llm: OpenAILLM | OllamaLLM | AzureLLM = llm
        self.dispatcher_llm: OpenAILLM | OllamaLLM | AzureLLM = llm
        self.embedder: OllamaEmbedder = embedder
        self.graph_store: Neo4jGraphDB = graph_db
        self.search_strategy = config.search_strategy
        self.bm25_retriever = (
            EnhancedBM25()
            if self.search_strategy and self.search_strategy.get("bm25", False)
            else None
        )
        self.tokenizer = tokenizer
        self.reranker = reranker
        self.memory_manager: MemoryManager = memory_manager
        # Create internet retriever if configured
        self.internet_retriever = None
        if config.internet_retriever is not None:
            self.internet_retriever = internet_retriever
            logger.info(
                f"Internet retriever initialized with backend: {config.internet_retriever.backend}"
            )
        else:
            logger.info("No internet retriever configured")
        self.include_embedding = include_embedding

    def add(
        self,
        memories: list["TextualMemoryItem" | dict[str, Any]],
        user_name: str | None = None,
        **kwargs,
    ) -> list[str]:
        """
        Add memories with embedding injection for Neo4jCommunityGraphDB.

        Neo4jCommunityGraphDB.add_nodes_batch() requires embedding in
        metadata, but TextualMemoryItem stores it at the top level.
        This override injects it into metadata before passing to manager.
        """
        from memos.memories.textual.item import TextualMemoryItem as TMI
        processed: list = []
        for mem in memories:
            if isinstance(mem, dict):
                mem = TMI(**mem)
            # Inject top-level embedding into metadata if not already set
            emb = getattr(mem, 'embedding', None)
            meta_emb = getattr(mem.metadata, 'embedding', None) if mem.metadata else None
            if emb is not None and meta_emb is None:
                mem.metadata.embedding = emb
            processed.append(mem)
        return super().add(processed, user_name=user_name, **kwargs)

