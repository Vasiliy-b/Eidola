"""Utility for loading prompt files from the prompts directory."""

import re
from pathlib import Path
from typing import Any

from ..config import settings


def load_prompt(
    filename: str,
    base_dir: Path | None = None,
    variables: dict[str, Any] | None = None,
) -> str:
    """
    Load a prompt from a .md file with optional variable substitution.

    Args:
        filename: Name of the prompt file (e.g., "orchestrator.md" or "persona/default.md")
        base_dir: Base directory for prompts. Defaults to config prompts_dir.
        variables: Dictionary of variables to substitute in the prompt.
                   Variables are referenced as {variable_name} in the prompt.

    Returns:
        The prompt content as a string.

    Raises:
        FileNotFoundError: If the prompt file doesn't exist.

    Example:
        >>> prompt = load_prompt("orchestrator.md", variables={"account": "user1"})
    """
    prompts_dir = base_dir or settings.prompts_dir

    # Handle both absolute and relative paths
    if Path(filename).is_absolute():
        prompt_path = Path(filename)
    else:
        prompt_path = prompts_dir / filename

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    content = prompt_path.read_text(encoding="utf-8")

    # Substitute variables if provided
    if variables:
        content = substitute_variables(content, variables)

    return content


def substitute_variables(content: str, variables: dict[str, Any]) -> str:
    """
    Substitute {variable_name} placeholders in content.

    Args:
        content: The string content with placeholders.
        variables: Dictionary mapping variable names to values.

    Returns:
        Content with variables substituted.
    """
    for key, value in variables.items():
        placeholder = f"{{{key}}}"
        content = content.replace(placeholder, str(value))

    return content


def list_prompts(base_dir: Path | None = None) -> list[Path]:
    """
    List all prompt files in the prompts directory.

    Args:
        base_dir: Base directory for prompts. Defaults to config prompts_dir.

    Returns:
        List of Path objects for all .md files.
    """
    prompts_dir = base_dir or settings.prompts_dir

    if not prompts_dir.exists():
        return []

    return list(prompts_dir.rglob("*.md"))


def validate_prompt_variables(content: str) -> list[str]:
    """
    Extract all variable placeholders from a prompt.

    Args:
        content: The prompt content.

    Returns:
        List of variable names found in the prompt.

    Example:
        >>> vars = validate_prompt_variables("Hello {name}, your account is {account}")
        >>> vars
        ['name', 'account']
    """
    pattern = r"\{(\w+)\}"
    matches = re.findall(pattern, content)
    return list(set(matches))
