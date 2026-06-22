from collections.abc import Callable

class JSONDecodeError(ValueError): ...

def dumps(
    obj: object,
    *,
    skipkeys: bool = ...,
    ensure_ascii: bool = ...,
    check_circular: bool = ...,
    allow_nan: bool = ...,
    cls: type[object] | None = ...,
    indent: int | str | None = ...,
    separators: tuple[str, str] | None = ...,
    default: Callable[[object], object] | None = ...,
    sort_keys: bool = ...,
    **kwds: object,
) -> str: ...

def loads(
    s: str | bytes | bytearray,
    *,
    cls: type[object] | None = ...,
    object_hook: Callable[[dict[str, object]], object] | None = ...,
    parse_float: Callable[[str], object] | None = ...,
    parse_int: Callable[[str], object] | None = ...,
    parse_constant: Callable[[str], object] | None = ...,
    object_pairs_hook: Callable[[list[tuple[str, object]]], object] | None = ...,
    **kwds: object,
) -> object: ...

def dump(obj: object, fp: object, **kwds: object) -> None: ...
def load(fp: object, **kwds: object) -> object: ...
