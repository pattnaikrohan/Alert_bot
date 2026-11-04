"""Data quality on databases people connect themselves (SQL Server, Snowflake), aggregate-only.

    vault      the password / key a person enters, encrypted at rest
    sql        approved query templates (T-SQL, Snowflake) over allowlisted identifiers; a statement guard
    privacy    the gate: only named numbers leave a query; database errors become fixed sentences; the LLM scrub
    client     connections: read-only transactions, timeouts, a concurrency limit, retries
    tools      the checks as LangChain tools (run by the workflow, never handed to a model)
    graph      the LangGraph workflow: validate → load → select → execute → validate → evaluate → analyse → report
    service    connections, mappings (which pipeline loads which table), suggestions from lineage, the cycle hook

Nothing in here can change data: every statement is a template that passes the guard, runs in a transaction that is
always rolled back, and returns one row of numbers that is checked before anything keeps it.
"""
