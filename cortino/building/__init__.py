from pydantic import BaseModel

from cortino.llm import LLM


class BuildError(Exception):
    """A builder could not produce valid files."""


def ask_valid(llm: LLM, system: str, prompt: str, schema: type[BaseModel], check) -> BaseModel:
    """Ask the builder; on semantic errors (check returns messages) retry once, then fail."""
    current = prompt
    for _ in range(2):
        reply = llm.structured("builder", system, current, schema)
        errors = check(reply)
        if not errors:
            return reply
        listed = "\n".join(f"- {e}" for e in errors)
        current = f"{prompt}\n\nYour previous reply was rejected:\n{listed}\nFix these and reply again."
    raise BuildError(f"builder: reply failed validation twice: {'; '.join(errors)}")
