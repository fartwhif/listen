"""The supervisor `environment=` format, proved against supervisor itself.

Everything multi-tenant that is not a path or a port travels through this one
line per program: a partition's `settings`, a Google password, a comma list of
cookie files. Supervisor parses that line TWICE - %-interpolation over the raw
text, then a shlex token walk - and a failure in either rejects the whole
generated file, which takes down every partition, not just the one with the
awkward value. So the interesting assertions here are not "env_val returns what
I expect" but:

  * what env_str writes, supervisor's own parser reads back as exactly the
    value that went in (round trip over a corpus of hostile values), and
  * the documented inverse `parse_env_line` agrees with supervisor's parser on
    every corpus line, which is what lets the rest of the suite assert on
    generated env without installing supervisor.

HOSTILE is the corpus that MUST survive: the shapes this stack produces plus
the ones a human can type into `hub partition set`. UNEXPRESSIBLE is the
measured remainder - the three families the format genuinely cannot carry (both
quote kinds, a quote at either end, a newline) - and each entry is paired with
a test showing what supervisor makes of it when written raw anyway.

Backslashes are NOT escapes here (supervisor builds `shlex.shlex` with the
default posix=False and only adds wordchars), so they need no rule and are in
HOSTILE. `%` is the one character that needs the OTHER stage's attention.
"""
import importlib
import textwrap

import pytest

envfmt = importlib.import_module("envfmt")

HOSTILE = [
    "plain",
    "a b",                                   # space: bare, it ends the value
    "a,b",                                   # comma: bare, it ends the pair
    "a=b",                                   # = inside a value
    "100%",                                  # lone %: stage 1's failure
    "%%",                                    # already doubled
    "%(here)s",                              # supervisor's own interpolation idiom
    "%(ENV_PROGRAM_NAME)s",                  # ...and an ENV_ one
    "a b, c=d%(x)s",                         # all of them at once
    "it's",                                  # apostrophe -> double-wrap
    'va"lue',                                # interior double quote -> single-wrap
    'multi "quoted" words',                  # wrap quote repeated inside
    'ends"with"quote',                       # wrap quote interior, ends are letters
    'a "b" c',                               # interior pair
    'a"b,c"d',                               # commas AND wrap quotes
    "back\\slash",                           # interior backslash (inert)
    "trailing backslash\\",                  # trailing backslash (also inert)
    "\\",                                    # a lone backslash
    "\\both",                                # leading backslash
    "a\\b\\",                                # interior and trailing
    "café ☃ 日本語",                          # non-ascii
    "",                                      # empty value
    " ",                                     # whitespace-only value
    "\t",                                    # a bare tab
    "x\ty",                                  # tab inside a value
    "\tx\t",                                 # tabs at both ends (strip is quotes only)
    "/data/users/owner/cookies.txt,/data/users/family/cookies.txt",
    "$(rm -rf)",                             # shell-ish, not our problem
    "`ticks` $dollar",
    "line\twith\ttabs",
    "x" * 400,
    "-flag",                                 # leading dash
    "#hash",                                 # comment-ish
    "50% of x,y=z",                          # %, space, comma and = together
]

# What the format cannot hold, one entry per family (see envfmt's docstring):
# both quote kinds present / a quote at either end / a newline.

# Values env_val refuses, i.e. the format cannot be trusted to carry them:
# both quote kinds present, a quote at either end, or a newline.
UNEXPRESSIBLE = [
    'both \' and " here',                    # both kinds, plus a space
    'a"b\'c',                                # both kinds, no separator (see below)
    'x"y\'z more',                           # both kinds with a space
    'a "b\' c',                              # both kinds inside a group
    'he said "hi"',                          # ends with a quote
    "trailing '",                            # ...at the end
    "'leading",                              # starts with a quote
    '"leading',                              # ...at the start
    "\"quoted at both ends\"",               # the parser strips both ends
    "'quoted'",                              # same, single kind
    "line\nbreak",                           # ends the config-file line
    "trailing\n",                            # ...even at the end
    "carriage\rreturn",                      # whitespace to shlex, line end to the file
]

# The subset that supervisor's own parser demonstrably mangles when written
# with no quoting at all - which is the whole point of refusing instead of
# guessing. `a"b'c` is deliberately absent: that one parses to itself raw,
# which test_a_both_quote_kind_value_without_separators_documents why we do
# not exploit.
RAW_MANGLE = [v for v in UNEXPRESSIBLE if v != 'a"b\'c']

def supervisor_parser():
    """supervisor's parser, or None on a host without supervisor installed.

    Inside the image this is the real thing (`supervisor` is a runtime
    dependency: PID 1). Off-image it is absent, and `parse_env_line` - which
    `test_parse_env_line_mirrors_supervisors_parser` pins to the real one here
    - is what the rest of the suite reads generated config with.
    """
    try:
        from supervisor.datatypes import dict_of_key_value_pairs
    except ImportError:
        return None
    return dict_of_key_value_pairs


# The locals supervisord supplies when it %-interpolates a program section
# (supervisor/options.py: `here`, the group/process names, and ENV_<name> for
# every name in the daemon's own environment).
LOCALS = {"program_name": "p", "group_name": "g", "here": "/etc/supervisor",
          "host_node_name": "h", "here_hostname": "h", "relative_directory": ".",
          "ENV_PROGRAM_NAME": "P"}


def expand(line: str) -> str:
    """Stage 1 of supervisor's config pipeline: %-interpolation over the raw
    line, with the locals supervisord supplies for a program section."""
    return line % LOCALS


def load_program_env(envbody: str, tmp_path):
    """Hand `envbody` to supervisord's own loader (a real file, a real
    ServerOptions) and return the environment map a program would start with.

    Whatever supervisord raises for an unloadable file (usage() -> SystemExit,
    or a ValueError) propagates, so a caller can assert refusal, not just a
    wrong value. childlogdir and the logfile directory must exist: supervisor
    validates those at parse time, which is why the entrypoint mkdirs
    /var/log/sup before it hands over.
    """
    options = pytest.importorskip("supervisor.options",
                                  reason="needs the image's supervisor")
    (tmp_path / "cl").mkdir(exist_ok=True)
    base = tmp_path / "supervisord.conf"
    base.write_text(textwrap.dedent(f"""
        [unix_http_server]
        file={tmp_path}/sock
        [rpcinterface:supervisor]
        supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface
        [supervisorctl]
        serverurl=unix://{tmp_path}/sock
        [supervisord]
        nodaemon=true
        logfile={tmp_path}/cl/supervisord.log
        pidfile={tmp_path}/cl/supervisord.pid
        childlogdir={tmp_path}/cl
        [program:victim]
        command=/bin/true
        directory={tmp_path}
        stdout_logfile={tmp_path}/cl/victim.log
        environment={envbody}
        """), newline="\n")
    so = options.ServerOptions()
    so.configfile = str(base)
    so.process_config()
    return so.process_group_configs[0].process_configs[0].environment


# ------------------------------------------------------------------ round trip
@pytest.mark.parametrize("value", HOSTILE)
def test_values_written_by_env_str_survive_supervisors_parser(value):
    """The contract the whole stack depends on: what env_str writes, supervisor
    reads back as the SAME string. Verified against supervisor's parser, not
    against envfmt's own inverse."""
    parser = pytest.importorskip("supervisor.datatypes",
                                 reason="needs the image's supervisor")
    from supervisor.datatypes import dict_of_key_value_pairs
    rendered = envfmt.env_str({"V": value})
    # Both stages a config line goes through, in supervisor's order.
    assert dict_of_key_value_pairs(expand(rendered)) == {"V": value}


@pytest.mark.parametrize("value", HOSTILE)
def test_parse_env_line_reads_back_what_env_str_writes(value):
    """The host-portable inverse used by the other test modules has to agree
    with the emitter for every value in the corpus (this is the pairing that
    makes a divergence a test failure instead of a changed meaning). Fed the
    INTERPOLATED line, because that is the text the shlex stage sees; the raw
    file line still holds `%%` for a `%`, and only supervisord's expansion
    turns it back (see test_parse_env_line_reads_the_written_line)."""
    line = expand(envfmt.env_str({"V": value}))
    assert envfmt.parse_env_line(line) == {"V": value}


def test_parse_env_line_reads_the_written_line_for_percent_free_values():
    """On the raw line, `%%` is still doubled, so the inverse returns the
    doubled text - which is why the corpus tests above interpolate first.
    Every value gen-conf generates itself (paths, ports, loopback URLs) has no
    `%` at all, so those tests can - and do - feed the file's own line."""
    assert envfmt.parse_env_line(envfmt.env_str({"V": "/data/a.txt"})) == \
        {"V": "/data/a.txt"}
    assert envfmt.parse_env_line(envfmt.env_str({"V": "100%"})) == \
        {"V": "100%%"}


def test_parse_env_line_mirrors_supervisors_parser():
    """Where both exist, they produce identical maps - over a multi-pair line,
    which is the shape gen-conf actually emits (comma-joined, one line)."""
    parser = supervisor_parser()
    if parser is None:
        pytest.skip("supervisor is only in the image")
    names = [f"NAME_{i}" for i in range(len(HOSTILE))]
    line = expand(envfmt.env_str(dict(zip(names, HOSTILE))))
    assert parser(line) == envfmt.parse_env_line(line)


def test_a_multi_pair_line_survives_whole():
    """A `,` inside a quoted value must not be read as a pair separator - the
    failure mode for TUNESTER_COOKIES and the vault push-URL lists."""
    parser = supervisor_parser()
    vals = {"A": "/a/cookies.txt,/b/cookies.txt", "B": "x,y,z", "C": "single"}
    line = expand(envfmt.env_str(vals))
    assert line.count(",") > 2 and "\n" not in line
    out = parser(line) if parser else envfmt.parse_env_line(line)
    assert out == vals


def test_the_whole_pipeline_delivers_the_same_map(tmp_path):
    """End to end, no shortcuts: a real config file, supervisord's own loader,
    and the environment a program actually starts with - over the whole corpus
    at once, which is the shape a partition's `settings` produce."""
    vals = {f"SETTING_{i}": v for i, v in enumerate(HOSTILE)}
    got = load_program_env(envfmt.env_str(vals), tmp_path)
    assert got == vals
    assert envfmt.parse_env_line(expand(envfmt.env_str(vals))) == vals


# ------------------------------------------------------------- inexpressible
@pytest.mark.parametrize("value", UNEXPRESSIBLE)
def test_unexpressible_values_are_refused_not_mangled(value):
    """env_val returns None rather than emitting something whose meaning
    changes. Every one of these PARSES in supervisor - into something else."""
    assert envfmt.env_val(value) is None
    assert not envfmt.renderable(value)
    with pytest.raises(ValueError) as exc:
        envfmt.env_str({"V": value})
    assert envfmt.REASON in str(exc.value)
    assert "V" in str(exc.value)


@pytest.mark.parametrize("value", RAW_MANGLE)
def test_refused_values_parse_to_something_other_than_themselves(value):
    """Why refusing is right: write one of these with no quoting at all and
    supervisor's own parser hands the program a DIFFERENT string (quotes eaten
    from the ends, the value cut at a space, the rest of the line swallowed) -
    or refuses the line outright. Either way nothing about the value survives
    to `os.environ`, which is why env_val returns None instead of guessing."""
    parser = supervisor_parser()
    if parser is None:
        pytest.skip("supervisor is only in the image")
    from supervisor.datatypes import dict_of_key_value_pairs
    try:
        got = dict_of_key_value_pairs(expand(f"V={value}"))
    except ValueError as exc:
        # The line does not load at all. That is the harsher failure: supervisord
        # rejects the whole generated file (usage() exit), so every partition in
        # the box is down instead of one program starting with a wrong value.
        assert "quotation" in str(exc) or "key/value pairs" in str(exc)
        return
    assert got != {"V": value}


def test_a_both_quote_kind_value_without_separators_documents_the_refusal():
    """The measured exception, kept so nobody "simplifies" the both-kinds rule
    into a space-only rule: `a"b'c` written RAW parses to itself, and even with
    a space behind it (`V=a"b'c more`) supervisor still reads the whole value -
    shlex only enters quote mode at a token boundary, so a quote in the middle
    of a word is inert and a symmetric pair is stripped. That is the shape a
    band name or a file name has (`Gun N' Roses "Live"'), and it survives. But
    the single-wrapped form a "prefer ' when both kinds" rule would emit parses
    to `a"b`, an unbalanced end is eaten from the inside, and the raw form
    stops parsing as soon as the value needs QUOTING to survive a comma at all
    (see the newline and 'both kinds + space' entries above). Nothing in the
    safe case says a both-kind value is safe to write, so env_val refuses the
    family and this test documents the boundary it refuses at."""
    parser = supervisor_parser()
    if parser is None:
        pytest.skip("supervisor is only in the image")
    from supervisor.datatypes import dict_of_key_value_pairs
    value = 'a"b\'c'
    assert dict_of_key_value_pairs(expand(f"V={value}")) == {"V": value}
    assert dict_of_key_value_pairs(expand(f"V={value} more")) == {"V": value}
    # What a wrapping rule would write instead:
    assert dict_of_key_value_pairs(expand(f"V='{value}'")) == {"V": 'a"b'}
    # And an unbalanced end gets eaten from the inside:
    assert dict_of_key_value_pairs(expand('V=\'a"b\\\'c\'')) == {"V": 'a"b\\'}


def test_a_literal_newline_kills_the_program_not_just_the_value(tmp_path):
    """A newline is inexpressible for a different reason than the quote shapes:
    this is a config FILE line, so the newline ends it and the remainder goes
    to the INI reader with the quote still open. supervisord refuses the whole
    file, so every partition in the box fails, not just the program whose
    setting held the newline."""
    with pytest.raises((SystemExit, ValueError)):
        load_program_env('A="one\ntwo",B="1"', tmp_path)
    # The same map without the newline loads, so it was the newline.
    assert load_program_env('A="onetwo",B="1"', tmp_path) == \
        {"A": "onetwo", "B": "1"}


def test_a_lone_percent_in_a_real_file_kills_the_program_too(tmp_path):
    """The %-stage failure, through the loader rather than through a helper: a
    password containing a `%` is a usage() exit for the whole config, which is
    why the CLI has to name the key instead of shipping the file."""
    with pytest.raises((SystemExit, ValueError)):
        load_program_env('PASS="hunter2%",OTHER="1"', tmp_path)
    assert load_program_env(envfmt.env_str({"PASS": "hunter2%", "OTHER": "1"}),
                            tmp_path) == {"PASS": "hunter2%", "OTHER": "1"}


def test_unsuitable_names_the_bad_keys_in_order():
    vals = {"OK1": "fine", "BAD1": 'both " and \'', "OK2": "a b",
            "BAD2": "two\nlines"}
    assert envfmt.unsuitable(vals) == ["BAD1", "BAD2"]
    assert envfmt.unsuitable({}) == []
    # Backslashes are NOT among them: shlex here is non-POSIX and inert.
    assert envfmt.unsuitable({"SLASH": "ends\\", "LONE": "\\", "BOTH": "a\\b\\"}) \
        == []


# ----------------------------------------------------------------- the stages
def test_a_lone_percent_breaks_the_file_not_just_the_value():
    """Stage 1 is %-interpolation over the RAW line, so an unescaped `%` raises
    while supervisord is loading the config - i.e. every program dies."""
    with pytest.raises(ValueError):
        expand('V=100%')
    # env_str doubled it, so the stage survives and yields the original.
    assert expand(envfmt.env_str({"V": "100%"})) == 'V="100%"'


def test_the_two_stages_compose_in_supervisors_order():
    """Quoting happens after doubling; if the order flipped, `%%` would reach
    shlex doubled and the app would see `%%`."""
    line = envfmt.env_str({"V": "50% of a,b"})
    parser = supervisor_parser()
    out = parser(expand(line)) if parser else envfmt.parse_env_line(expand(line))
    assert out == {"V": "50% of a,b"}


def test_doubling_also_protects_supervisors_own_interpolation_idiom():
    """`%%(here)s` survives as the LITERAL text `%(here)s`, because Python's
    %-interpolation is a single pass and `%%` consumes the `%` that would have
    started the name: a settings value quoting a supervisord path keeps its
    meaning instead of being rewritten to the config directory."""
    vals = {"A": "%(here)s", "B": "/srv/x/%(program_name)s"}
    written = envfmt.env_str(vals)
    assert written == 'A="%%(here)s",B="/srv/x/%%(program_name)s"'
    # Stage 1 turns the doubled file text back into the single form:
    line = expand(written)
    assert line == 'A="%(here)s",B="/srv/x/%(program_name)s"'
    parser = supervisor_parser()
    out = parser(line) if parser else envfmt.parse_env_line(line)
    assert out == vals


def test_empty_dict_renders_empty_body():
    assert envfmt.env_str({}) == ""


def test_values_are_quoted_even_when_not_required():
    """Uniform quoting is what keeps the shlex stage from treating `%%` or a
    leading `-` as anything other than literal text."""
    assert envfmt.env_str({"V": "x"}) == 'V="x"'
