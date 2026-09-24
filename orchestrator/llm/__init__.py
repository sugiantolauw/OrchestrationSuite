"""The LLM layer (independent review 2026-09-24 item 3): capability-filtered,
synchronously-logged, cache-backed calls to Databricks Model Serving. See
orchestrator.llm.gateway.LLMGateway for the single entry point every node
uses -- nothing outside this package builds a raw model request."""
