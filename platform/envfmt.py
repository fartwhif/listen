"""Supervisor's `environment=` format, in one place.

A supervisor program's environment travels as ONE line, and supervisord runs
that line through TWO parsers before a value ever reaches an app. Both fail the
whole generated file - supervisord refuses to load a config it cannot parse, so
one bad value takes down every partition in the box, not just its own program:

1. `supervisor/options.py:expand` (via `supervisor.options.expand`) applies
   Python's %-interpolation to the RAW line, with `here`, `program_name` and
   every `ENV_*` in scope - that is how `%(here)s` works. A lone `%` is then a
   `ValueError` at config load, whatever it came from: a password, a search
   pattern, a printf fragment. Doubling (`%%`) survives, because interpolation
   turns `%%` back into `%` before stage 2 sees the line.
2. `supervisor/datatypes.py:dict_of_key_value_pairs` runs `shlex.shlex` over
   the interpolated line with `wordchars += "/.+-():"`. That constructor is
   NON-POSIX: backslashes are not escapes inside quotes, and the quote
   characters stay IN the token. It then takes tokens `i:i+3` as `KEY = VALUE`
   (so `,`, whitespace and `=` outside quotes are pair boundaries) and stores
   `value.strip('"\'')` - a strip of BOTH kinds, repeatedly, from BOTH ends.

What that combination allows, and what it silently mangles (all of it measured
against the shipped supervisor in `tests/test_envfmt.py`, not inferred):

* quoting is required around a space, `,`, `=`, `%` or a quote: bare, the first
  space/comma ends the value and the tail breaks the token window;
* the wrapping quote must be the kind the value does NOT contain, because a
  quoted token ends at the first matching quote and nothing escapes it - so a
  value holding both kinds cannot be expressed at all;
* the final `strip` removes every leading/trailing quote of either kind, so a
  value that STARTS or ENDS with a quote arrives stripped, with or without
  wrapping: inexpressible;
* backslashes are inert, so `\\` (and even a value that ends in one) round-trips
  inside quotes - POSIX-style escaping is NOT a thing here, and this module
  therefore never escapes a backslash;
* a `%` must be doubled (stage 1) but nothing else is, and doubling happens
  BEFORE the value is wrapped: quoting first and doubling after would hand the
  app a literal `%%`.

Newlines and carriage returns are refused for a different reason: this is a
config FILE line, so one would end the `environment=` line and hand the rest to
the INI reader as a continuation.

Everything gen-conf generates itself (paths, loopback URLs, ports) is inside
the safe domain. The shapes that are not come from a partition's `settings` or
from a credential in the container's environment, which is why `hub` refuses
them at the CLI and gen-conf drops them loudly instead of writing a file
supervisord will reject.
"""
import shlex

QUOTE_CHARS = ('"', "'")

# Quoted verbatim by every refusal so a CLI user and a log reader see the same
# reason and the same escape hatches.
REASON = ("supervisor's environment format cannot hold it: a value containing "
          "both quote kinds, starting or ending with a quote, or containing a "
          "newline cannot be passed to a program (spaces, commas, %, = and "
          "backslashes inside a value are fine)")


def env_val(v: str) -> str | None:
    """One value in supervisor's format, or None if it cannot be written.

    Doubling comes first (it is stage 1's requirement, and quoting must wrap
    the doubled text); then the shapes the two parsers cannot carry.
    """
    if "\n" in v or "\r" in v:
        # A config FILE line: a newline would end the environment= line and
        # hand the remainder to the INI reader, failing supervisord's load.
        return None
    e = v.replace("%", "%%")
    if '"' in e and "'" in e:
        # shlex ends a quoted token at the first matching quote and nothing
        # escapes it here, so no delimiter exists for a value holding both.
        return None
    if e[:1] in QUOTE_CHARS or e[-1:] in QUOTE_CHARS:
        return None
    # Uniform quoting: `%%` and a leading `-` stay literal text, and a value
    # needs no quoting at all only when it has no space/comma/=/quote, which
    # guessing is not worth.
    return f"'{e}'" if '"' in e else f'"{e}"'


def renderable(v: str) -> bool:
    return env_val(v) is not None


def unsuitable(vals: dict) -> list:
    """Names in `vals` whose value the format cannot hold, in dict order."""
    return [k for k, v in vals.items() if env_val(v) is None]


def env_str(vals: dict) -> str:
    """`environment=` body for a whole program.

    Raises ValueError naming the offending key: a silently truncated credential
    is worse than a box that refuses to start. Both callers screen first
    (`load_registry` for settings, `passthrough` for credentials), so this is
    the backstop, not the usual path.
    """
    out = []
    for k, v in vals.items():
        text = env_val(v)
        if text is None:
            raise ValueError(f"{k}={v!r}: {REASON}")
        out.append(f"{k}={text}")
    return ",".join(out)


def parse_env_line(line: str) -> dict:
    """Read an `environment=` body back into a NAME -> VALUE map.

    Production code never needs it - gen-conf writes, supervisor reads, and the
    apps only ever see `os.environ` - but a reader has to exist for a test to
    assert what a program actually receives instead of what the emitter
    intended. It mirrors supervisor's algorithm step for step (same shlex flags,
    same three-token window, same quote strip) and `tests/test_envfmt.py` proves
    the two agree over every generated line plus a corpus of hostile values, so
    a divergence fails a test rather than changing a config's meaning.

    Callers feed it a line that has ALREADY been through %-interpolation (i.e.
    the bytes supervisord would hand the shlex stage), because the raw file line
    still holds `%%`.
    """
    lexer = shlex.shlex(line)
    lexer.wordchars += "/.+-():"
    tokens = list(lexer)
    out = {}
    i = 0
    while i < len(tokens):
        trip = tokens[i:i + 3]
        if len(trip) != 3 or trip[1] != "=":
            raise ValueError(f"Unexpected end of key/value pairs in {line!r}")
        out[trip[0]] = trip[2].strip('"\'')
        i += 4
    return out
