import json
from pathlib import Path

from pydantic import ValidationError
from sysdesign_engine.schemas.components_schema import ComponentLibrary

DEFAULT_LIBRARY_PATH = Path(__file__).parent / "base_components.json"


class LibraryError(Exception):
    """Raised when a component library JSON fails validation.

    Carries one human-readable line per offending component/field so
    authors can fix the JSON without reading a pydantic traceback.
    """


def _format_error(raw: dict, err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = e["loc"]
        # loc looks like ("components", "<key>", "field", "subfield", ...)
        if len(loc) >= 2 and loc[0] == "components_schema":
            key = loc[1]
            comp_type = raw.get(key, {}).get("type", key)
            field_path = ".".join(str(p) for p in loc[2:])
            if field_path:
                lines.append(f"component '{comp_type}', field '{field_path}': {e['msg']}")
            else:
                lines.append(f"component '{comp_type}': {e['msg']}")
        else:
            field_path = ".".join(str(p) for p in loc)
            lines.append(f"{field_path}: {e['msg']}" if field_path else e["msg"])
    return "\n".join(lines)


def load_library(path: Path = DEFAULT_LIBRARY_PATH) -> ComponentLibrary:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        return ComponentLibrary.model_validate({"components": raw})
    except ValidationError as err:
        raise LibraryError(_format_error(raw, err)) from err
