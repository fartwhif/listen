r"""The final fallback reads names out of OBSCURED Usenet posts.

The operator's case: an album whose only copy on this box is already in the
NZB manifest cache, posted by a poster whose subjects hide the file name -
`[PRiVATE]-[EnCrYpTnZb]-[29-oingo_boingo-goodbye-goodbye-marr.mp3]-[33/36] -
"" yEnc 9180638 (1/13)`. Two independent failures kept it unreachable:

1. `_nzb_file_name` distilled those subjects (and the `\HASH\::Artist - Album
   - Track.flac (1/298)` / `... - 01 Modern Love.flac (1/52)` shapes) to the
   part counter `(1/298)`, so the release looked like it had NO files; and
2. every search layer only judges releases a SEARCH returned, and nzb.life's
   search matches the RELEASE TITLE - a release titled neither the song nor
   its album was invisible however good its file names were.

The fix under test:
- `_nzb_file_name` gained tier 3 (peel the part counter / yEnc banner / brand
  tags / `\HASH\::` prefixes and `::/seg/seg` segments, accept the remainder
  only when it ends in a real extension) - measured over this box's real
  manifest corpus: 153 subjects distill to a media file that used to distill
  to nothing, 0 lost a name they used to have;
- `_nzb_release_name` derives what a manifest calls itself (the NZB's own
  `<meta type="name">`, else the scene tag the subjects share, else their
  longest common prefix) so a cached release can be named and attributed;
- `_nzb_cached_cands` sweeps the manifest CACHE for releases whose FILE NAMES
  carry the song, and `tune` hands those candidates to the SAME
  `_try_candidates` the other sources use - probe, MusicBrainz duration
  anchor, AcoustID attribution/live veto, LLM version gate. The sweep is the
  only thing that changed about the search; every gate still decides, so a
  release that merely looks right by name still dies at download/verify.

Everything runs against the vendored tree with synthetic manifests in a
tmpdir cache: no nzb.life, no NNTP, no MusicBrainz, no AcoustID, no LLM. The
subjects below are copied from real posts in this box's cache.
"""
import importlib
import os
import sys
from xml.sax.saxutils import quoteattr

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# The repo keeps the vendored tree under `stack/tunester`; the image flattens
# `stack/*` to the top of /srv/stack. Both are searched so the suite says the
# same thing in a host checkout and inside the image.
for _cand in (os.path.join(BASE, "stack", "tunester"),
              os.path.join(BASE, "tunester")):
    if os.path.isdir(_cand):
        TUNE_DIR = _cand
        break
else:
    TUNE_DIR = os.path.join(BASE, "stack", "tunester")  # names it in skips

NS = "http://www.newzbin.com/DTD/2003/nzb"


@pytest.fixture(scope="module")
def tune_pipe():
    """`pipeline` from the vendored tree, with its generic sibling names kept
    off everyone else's import path afterwards."""
    sys.path.insert(0, TUNE_DIR)
    saved = {}
    try:
        for name in ("pipeline", "sources", "llm"):
            saved[name] = sys.modules.get(name)
        yield importlib.import_module("pipeline")
    finally:
        sys.path.remove(TUNE_DIR)
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture(autouse=True)
def clean_cache_env(monkeypatch):
    """Every test gets its own cache dir; none inherits its predecessor's."""
    monkeypatch.delenv("TUNESTER_NZB_CACHE", raising=False)


def nzb(files, name_meta=None, poster="poster@example.com"):
    """An NZB 1.1 manifest: `files` = list of (subject, date, group)."""
    head = ""
    if name_meta:
        head = f'<head><meta type="name">{name_meta}</meta></head>'
    body = "".join(
        f'<file poster={quoteattr(poster)} date="{date}" '
        f'subject={quoteattr(subj)}>'
        f'<groups><group>{grp}</group></groups>'
        f'<segments><segment bytes="700000" number="1">'
        f'{date}.1@news.example.com</segment></segments></file>'
        for subj, date, grp in files)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<nzb xmlns="{NS}">{head}{body}</nzb>').encode()


def seed(tmp_path, url, manifest):
    """Put one release's manifest in a tmpdir cache and point the tuner at it."""
    d = tmp_path / "cache"
    d.mkdir(exist_ok=True)
    (d / f"{url}.nzb").write_bytes(manifest)
    os.environ["TUNESTER_NZB_CACHE"] = str(d)
    return d


# ---------------------------------------------------------------- distillation

def test_obfuscated_bracket_name_is_the_file_name(tune_pipe):
    r"""The empty quoted slot is the whole trick: the name moved into a
    bracket tag, so a bracket tag ending in an extension IS the file."""
    subj = ('[PRiVATE]-[EnCrYpTnZb]-[29-oingo_boingo-goodbye-goodbye-marr.mp3]'
            '-[33/36] - "" yEnc 9180638 (1/13)')
    assert tune_pipe._nzb_file_name(subj) == \
        "29-oingo_boingo-goodbye-goodbye-marr.mp3"


def test_hash_prefix_subject_distills_to_the_track(tune_pipe):
    r"""`[N3wZ] \HASH\::Artist - Album [spec]04 - Track.flac (1/298)` used to
    distill to the part counter, making the whole release look empty."""
    subj = (r"[N3wZ] \VWNgWR378528\::Metallica - Kill 'em All "
            "[1983-Flac 24Bit 96Khz]04 - Jump In The Fire.flac (1/298)")
    got = tune_pipe._nzb_file_name(subj)
    assert got.endswith("04 - Jump In The Fire.flac")
    assert got.startswith("Metallica - Kill 'em All")
    assert "\\VWNgWR" not in got and "(1/298)" not in got


def test_part_counter_in_the_middle_is_not_mistaken_for_the_release(tune_pipe):
    subj = "David Bowie - Let's Dance (1983) - [03/17] - 01 Modern Love.flac (1/52)"
    got = tune_pipe._nzb_file_name(subj)
    assert got.endswith("01 Modern Love.flac")
    assert not got.startswith("(")


def test_hash_path_inside_a_name_is_collapsed_not_kept(tune_pipe):
    subj = (r"[N3wZ] \iRPjHI378528\::Various Artists - Entertainment Weekly"
            " Greatest Hits - 1982 - A Flock Of Seagulls - I Ran (So Far Away).flac"
            " yEnc (1/73)")
    assert tune_pipe._nzb_file_name(subj) == \
        ("Various Artists - Entertainment Weekly Greatest Hits - 1982"
         " - A Flock Of Seagulls - I Ran (So Far Away).flac")


def test_quoted_name_outranks_a_bracket_tag(tune_pipe):
    """Tier order is unchanged where a stronger tier already worked."""
    subj = '[128415]-[Some-Release-2001-GRP]-[04/23] - "03 - Song.flac" yEnc (1/9)'
    assert tune_pipe._nzb_file_name(subj) == "03 - Song.flac"


def test_scaffolding_only_subject_falls_back_to_last_token(tune_pipe):
    """Tier 3 is gated on a real extension, so a subject that names no file
    never invents one: the old last-token answer survives."""
    assert tune_pipe._nzb_file_name("Some Subject With No File (1/7)") == "(1/7)"
    # A bare extension has no stem, so it is not a name either.
    assert tune_pipe._nzb_file_name("Band - Song [00/11] - \".nzb\" (1/1)") != ".nzb"


def test_part_counter_never_survives_as_a_media_name(tune_pipe):
    subj = (r'[N3wZ] \7Ci31U378528\::System_Of_A_Down-Toxicity-2001-HiTS_iNT'
            '-[09/23] - 09-system_of_a_down-seeking_destroy.flac (1/435)')
    got = tune_pipe._nzb_file_name(subj)
    # What survives is the track file with the poster's own release tag still
    # riding in front of it (that is how these posters post). The point of
    # the tier is that the name ENDS at the media file and that neither the
    # `[09/23]` set counter nor the `(1/435)` part counter is ever the name.
    assert got.endswith("09-system_of_a_down-seeking_destroy.flac")


# ------------------------------------------------------------- release naming

def test_release_name_prefers_the_nzbs_own_meta(tune_pipe):
    m = nzb([("band - song one.flac (1/2)", 1400000000, "a.b.audio"),
             ("band - song two.flac (1/2)", 1400000000, "a.b.audio")],
            name_meta="Falco.-.Falco.3.1985")
    name, poster = tune_pipe._nzb_release_name(m, tune_pipe._nzb_release_files(m))
    assert name == "Falco - Falco 3 1985"  # dots read as separators
    assert poster == "poster@example.com"


def test_release_name_uses_the_shared_scene_tag(tune_pipe):
    subj = ('[128415]-[FULL]-[#alt.binaries]-[System_Of_A_Down-Toxicity-2001'
            '-HiTS_iNT]-[04/23] - 01-system_of_a_down-song.mp3 yEnc (1/9)')
    m = nzb([(subj, 1400000000, "alt.binaries")])
    name, _ = tune_pipe._nzb_release_name(m, tune_pipe._nzb_release_files(m))
    assert name == "System Of A Down-Toxicity-2001-HiTS iNT"


def test_spec_tag_is_not_a_release_name(tune_pipe):
    r"""`[1983-Flac 24Bit 96Khz]` shares the hyphenated shape but names the
    encoding, so it is never picked as the scene tag; the shared head carries
    the name instead, and the per-file track titles diverge out of it."""
    m = nzb([(rf"[N3wZ] \HASH\::Metallica - Kill 'em All [1983-Flac 24Bit 96Khz]"
              f"{n} - {t}.flac (1/298)", 1400000000, "a.b.audio")
             for n, t in ((4, "Jump In The Fire"), (1, "Enter Sandman"))])
    name, _ = tune_pipe._nzb_release_name(m, tune_pipe._nzb_release_files(m))
    assert name.startswith("Metallica - Kill 'em All")
    assert "Jump In The Fire" not in name and "Enter Sandman" not in name
    assert name != "1983-Flac 24Bit 96Khz"


def test_release_name_is_the_shared_head_when_no_tags(tune_pipe):
    """One-file-per-release boxes whose subjects are all
    `Various - Box - 1982 - Artist - Track.flac`: the shared prefix, cut where
    the per-file names diverge, is the release."""
    files = [(f"Various Artists - Billboard Top Hits- 1982 - Toto - {t}.flac (1/1)",
              1450075027, "a.b.wav")
             for t in ("Rosanna", "Africa")]
    m = nzb(files)
    name, _ = tune_pipe._nzb_release_name(m, tune_pipe._nzb_release_files(m))
    assert name == "Various Artists - Billboard Top Hits- 1982 - Toto"


def test_release_name_empty_when_files_share_nothing(tune_pipe):
    m = nzb([("100-vol01.part1.rar (1/1)", 1400000000, "a.b.rar"),
             ("200-vol02.part1.rar (1/1)", 1400000000, "a.b.rar")])
    name, poster = tune_pipe._nzb_release_name(m, tune_pipe._nzb_release_files(m))
    assert name == ""
    assert poster


# ---------------------------------------------------------------- admission

TARGET = ("Oingo Boingo", "Goodbye Goodbye", "1991", "1991", "Best O' Boingo")


def sweep(tune_pipe, target=None, tried=()):
    t = tune_pipe.Target(*(target or TARGET))
    notes = []
    got = tune_pipe._nzb_cached_cands(t, set(tried), notes.append)
    return got, notes


def test_cache_release_naming_the_song_becomes_a_candidate(tmp_path, tune_pipe):
    seed(tmp_path, "aabbccdd11223344", nzb([
        (r"[N3wZ] \HASH\::Oingo Boingo - Best O Boingo 1991 "
         "17 - Goodbye Goodbye.flac (1/298)", 1363246419, "alt.binaries.flac")]))
    got, notes = sweep(tune_pipe)
    assert [c.url for c in got] == ["aabbccdd11223344"]
    assert got[0].source == "nzb"
    assert "Goodbye Goodbye" in got[0].title
    assert any("names the song" in line for line in notes)


def test_obfuscated_bracket_release_is_admitted(tmp_path, tune_pipe):
    """The operator's exact post: no meta name, empty quoted slot."""
    seed(tmp_path, "4121af92d150cd78", nzb([
        ('[PRiVATE]-[EnCrYpTnZb]-[17-oingo_boingo-goodbye_goodbye-marr.mp3]'
         '-[33/36] - "" yEnc 9180638 (1/13)', 1662386596, "alt.binaries.mp3")]))
    got, _ = sweep(tune_pipe)
    assert [c.url for c in got] == ["4121af92d150cd78"]
    # A one-file obfuscated release yields no derivable release name, so the
    # distilled FILE name is what the gates get shown.
    assert got[0].title == "17-oingo_boingo-goodbye_goodbye-marr.mp3"


def test_wrong_song_is_never_admitted(tmp_path, tune_pipe):
    seed(tmp_path, "deadbeefcafebabe", nzb([
        (r"[N3wZ] \HASH\::Oingo Boingo - Best O Boingo 1991 "
         "12 - Nothing To Hear.flac (1/298)", 1363246419, "alt.binaries.flac")]))
    got, notes = sweep(tune_pipe)
    assert got == []
    assert any("no cached release named the song" in line for line in notes)


def test_artist_must_appear_in_file_or_release_name(tmp_path, tune_pipe):
    """A VA box whose track file names the song but not the artist, under a
    release name that names neither, is not this artist's copy."""
    seed(tmp_path, "5555aaaa6666bbbb", nzb([
        ("13 - Don't You Want Me.flac (1/1)", 1622635241, "alt.binaries.flac")]))
    got, _ = sweep(tune_pipe, target=("Human League", "Don't You Want Me", ""))
    assert got == []


def test_artist_carried_by_the_release_name_is_enough(tmp_path, tune_pipe):
    seed(tmp_path, "7777cccc8888dddd", nzb([
        ("Various Artists - Hits Of 1982 - The Human League - Don't You "
         "Want Me.flac (1/1)", 1622635241, "alt.binaries.flac")]))
    got, _ = sweep(tune_pipe, target=("Human League", "Don't You Want Me", ""))
    assert [c.url for c in got] == ["7777cccc8888dddd"]


def test_tried_releases_are_not_re_swept(tmp_path, tune_pipe):
    seed(tmp_path, "aabbccdd11223344", nzb([
        ("Oingo Boingo - Best O Boingo 17 - Goodbye Goodbye.flac (1/1)",
         1363246419, "alt.binaries.flac")]))
    got, _ = sweep(tune_pipe, tried={"aabbccdd11223344"})
    assert got == []


def test_non_release_stems_and_torn_cache_are_ignored(tmp_path, tune_pipe):
    d = tmp_path / "cache"
    d.mkdir()
    (d / "aabbccdd11223344.nzb").write_bytes(nzb([
        ("Oingo Boingo - Best O Boingo 17 - Goodbye Goodbye.flac (1/1)",
         1363246419, "alt.binaries.flac")]))
    os.environ["TUNESTER_NZB_CACHE"] = str(d)
    (d / "not-a-release-hash.nzb").write_text("not xml at all")  # bad stem
    (d / "deadbeefcafe.nzb").write_bytes(nzb([                 # short stem
        ("Oingo Boingo - Other Album 03 - Goodbye Goodbye.flac (1/1)",
         1400000000, "alt.binaries.flac")]))
    got, _ = sweep(tune_pipe)
    assert [c.url for c in got] == ["aabbccdd11223344", "deadbeefcafe"]


def test_missing_cache_dir_is_not_an_error(tune_pipe, monkeypatch):
    monkeypatch.setenv("TUNESTER_NZB_CACHE", "/nonexistent-tunester-cache")
    assert sweep(tune_pipe)[0] == []


def test_album_titled_release_ranks_above_a_va_box(tmp_path, tune_pipe):
    d = tmp_path / "cache"
    d.mkdir()
    (d / "1111aaaa2222bbbb.nzb").write_bytes(nzb([
        ("Various Artists - Greatest Hits 1995 - Oingo Boingo - Goodbye "
         "Goodbye.flac (1/1)", 1300000000, "alt.binaries.flac")]))
    (d / "3333cccc4444dddd.nzb").write_bytes(nzb([
        ("Oingo Boingo - Best O Boingo 17 - Goodbye Goodbye.flac (1/1)",
         1500000000, "alt.binaries.flac")]))
    os.environ["TUNESTER_NZB_CACHE"] = str(d)
    got, _ = sweep(tune_pipe)
    assert [c.url for c in got] == ["3333cccc4444dddd", "1111aaaa2222bbbb"]


def test_oldest_posting_ranks_first_for_equal_releases(tmp_path, tune_pipe):
    """Re-uploads of the same release: the original predates them."""
    d = tmp_path / "cache"
    d.mkdir()
    for url, when in (("5555eeee6666ffff", 1600000000),
                      ("7777aaaa8888bbbb", 1300000000)):
        (d / f"{url}.nzb").write_bytes(nzb([
            (f"Oingo Boingo - Best O Boingo 17 - Goodbye Goodbye {url}.flac (1/1)",
             when, "alt.binaries.flac")]))
    os.environ["TUNESTER_NZB_CACHE"] = str(d)
    got, _ = sweep(tune_pipe)
    assert [c.url for c in got] == ["7777aaaa8888bbbb", "5555eeee6666ffff"]


def test_sweep_candidate_carries_its_own_identity(tmp_path, tune_pipe):
    seed(tmp_path, "9999aaaa0000bbbb", nzb(
        [('[PRiVATE]-[EnCrYpTnZb]-[17-oingo_boingo-goodbye_goodbye-marr.mp3]'
          '-[33/36] - "" yEnc 9180638 (1/13)', 1662386596, "alt.binaries.mp3")],
        poster="nEwZ[NZB] <pr3d@NET.world>"))
    got, _ = sweep(tune_pipe)
    assert got[0].uploader == "nEwZ[NZB] <pr3d@NET.world>"
    assert got[0].posted == 1662386596.0


# ----------------------------------------------------------------- gate wiring

@pytest.fixture
def no_network(tune_pipe, monkeypatch):
    """The search + fetch + verify primitives, with every call recorded in
    pipeline order; nothing touches nzb.life, NNTP, MusicBrainz or the LLM.

    The cache sweep is the REAL one (it reads only this test's tmpdir
    manifest cache, never the network) wrapped so the run records that it
    ran; what it admits is what `_try_candidates` is then handed."""
    calls = {"order": [], "gate_ok": [], "result": None}

    monkeypatch.setattr(tune_pipe, "discover",
                        lambda raw, llm, use_llm, log: tune_pipe.Target(
                            *TARGET))
    monkeypatch.setattr(tune_pipe, "find_cookies", lambda explicit: None)
    monkeypatch.setattr(tune_pipe, "_delivery_get", lambda *a: None)
    monkeypatch.setattr(tune_pipe, "nzb_configured", lambda: True)
    monkeypatch.setattr(tune_pipe, "search_candidates",
                        lambda *a, **k: calls["order"].append("search") or [])
    monkeypatch.setattr(tune_pipe, "_nzb_album_cands",
                        lambda *a: calls["order"].append("pluck") or [])
    monkeypatch.setattr(tune_pipe, "_nzb_comp_cands",
                        lambda *a: calls["order"].append("comp") or [])

    sweep_real = tune_pipe._nzb_cached_cands  # capture before wrapping

    def real_sweep(target, tried, log):
        calls["order"].append("sweep")
        return sweep_real(target, tried, log)

    monkeypatch.setattr(tune_pipe, "_nzb_cached_cands", real_sweep)

    def fake_try(cands, *a, **k):
        calls["order"].append("try")
        calls["gate_ok"].append([c.url for c in cands])
        # Nothing to judge hands nothing back, as the real gate loop returns
        # for an empty list - so the run only reaches the sweep when the
        # sweep is what is left.
        return calls["result"] if cands else None

    monkeypatch.setattr(tune_pipe, "_try_candidates", fake_try)
    return calls


def run_tune(tune_pipe, monkeypatch, cache_dir):
    """One full pipeline run against the stubbed world, expecting the
    sources to run out."""
    monkeypatch.setenv("TUNESTER_NZB_CACHE", str(cache_dir))
    with pytest.raises(tune_pipe.TuneError) as err:
        got = tune_pipe.tune("Oingo Boingo - Goodbye Goodbye",
                             opts=tune_pipe.Options(outdir=cache_dir / "out",
                                                    use_llm=False),
                             log=lambda *a: None)
        raise AssertionError(f"tune returned {got!r}, expected TuneError")
    return err.value


# A cached release that genuinely holds the song: the file name carries it.
SONG_NZB = [("Oingo Boingo - Best O Boingo 17 - Goodbye Goodbye.flac (1/1)",
             1363246419, "alt.binaries.flac")]


def test_sweep_runs_last_after_the_compilation_scan(tune_pipe, no_network,
                                                    tmp_path, monkeypatch):
    cache = seed(tmp_path, "aabbccdd11223344", nzb(SONG_NZB))
    run_tune(tune_pipe, monkeypatch, cache)
    # The sweep is the LAST layer - after every source's search, the album
    # pluck and the artist compilation browse - and what it admits goes to
    # `_try_candidates` exactly like any other source's list.
    assert no_network["order"] == ["search", "pluck", "search", "search",
                                   "comp", "try", "sweep", "try"]
    assert no_network["gate_ok"][-1] == ["aabbccdd11223344"]


def test_a_swept_release_is_still_gate_decided(tune_pipe, no_network,
                                               tmp_path, monkeypatch):
    """The sweep widens what the gates judge; it never delivers on a name."""
    cache = seed(tmp_path, "aabbccdd11223344", nzb(SONG_NZB))
    # `_try_candidates` - the probe, the MusicBrainz length anchor, the
    # AcoustID attribution/live veto and the LLM version gate - rejects
    # everything it is handed, as it would a release that only LOOKS right
    # by name: the run still ends in the honest "nothing verified" failure
    # and writes no file, exactly as before this fallback existed.
    err = run_tune(tune_pipe, monkeypatch, cache)
    assert no_network["gate_ok"][-1] == ["aabbccdd11223344"]  # it WAS judged
    assert "no verified" in str(err)
    # `tune` creates the outdir before it searches, so the proof that
    # nothing was delivered is an empty store, not a missing directory.
    assert list((cache / "out").iterdir()) == []


def test_sweep_reaches_the_delivery_when_the_gates_pass(tune_pipe, no_network,
                                                        tmp_path, monkeypatch):
    """And the fallback is what turns a gate-passing cached release into a
    delivery: the swept candidate's own identity comes back on the Result."""
    cache = seed(tmp_path, "aabbccdd11223344", nzb(SONG_NZB))
    dst = cache / "out" / "Oingo Boingo - Goodbye Goodbye (1991).mp3"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"ID3")
    no_network["result"] = tune_pipe.Result(
        "Oingo Boingo", "Goodbye Goodbye", "1991", "nzb", "aabbccdd11223344",
        dst, "acoustid", 253.0)
    got = tune_pipe.tune("Oingo Boingo - Goodbye Goodbye",
                         opts=tune_pipe.Options(outdir=cache / "out",
                                                use_llm=False),
                         log=lambda *a: None)
    assert got.file == dst
    assert got.source == "nzb" and got.url == "aabbccdd11223344"
    assert no_network["order"][-2:] == ["sweep", "try"]
