"""Agent implementations — one module per persona.

Each agent owns: loading its persona prompt, calling its LLM, validating
the result, and emitting EventLog entries. Agents are stateless functions —
they take inputs, produce one output, return.
"""
