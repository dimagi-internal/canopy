"""Prompt template loader.

Prompts live as packaged data files (``orchestrator/prompts/<name>.md``) and are
resolved via ``importlib.resources`` rather than ``Path(__file__).parent`` so the
lookup is install-safe — it finds the ``.md`` even when ``orchestrator`` is
installed as a wheel or relocated/zipped, where a filesystem-relative path may not
point at the data files. Belt-and-suspenders with the ``package-data`` declaration
in ``pyproject.toml``: the data ships AND is addressed portably.
"""
from importlib.resources import files


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template and fill in placeholders.

    ``name`` is the stem of a packaged ``orchestrator/prompts/<name>.md`` file.
    Only the template's own ``{placeholder}`` braces are interpreted by
    ``str.format`` — braces inside the substituted values (e.g. JSON corpora)
    are inserted literally and never re-parsed.
    """
    template = files("orchestrator.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")
    return template.format(**kwargs)
